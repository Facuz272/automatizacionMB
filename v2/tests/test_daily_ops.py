"""Service-area filter, firm-phone consolidation, enrich-phones, outreach pack and the daily command (no network)."""

import csv
import json

import pytest

from lead_engine import cam, daily, dbpr, outreach_pack, pipeline, portfolio_consolidation as pc, portfolios
from lead_engine.collectors import dbpr_seed, needs_phone
from lead_engine.config import service_area, settings
from lead_engine.models import Lead

from test_pdf_route_intel import LEAD, _pages_and_text
from test_pipeline_e2e import CAM_FIXTURE, FIXTURE, wired  # noqa: F401  (fixture)

pypdf = pytest.importorskip("pypdf")


@pytest.fixture()
def registry(tmp_path):
    conn = dbpr.connect(tmp_path / "dbpr.db")
    dbpr.load_csv(FIXTURE, conn)
    cam.load_csv(CAM_FIXTURE, conn)
    dbpr.index_routes(conn)
    return conn


@pytest.fixture()
def shared(monkeypatch):
    """Restore the shared settings fields a test touches."""
    keys = ("target_cities", "allow_mgmt_firm_phone", "company_booking_url", "company_phone", "company_email",
            "company_website", "company_name", "crm_webhook_secret", "documents_available")
    saved = {k: getattr(settings, k) for k in keys}
    yield settings
    for k, v in saved.items():
        object.__setattr__(settings, k, v)


# ── 1. service area ─────────────────────────────────────────────────────────

def test_portfolio_ranking_counts_only_buildings_inside_the_service_area(registry, monkeypatch):
    assert [f["firm"] for f in portfolios.rank(registry, cities=["Miami Beach"])] == ["Castle Management LLC"]
    assert portfolios.rank(registry, cities=["Brickell Miami"]) == []                     # ZIP alias, nothing there
    assert {f["firm"] for f in portfolios.rank(registry, cities=["Miami Beach", "Hialeah"])} == {
        "Castle Management LLC", "Ritz Mgmt, INC"}
    # A neighbourhood alias reaches buildings filed under another postal city (here 33196 under "MIAMI").
    monkeypatch.setitem(dbpr_seed.CITY_ZIPS, "KENDALL WEST", {"33196"})
    assert [f["license"] for f in portfolios.rank(registry, cities=["Kendall West"])] == ["CAB4789"]


def test_service_area_is_the_tenant_profile_union(shared):
    object.__setattr__(settings, "target_cities", {"onsite": ["Miami Beach", "Aventura"], "portfolio": ["Aventura", "Doral"]})
    assert service_area() == ["Miami Beach", "Aventura", "Doral"]


def test_ranking_defaults_to_the_tenant_area(registry, shared):
    object.__setattr__(settings, "target_cities", {"onsite": ["Hialeah"]})
    assert [f["firm"] for f in portfolios.rank(registry)] == ["Ritz Mgmt, INC"]


# ── 2. one firm phone, several buildings ────────────────────────────────────

CENTRAL = "+19549262921"


def _bldg(i, units, addr, firm="Castle Management LLC"):
    lead = Lead(source="dbpr", source_id=f"dbpr:{i}", strategy_kind="portfolio", company_name=f"Tower {i}",
                address=addr)
    lead.management_company = firm
    lead.management_type = "third_party"
    lead.public_records["dbpr"] = {"units": units, "county": "Dade"}
    return lead


def _fake_official(monkeypatch):
    monkeypatch.setattr(pc.appraiser, "lookup", lambda a: {"year_built": 1967, "floors": 17} if a.startswith("2") else {})
    monkeypatch.setattr(pc.municipal_signals, "lookup", lambda a, **k: [
        {"category": "municipal_permit", "label": "obra exterior con permiso reciente", "weight": 2.0, "source": "municipal",
         "quote": "Permiso 1 (2026-03-31): FACADE", "data": {"permit": "1", "issued": "2026-03-31", "work": "FACADE"}}]
        if a.startswith("3") else [])


