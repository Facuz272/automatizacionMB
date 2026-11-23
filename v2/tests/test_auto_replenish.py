"""Low-cost autopilot (no network): Places IDs Only + Place Details, the run cost ceiling, the free monthly usage,
CAB management firms as a seed, auto-replenish demand / territory queue / run, and the AWS deploy files."""

import json
from pathlib import Path

import pytest

from lead_engine import auto_replenish as ar
from lead_engine import cam, costs, dbpr, pipeline
from lead_engine.collectors import cab_seed
from lead_engine.collectors import google_places as gp
from lead_engine.config import settings
from lead_engine.state import Ledger
from lead_engine.web_extract import parse_page


class Resp:
    def __init__(self, body, status=200):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


def place(pid, name, address, phone, types=("condominium_complex",), website=None):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": address, "nationalPhoneNumber": phone,
            "websiteUri": website, "googleMapsUri": f"https://maps.google.com/?cid={pid}", "rating": 4.1,
            "userRatingCount": 20, "businessStatus": "OPERATIONAL", "types": list(types),
            "location": {"latitude": 25.8, "longitude": -80.13}}


@pytest.fixture
def places_api(monkeypatch):
    """Fake Places API (New): records every call with its field mask."""
    object.__setattr__(settings, "google_places_api_key", "k")
    state = type("API", (), {"calls": [], "ids": [], "details": {}})()

    def post(url, json=None, headers=None, timeout=None):
        state.calls.append(("search", headers["X-Goog-FieldMask"], json["textQuery"]))
        return Resp({"places": [{"id": i} for i in state.ids]})

    def get(url, headers=None, timeout=None):
        pid = url.rsplit("/", 1)[-1]
        state.calls.append(("details", headers["X-Goog-FieldMask"], pid))
        return Resp(state.details[pid])
    monkeypatch.setattr(gp.requests, "post", post)
    monkeypatch.setattr(gp.requests, "get", get)
    costs.tracker.reset()
    return state


# ── Places: IDs Only + Place Details ────────────────────────────────────────

def test_details_mode_pays_only_for_the_candidates_it_opens(places_api):
    object.__setattr__(settings, "places_mode", "details")
    places_api.ids = ["G", "A", "B"]
    places_api.details = {
        "G": place("G", "Oceanside Plaza Parking Garage", "5555 Collins Ave, Miami Beach, FL 33140, USA",
                   "(305) 866-1301", types=("parking",)),
        "A": place("A", "Oceanside Plaza Condo", "5555 Collins Ave, Miami Beach, FL 33140, USA", "(305) 866-1300"),
        "B": place("B", "Other", "1 Other St, Miami Beach, FL 33140, USA", "(305) 111-2222")}
    found = gp.find_place("Oceanside Plaza", "5555 Collins Ave, Miami Beach, FL 33140", "onsite")
    assert found.source_id == "A" and found.phone == "+13058661300" and found.evidence["places_match"] == "address"
    kinds = [c[0] for c in places_api.calls]
    assert kinds == ["search", "details", "details"]                # the garage, then the building; never "B"
    assert places_api.calls[0][1] == "places.id"                     # IDs Only: no charge
    assert "reviews" not in places_api.calls[1][1] and "nationalPhoneNumber" in places_api.calls[1][1]
    assert costs.tracker.counts == {"places_text_search_ids": 1, "places_details_enterprise": 2}
    assert costs.tracker.total() == 0.04                              # 2 × US$ 0.020, the search is free


def test_details_mode_caps_the_calls_per_lookup(places_api):
    object.__setattr__(settings, "places_mode", "details")
    object.__setattr__(settings, "places_details_max", 2)
    places_api.ids = ["X1", "X2", "X3", "X4"]
    places_api.details = {i: place(i, f"Shop {i}", f"{n} Far St, Doral, FL 33178, USA", "(305) 111-000" + str(n))
                          for n, i in enumerate(places_api.ids)}
    assert gp.find_place("Oceanside Plaza", "5555 Collins Ave, Miami Beach, FL 33140", "onsite") is None
    assert [c[0] for c in places_api.calls].count("details") == 2