def test_consolidate_moves_the_phone_to_the_strongest_building_and_lists_the_rest(monkeypatch):
    _fake_official(monkeypatch)
    a, b, c = (_bldg(1, 300, "100 Collins Ave, Miami Beach, FL 33140"),
               _bldg(2, 240, "200 Collins Ave, Miami Beach, FL 33140"),     # milestone window → strongest
               _bldg(3, 180, "300 Collins Ave, Miami Beach, FL 33140"))     # exterior permit
    for x in (a, b, c):
        x.phone = CENTRAL
        x.evidence["phone_from"] = "firm_central"
    other = _bldg(9, 90, "9 Ocean Dr, Miami Beach, FL 33139")
    other.phone = "+13055550199"
    gated, left = pc.consolidate([(a, None), (other, None)], [b, c])
    assert [l.company_name for l, _ in gated] == ["Tower 2", "Tower 9"]
    assert {l.company_name for l in left} == {"Tower 1", "Tower 3"}
    assert all(l.evidence["consolidated_into"] == "Tower 2" for l in left)
    assert [p["name"] for p in b.portfolio_siblings][0] in ("Tower 1", "Tower 3")
    line = pc.hook_line(b)
    assert line.startswith("Teléfono = central de Castle Management LLC: al llamar, cotizar también")
    assert "[Tower 3 · 300 Collins Ave – 180 un., permiso de obra exterior]" in line and "Tower 1" in line
    assert "Potencial extra estimado: hasta US$" in line


def test_run_consolidates_firm_central_buildings_in_crm_hook_pdf_and_needs_phone(wired, monkeypatch, shared):  # noqa: F811
    from lead_engine import config, phone_resolver
    s = type(config.settings)(**{**config.settings.__dict__, "google_places_api_key": None, "crm_webhook_secret": None,
                                 "allow_mgmt_firm_phone": True})
    monkeypatch.setattr(pipeline, "settings", s)
    object.__setattr__(settings, "allow_mgmt_firm_phone", True)
    _fake_official(monkeypatch)
    blds = [_bldg(1, 300, "100 Collins Ave, Miami Beach, FL 33140"), _bldg(2, 240, "200 Collins Ave, Miami Beach, FL 33140"),
            _bldg(3, 180, "300 Collins Ave, Miami Beach, FL 33140")]
    monkeypatch.setattr(pipeline.dbpr_seed, "collect", lambda strategy, n: iter(blds))
    monkeypatch.setattr(pipeline.osm, "lookup_address", lambda address, name=None: None)
    monkeypatch.setattr(phone_resolver.lodging, "building_phone", lambda a, n: None)
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: {"number": CENTRAL, "source": "firm_website",
                                                                    "firm": "Castle Group", "url": "https://castlegroup.com"})
    out = pipeline.run(strategy="portfolio", limit=5, use_ai=False, use_apollo=False, precheck=False, use_search=False,
                       out_dir=wired / "o", make_pdfs=True, pdf_lang="en")
    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text().splitlines()]
    assert [l["company_name"] for l in leads] == ["Tower 2"] and leads[0]["phone"] == CENTRAL
    payload = json.loads((out / "crm_payload.json").read_text())["leads"][0]
    assert payload["sales_strategy"].startswith("Teléfono = central de Castle Management LLC")
    rows = list(csv.DictReader(open(out / "needs_phone.csv")))
    assert {r["condo"] for r in rows} == {"Tower 1", "Tower 3"}
    assert {r["consolidated_into"] for r in rows} == {"Tower 2"} and {r["firm_phone"] for r in rows} == {CENTRAL}
    n, text = _pages_and_text(out / "pdf" / "Tower_2.pdf")
    assert n == 2 and "Other Castle Management LLC buildings to quote in the same visit" in text and "Tower 3" in text
    report = json.loads((out / "quality_report.json").read_text())
    assert report["consolidated_siblings"] == 2
    pack = json.loads((out / "outreach_pack.json").read_text())
    assert pack[0]["company_name"] == "Tower 2" and "Tower 1" in pack[0]["email_en"]["body"]
    assert pack[0]["script_es"][0].startswith("OJO: el teléfono es la central")

    # enrich-phones: later OSM knows Tower 3's own line → it enters with its own phone.
    monkeypatch.setattr(pipeline.osm, "lookup_address", lambda address, name=None: {
        "lat": 25.83, "lng": -80.12, "phone": "+13058661777", "website": None, "osm_id": "way/3"}
        if address.startswith("300") else None)
    s2 = type(s)(**{**s.__dict__, "allow_mgmt_firm_phone": False})
    monkeypatch.setattr(pipeline, "settings", s2)
    object.__setattr__(settings, "allow_mgmt_firm_phone", False)
    out2 = pipeline.run(strategy=needs_phone.main_strategy(needs_phone.read(out / "needs_phone.csv")), limit=5,
                        sources=["needs_phone"], seed=str(out / "needs_phone.csv"), use_ai=False, use_apollo=False,
                        precheck=False, use_search=False, out_dir=wired / "o2", retry_needs_phone=True)
    leads2 = [json.loads(l) for l in (out2 / "leads.jsonl").read_text().splitlines()]
    assert [(l["company_name"], l["phone"], l["strategy_kind"]) for l in leads2] == [("Tower 3", "+13058661777", "portfolio")]
    assert json.loads((out2 / "crm_payload.json").read_text())["leads"][0]["phone"] == "+13058661777"


def test_needs_phone_rows_round_trip(tmp_path):
    lead = _bldg(7, 120, "700 Collins Ave, Miami Beach, FL 33140")
    lead.evidence["consolidated_into"] = "Tower 2"
    pipeline.write_needs_phone(tmp_path / "n.csv", [lead])
    rows = needs_phone.read(tmp_path / "n.csv")
    back = needs_phone.to_lead(rows[0], "onsite")
    assert (back.source, back.source_id, back.strategy_kind, back.management_company) == (
        "dbpr", "dbpr:7", "portfolio", "Castle Management LLC")
    assert back.evidence["retry_of"] == "Tower 2"


# ── 3. outreach pack ────────────────────────────────────────────────────────

MILESTONE = {"category": "milestone", "label": "ventana legal de Milestone Inspection / recertificación", "weight": 6.0,
             "quote": "x", "data": {"year_built": 1967, "age": 59, "milestone": 60, "milestone_year": 2027,
                                    "coastal": True, "condo": True}}


def test_outreach_messages_use_only_real_facts(shared):
    object.__setattr__(settings, "company_name", "Acme Softwash")
    object.__setattr__(settings, "company_booking_url", None)
    object.__setattr__(settings, "company_phone", None)
    object.__setattr__(settings, "company_email", None)
    object.__setattr__(settings, "company_website", "https://acme.example")
    object.__setattr__(settings, "documents_available", [])
    rec = {**LEAD, "municipal": [MILESTONE] + LEAD["municipal"][1:],
           "portfolio_siblings": [{"name": "Tower 3", "address": "300 Collins Ave", "units": 180, "signals": []}]}
    m = outreach_pack.messages(rec, "Oceanside_Plaza_Condo.pdf")
    body = m["email_en"]["body"]
    assert m["hook_kind"] == "milestone" and body.startswith("Hi Ana,")
    for s in ("completed in 1967", "60-year mark in 2027", "SB 4-D Milestone", "310,037 ft²", "297 units",
              "attached (Oceanside_Plaza_Condo.pdf)", "Tower 3", "https://acme.example"):
        assert s in body, s
    assert "COI" not in body and "W-9" not in body and "reply with a day" in body
    assert len(m["sms_en"]) <= 320 and "Acme Softwash" in m["sms_en"]
    script = " ".join(m["script_es"])
    assert "cumple 60 años en 2027" in script and "310.037 ft²" in script and "Tower 3 (180 un.)" in script

    bare = outreach_pack.messages({"company_name": "Blue Plaza", "address": "1 Main St, Miami, FL 33101",
                                   "municipal": [{"category": "municipal_permit", "label": "x", "quote": "y",
                                                  "data": {"permit": "9", "issued": "2026-02-01", "work": "ROOF COATING"}}]})
    assert bare["email_en"]["body"].startswith("Hi there,") and "roof coating" in bare["email_en"]["body"]
    assert bare["hook_kind"] == "permit" and bare["pdf"] is None


def test_outreach_pack_files(tmp_path, shared):
    md, js = outreach_pack.build([LEAD], tmp_path, [tmp_path / "Oceanside_Plaza_Condo.pdf"])
    text = md.read_text()
    assert "## 1. Oceanside Plaza Condo" in text and "### Guion telefónico (ES)" in text and "**Subject:**" in text
    assert json.loads(js.read_text())[0]["pdf"] == "Oceanside_Plaza_Condo.pdf"