def test_search_mode_is_unchanged(places_api):
    object.__setattr__(settings, "places_mode", "search")
    calls = []
    import lead_engine.collectors.google_places as g

    def search(q, tok, with_reviews, page_size=20):
        calls.append(q)
        return {"places": [place("A", "Oceanside Plaza Condo", "5555 Collins Ave, Miami Beach, FL 33140, USA",
                                 "(305) 866-1300")]}
    g._search, real = search, g._search
    try:
        assert gp.find_place("Oceanside Plaza", "5555 Collins Ave, Miami Beach, FL 33140", "onsite").source_id == "A"
    finally:
        g._search = real
    assert calls == ["Oceanside Plaza, 5555 Collins Ave, Miami Beach, FL 33140"]


# ── costs ───────────────────────────────────────────────────────────────────

def test_free_monthly_usage_is_subtracted():
    counts = {"places_details_enterprise": 300, "places_text_search_ids": 300, "claude_input_token": 1_000_000}
    assert costs.net_of_free_tier(counts, {"places_details_enterprise": 900}) == pytest.approx(200 * 0.020 + 2.0)
    assert costs.net_of_free_tier({"places_details_enterprise": 300}, {}) == 0.0
    assert costs.DEFAULT_PRICES["places_text_search_ids"] == 0.0


def test_the_cost_ceiling_stops_examining_candidates(monkeypatch, tmp_path):
    seen = []

    def candidates(*a, **k):
        for i in range(10):
            lead = pipeline.Lead(source="dbpr", source_id=f"dbpr:{i}", strategy_kind="onsite",
                                 company_name=f"Tower {i}", address=f"{i} Ocean Dr, Miami Beach, FL 33139")
            seen.append(i)
            yield lead

    def resolve(lead, use_search):
        costs.tracker.add("places_details_enterprise", 2)          # US$ 0.04 per building
    monkeypatch.setattr(pipeline, "_candidates", candidates)
    monkeypatch.setattr(pipeline, "resolve_seed", resolve)
    costs.tracker.budget_usd = 0.10
    try:
        pipeline.run(strategy="onsite", limit=10, sources=["dbpr"], use_ai=False, use_apollo=False, precheck=False,
                     send=False, use_search=False, use_geo=False, check_mx=False, out_dir=tmp_path)
    finally:
        costs.tracker.budget_usd = None
    assert len(seen) == 4                                            # 3 × 0.04 = 0.12 ≥ 0.10 → the 4th stops it


# ── CAB management firms ────────────────────────────────────────────────────