# ── 4. daily ────────────────────────────────────────────────────────────────

def test_batch_size_from_health_answers():
    assert daily.batch_size({"ok": True, "facts": {"pool_available": 31}}, 40, 10) == (9, "Pool con 31 disponibles (objetivo 40)")
    assert daily.batch_size({"pool_restante": 55}, 40, 10)[0] == 0
    assert daily.batch_size({"ok": False, "problems": [{"code": "pool_low"}], "facts": {"pool_low": True}}, 40, 10) == (
        40, "el CRM marca el Pool bajo")
    assert daily.batch_size({"ok": True, "facts": {"pool_low": False}}, 40, 10)[0] == 10
    assert daily.batch_size(None, 40, 10) == (10, "sin lectura del CRM")
    assert daily.pool_count({"facts": {"pool_low": True}}) is None        # booleans are not counts


def test_daily_runs_the_batch_and_posts_the_summary(tmp_path, monkeypatch, shared):
    out = tmp_path / "le2-x"
    out.mkdir()
    rec = {**LEAD, "estimated_deal": {"low": 37000, "high": 68000}}
    (out / "leads.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps({**rec, "company_name": "B", "lead_score": 50,
                                                                           "municipal": [], "pain": []}) + "\n")
    (out / "quality_report.json").write_text(json.dumps({"crm": {"inserted": 2, "would_insert": 0},
                                                         "consolidated_siblings": 1}))
    calls = {}
    monkeypatch.setattr(daily, "fetch_health", lambda: {"ok": False, "facts": {"pool_low": True}})

    def fake_run(**kw):
        calls["run"] = kw
        return out
    monkeypatch.setattr(pipeline, "run", fake_run)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/x")
    posted = []

    class R:
        def raise_for_status(self):
            pass
    monkeypatch.setattr(daily.requests, "post", lambda url, json, timeout: posted.append((url, json)) or R())
    object.__setattr__(settings, "crm_webhook_secret", "s")
    s = daily.run(target_pool=40, commit=True)
    assert calls["run"]["limit"] == 40 and calls["run"]["make_pdfs"] and calls["run"]["pdf_lang"] == "en"
    assert calls["run"]["commit"] and calls["run"]["send"]
    assert s["injected"] == 2 and s["annual_usd_high"] == 136000 and s["slack_sent"]
    text = posted[0][1]["text"]
    assert "Inyectados al CRM: *2*" in text and "US$ 74.000 – US$ 136.000" in text
    assert "1. *Oceanside Plaza Condo*" in text and "ventana legal de Milestone" in text and "pdf" in text
    assert json.loads((out / "daily_summary.json").read_text())["top"][0]["name"] == "Oceanside Plaza Condo"


def test_daily_skips_when_the_pool_is_full(monkeypatch, shared):
    monkeypatch.setattr(daily, "fetch_health", lambda: {"facts": {"pool_available": 45}})
    monkeypatch.setattr(pipeline, "run", lambda **kw: pytest.fail("no batch expected"))
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert daily.run(target_pool=40)["skipped"]


def test_cli_wiring_for_new_subcommands(capsys, shared):
    from lead_engine.__main__ import main
    for argv in (["daily", "--help"], ["enrich-phones", "--help"]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 0
    assert main(["daily", "--limit", "0", "--no-slack"]) == 0          # nothing to do, no network
    assert '"skipped": true' in capsys.readouterr().out
    assert settings.tenant == "mb_softwash"                            # daily defaults to the MB Softwash profile


def test_zip_alias_never_pulls_in_other_municipalities():
    area = dbpr_seed.area_filter(["Hollywood FL", "North Miami Beach"])
    assert dbpr_seed.in_area("HOLLYWOOD", "33024", area)
    assert not dbpr_seed.in_area("DAVIE", "33024", area) and not dbpr_seed.in_area("PEMBROKE PINES", "33024", area)
    assert dbpr_seed.in_area("MIAMI", "33181", area) and not dbpr_seed.in_area("NORTH MIAMI", "33181", area)
    assert dbpr_seed.in_area("North Miami Beach", "99999", area)                      # configured by name