def cab_db(tmp_path):
    conn = dbpr.connect(tmp_path / "dbpr.db")
    cam.ensure(conn)
    rows = [("CAB1", "CAB", "CASTLE MANAGEMENT LLC", None, "CASTLE MANAGEMENT", None, "PLANTATION", "33324", "16", "C", "A", "2027-09-30", 1),
            ("CAB2", "CAB", "CASTLE MANAGEMENT LLC", None, "CASTLE MANAGEMENT", None, "MIAMI", "33130", "23", "C", "A", "2027-09-30", 1),
            ("CAB3", "CAB", "TINY HOA SERVICES INC", None, "TINY HOA SERVICES", None, "DORAL", "33178", "23", "C", "A", "2027-09-30", 1),
            ("CAB4", "CAB", "ORLANDO MGMT INC", None, "ORLANDO MGMT", None, "ORLANDO", "32801", "48", "C", "A", "2027-09-30", 1),
            ("CAB5", "CAB", "EXPIRED MGMT INC", None, "EXPIRED MGMT", None, "MIAMI", "33130", "23", "C", "I", "2020-09-30", 0),
            ("CAM9", "CAM", "PEREZ, ANA", None, "PEREZ ANA", None, "MIAMI", "33130", "23", "C", "A", "2027-09-30", 1)]
    conn.executemany("INSERT INTO cam_licenses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn


def test_cab_candidates_one_per_firm_largest_portfolio_first(tmp_path, monkeypatch):
    from lead_engine import backfill
    conn = cab_db(tmp_path)
    monkeypatch.setattr(backfill, "firm_portfolio", lambda name: {"count": 27} if name and "Castle" in name else None)
    rows = cab_seed.candidates(conn)
    assert [r["name"] for r in rows] == ["Castle Management LLC", "Tiny HOA Services INC"]   # active, Dade/Broward
    assert rows[0]["licenses"] == ["CAB1", "CAB2"] and rows[0]["portfolio"] == 27
    lead = cab_seed.to_lead(rows[0])
    assert lead.source == "cab" and lead.strategy_kind == "portfolio" and lead.management_company == lead.company_name
    assert lead.evidence["lead_kind"] == "management_firm" and lead.licenses["firm"]["license_id"] == "CAB1"
    assert [r["name"] for r in cab_seed.candidates(conn, ("16",))] == ["Castle Management LLC"]


@pytest.mark.parametrize("p,ok", [
    (place("F", "Castle Management", "12270 SW 3rd St, Plantation, FL 33325, USA", "(954) 792-6000",
           types=("real_estate_agency",)), True),
    (place("C", "Castle Towers Condominium", "1 Ocean Dr, Miami Beach, FL 33139, USA", "(305) 1112222"), False),
    (place("O", "Castle Management", "100 Main St, Orlando, FL 32801, USA", "(407) 111-2222",
           types=("real_estate_agency",)), False),
    (place("Z", "Zeta Realty", "1 A St, Miami, FL 33130, USA", "(305) 111-3333", types=("real_estate_agency",)), False),
])
def test_a_listing_is_the_firm_only_when_name_and_area_match(p, ok):
    assert cab_seed.accept_listing("Castle Management LLC", p) is ok


def test_a_cab_firm_becomes_a_portfolio_lead_without_cadastre(places_api, monkeypatch, tmp_path):
    from lead_engine import backfill
    object.__setattr__(settings, "places_mode", "details")
    places_api.ids = ["F"]
    places_api.details = {"F": place("F", "Castle Management", "12270 SW 3rd St, Plantation, FL 33325, USA",
                                     "(954) 792-6000", types=("real_estate_agency",), website="https://castlegroup.com/")}
    firm = cab_seed.to_lead({"license_id": "CAB1", "licenses": ["CAB1"], "name": "Castle Management LLC",
                             "legal_name": "CASTLE MANAGEMENT LLC", "city": "Plantation", "zip": "33324",
                             "county_code": "16", "portfolio": 27})
    monkeypatch.setattr(pipeline, "_candidates", lambda *a, **k: iter([firm]))
    monkeypatch.setattr(pipeline.appraiser, "lookup", lambda a: pytest.fail("an office is not a property to wash"))
    monkeypatch.setattr(pipeline, "crawl", lambda url, max_pages=5: parse_page(
        "<html><body><p>Laura Vega, Director of Operations</p></body></html>", url))
    monkeypatch.setattr(backfill, "firm_portfolio", lambda name: {"by": "company", "key": name, "count": 27,
                        "total_units": 4000, "siblings": [{"name": "Valencia", "units": 200}]})
    monkeypatch.setattr(pipeline.mgmt_domains, "from_catalog", lambda n: None, raising=False)
    out = pipeline.run(strategy="portfolio", limit=1, sources=["cab"], use_ai=False, use_apollo=False, precheck=False,
                       send=False, use_search=False, use_geo=False, check_mx=False, out_dir=tmp_path)
    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text().splitlines() if l.strip()]
    assert len(leads) == 1 and leads[0]["phone"] == "+19547926000"
    payload = json.loads((out / "crm_payload.json").read_text())["leads"][0]
    ficha = payload["sales_strategy"].split("\nFicha: ")[1]
    assert ficha.startswith("Administradora de propiedades") and "administra 27 condominios" in ficha
    assert payload["management_company"] == "Castle Management LLC"
    assert [c[0] for c in places_api.calls] == ["search", "details"]


# ── demand and mix ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("pool,run,deficit", [(42, True, 408), (299, True, 151), (300, False, 0), (500, False, 0)])
def test_demand_refills_below_two_days_up_to_three(pool, run, deficit):
    d = ar.demand({"pool_available": pool, "pool_target": 300}, floor_days=2, target_days=3, max_leads=1000)
    assert d["run"] is run and d["deficit"] == deficit and d["daily"] == 150 and d["target"] == 450


def test_demand_caps_and_needs_the_crm():
    assert ar.demand({"pool_available": 42, "pool_target": 300}, floor_days=2, target_days=3, max_leads=250)["deficit"] == 250
    assert not ar.demand(None)["run"] and not ar.demand({"ok": True})["run"]


def test_split_is_exact_and_mix_comes_from_env(monkeypatch):
    assert ar.split(408, ar.DEFAULT_MIX) == {"condo_onsite": 163, "condo_portfolio": 102, "cab_firm": 143}
    monkeypatch.setenv("REPLENISH_MIX", '{"cab_firm": 1, "nope": 3}')
    assert ar.mix() == {"cab_firm": 1.0}
    monkeypatch.setenv("REPLENISH_MIX", "garbage")
    assert ar.mix() == ar.DEFAULT_MIX


# ── territory queue ─────────────────────────────────────────────────────────

def test_areas_rotate_and_exhausted_ones_are_skipped():
    from lead_engine import tenant
    tenant.use("mb_softwash")
    led = Ledger()
    ar.sync_territories(led)
    first = ar.next_areas(led, "condo_onsite")
    assert first[0] == "Miami Beach" and "Fort Lauderdale" in first
    ar.record_area(led, "condo_onsite", "Miami Beach", accepted=12, exhausted=False)
    assert ar.next_areas(led, "condo_onsite")[-1] == "Miami Beach"            # worked: goes to the back
    ar.record_area(led, "cab_firm", "Miami-Dade", accepted=0, exhausted=True)
    assert ar.next_areas(led, "cab_firm") == ["Broward"]
    assert any("cab_firm" in a for a in ar.inventory_alerts(led, ["cab_firm"]))


def test_monthly_usage_accumulates():
    led = Ledger()
    ar.add_usage(led, {"places_details_enterprise": 400}, month="2026-12")
    ar.add_usage(led, {"places_details_enterprise": 700}, month="2026-12")
    assert ar.usage_this_month(led, "2026-12") == {"places_details_enterprise": 1100}


# ── the run ─────────────────────────────────────────────────────────────────

@pytest.fixture
def crm(monkeypatch):
    from lead_engine import daily, feedback, tenant
    tenant.use("mb_softwash")
    object.__setattr__(settings, "crm_webhook_secret", "s")
    state = type("CRM", (), {"pool": 42, "slack": []})()
    monkeypatch.setattr(daily, "fetch_health", lambda timeout=20: {"pool_available": state.pool, "pool_target": 300})
    monkeypatch.setattr(daily, "post_slack", lambda text, url=None: state.slack.append(text) or True)
    monkeypatch.setattr(pipeline, "resend_pending", lambda: {"resent": [], "still_pending": []})
    monkeypatch.setattr(feedback, "sync", lambda ledger=None: {"ok": True})
    return state


def test_plan_only_spends_nothing(crm, monkeypatch):
    monkeypatch.setattr(ar, "run_slice", lambda *a, **k: pytest.fail("no slice in --plan"))
    s = ar.run(plan_only=True)
    assert s["demand"]["deficit"] == 400                                    # 408 capped by REPLENISH_MAX_LEADS
    assert s["plan"] == {"condo_onsite": 160, "condo_portfolio": 100, "cab_firm": 140}
    assert s["areas"]["cab_firm"] == ["Miami-Dade", "Broward"] and crm.slack == []


def test_nothing_runs_when_the_pool_is_full(crm, monkeypatch):
    crm.pool = 320
    monkeypatch.setattr(ar, "run_slice", lambda *a, **k: pytest.fail("pool is full"))
    s = ar.run(commit=True)
    assert s["skipped"] and "no hace falta" in s["why"] and "no hace falta" in crm.slack[0]


def test_run_fills_by_area_within_the_budget(crm, monkeypatch):
    calls = []

    def fake_slice(kind, area, n, *, commit, budget_left, use_ai=True):
        calls.append((kind, area, n, round(budget_left, 2)))
        got = 0 if area == "Miami Beach" else min(n, 60)                   # Miami Beach is exhausted
        crm.pool += got if commit else 0
        return {"kind": kind, "area": area, "quota": n, "batch": f"b-{len(calls)}", "accepted": got,
                "with_email": got // 2, "inserted": got if commit else 0, "would_insert": got,
                "crm_error": None, "counts": {"places_details_enterprise": 2 * got}, "cost_usd": 0.04 * got}
    monkeypatch.setattr(ar, "run_slice", fake_slice)
    monkeypatch.setenv("REPLENISH_MAX_COST_USD", "8")
    s = ar.run(commit=True)
    assert calls[0][:2] == ("condo_onsite", "Miami Beach") and calls[1][:2] == ("condo_onsite", "Sunny Isles Beach")
    assert s["cost_list_usd"] <= 8 + 0.04 * 60 and any("tope de costo" in a for a in s["alerts"])
    assert s["inserted"] == s["accepted"] and s["pool_after"] == crm.pool and s["with_email"] * 2 == s["accepted"]
    assert s["cost_net_usd"] == 0.0                                          # within the 1,000 free Details calls
    led = Ledger()
    assert led.conn.execute("SELECT exhausted FROM territories WHERE kind='condo_onsite' AND area='Miami Beach'"
                            ).fetchone() == (1,)
    row = led.conn.execute("SELECT committed, pool_before, inserted FROM replenish_runs").fetchone()
    assert row[0] == 1 and row[1] == 42 and row[2] == s["inserted"]
    assert "Pool: 42 →" in crm.slack[-1] and "con email 50%" in crm.slack[-1]


def test_a_failing_crm_queue_blocks_new_spend(crm, monkeypatch):
    monkeypatch.setattr(pipeline, "resend_pending", lambda: {"resent": [], "still_pending": [{"batch_id": "x"}]})
    monkeypatch.setattr(ar, "run_slice", lambda *a, **k: pytest.fail("no new spend while the CRM fails"))
    assert ar.run(commit=True)["skipped"]


def test_cli_plan(crm, capsys):
    from lead_engine.__main__ import main
    assert main(["auto-replenish", "--plan", "--no-slack"]) == 0
    assert '"condo_onsite": 160' in capsys.readouterr().out


# ── deploy files ────────────────────────────────────────────────────────────

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "aws"


def test_timer_runs_at_three_miami_time_and_survives_downtime():
    t = (DEPLOY / "lead-engine-replenish.timer").read_text()
    assert "OnCalendar=*-*-* 03:00:00 America/New_York" in t and "Persistent=true" in t
    svc = (DEPLOY / "lead-engine-replenish.service").read_text()
    assert "EnvironmentFile=/opt/lead-engine/v2/.env" in svc and "run_replenish.sh" in svc


def test_aws_env_template_is_dry_run_and_low_cost():
    env = dict(l.split("=", 1) for l in (DEPLOY / "env.aws.example").read_text().splitlines()
               if l and not l.startswith("#"))
    assert env["REPLENISH_COMMIT"] == "false" and env["PLACES_MODE"] == "details"
    assert env["PLACES_REVIEWS"] == "false" and env["COMPETITOR_NEARBY"] == "false"
    assert env["ANTHROPIC_MODEL"] == "claude-haiku-5-5"
    assert all(not env[k] for k in ("GOOGLE_PLACES_API_KEY", "ANTHROPIC_API_KEY", "CRM_WEBHOOK_SECRET"))
    assert json.loads(env["REPLENISH_MIX"]) == ar.DEFAULT_MIX


# ── run-time guards added after the live smoke test ─────────────────────────

def test_explicit_env_beats_the_tenant_for_nearby(monkeypatch):
    from lead_engine import tenant
    monkeypatch.setenv("COMPETITOR_NEARBY", "false")
    object.__setattr__(settings, "competitor_nearby", False)
    tenant.use("mb_softwash")                                         # the profile says true
    assert settings.competitor_nearby is False
    monkeypatch.delenv("COMPETITOR_NEARBY")
    tenant.use("mb_softwash")
    assert settings.competitor_nearby is True


@pytest.fixture
def crm_snapshot(tmp_path, monkeypatch):
    p = tmp_path / "crm_names.json"
    p.write_text(json.dumps({"leads": [["Miami Management Inc", "1145 Sawgrass Corporate Pkwy, Sunrise, FL 33323"],
                                       ["FirstService Residential Miami", None],
                                       ["Mystic Pointe Tower 200", None],
                                       ["Pillar Management Group LLC", "1000 Brickell Ave, Miami, FL 33131"],
                                       ["Flamenco Towers Condominium", "3701 N Country Club Dr, Aventura, FL 33180"]]}))
    monkeypatch.setenv("CRM_NAMES_FILE", str(p))
    return p


@pytest.mark.parametrize("name,source,address,hit", [
    ("Miami Management INC", "cab", "Miami, FL 33130", "Miami Management Inc"),          # another office line
    ("Firstservice Residential", "cab", "Plantation, FL 33324", "FirstService Residential Miami"),
    ("Miami Association Management", "cab", "Miami, FL 33130", None),
    ("Mystic Pointe Tower 400", "dbpr", "19195 Mystic Pointe Dr, Aventura, FL 33180", None),  # another tower
    ("FLAMENCO TOWER CONDO", "dbpr", "3701 N Country Club Dr, Miami, FL 33180", "Flamenco Towers Condominium"),
    ("1000 Brickell Condo", "dbpr", "1000 Brickell Ave, Miami, FL 33131", None),         # a firm's office there
])
def test_crm_names_catch_leads_under_another_phone(crm_snapshot, name, source, address, hit):
    from lead_engine import crm_names
    from lead_engine.models import Lead
    l = Lead(source=source, source_id=f"{source}:x", strategy_kind="onsite", company_name=name, address=address)
    assert crm_names.known(l) == hit


def test_the_gate_rejects_crm_names_before_paying(crm_snapshot, monkeypatch, tmp_path):
    from lead_engine.models import Lead
    firm = Lead(source="cab", source_id="cab:CAB9", strategy_kind="portfolio", company_name="Miami Management INC",
                address="Miami, FL 33130")
    firm.evidence["lead_kind"] = "management_firm"
    monkeypatch.setattr(pipeline, "_candidates", lambda *a, **k: iter([firm]))
    monkeypatch.setattr(cab_seed, "resolve", lambda lead: pytest.fail("no paid lookup for a lead already in the CRM"))
    out = pipeline.run(strategy="portfolio", limit=1, sources=["cab"], use_ai=False, use_apollo=False, precheck=False,
                       send=False, use_search=False, use_geo=False, check_mx=False, out_dir=tmp_path)
    rej = [json.loads(l) for l in (out / "rejected.jsonl").read_text().splitlines() if l.strip()]
    assert rej[0]["reason"] == "ya existe en el CRM"


@pytest.mark.parametrize("a,b,same", [
    ("GRS Management, INC.", "GRS Community Management", False),
    ("Miami Management INC", "Homes Management of Miami Inc.", False),
    ("Miami Quality Management", "Management Miami, LLC", False),
    ("Trust Management Services Group Corp", "The Management Trust", False),
    ("Firstservice Residential", "FirstService Residential Miami", True),
    ("Kw Property Management & Consulting", "KW PROPERTY MANAGEMENT & CONSULTING", True),
    ("Phoenix Management Services INC", "Phoenix Management Services, Inc", True),
    ("Keystone Property Mgmt INC", "Keystone Property Management", True),
])
def test_same_firm_is_strict(a, b, same):
    from lead_engine import crm_names
    assert crm_names.same_firm(a, b) is same
