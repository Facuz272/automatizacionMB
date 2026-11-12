"""backfill-crm (no network): CRM paging, priority order, firm vs building, Places identity (phone / site /
unique name, never a garage), enrichment paths, the exact /api/public/leads-enrich payload and the run."""

import json

import pytest
import requests

from lead_engine import backfill, costs, dbpr, pipeline
from lead_engine.collectors import google_places as gp
from lead_engine.config import settings
from lead_engine.normalize import phone_key
from lead_engine.web_extract import parse_page

from test_pipeline_e2e import PLACES, _place, wired  # noqa: F401  (fixture)

ALLOWED = {"id", "phone", "name", "sales_strategy", "management_company", "address", "contact_name",
           "contact_title", "email", "website_url", "maps_url", "mobile_phone"}


def row(**kw):
    base = {"id": "0b9a7f5e-1111-4c2a-9d1e-000000000001", "position": 1, "name": "Oceanside Plaza",
            "phone": "+13058661301", "address": None, "city": None, "zip": None, "segment": "Gran Comunidad",
            "status": "papers_sent", "management_company": None, "contact_name": None,
            "website_url": "https://www.oceansideplaza.org/", "source": None, "enriched_at": None,
            "missing": ["ficha", "management_company", "address"]}
    return {**base, **kw}


class Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, json.dumps(body)

    def json(self):
        return self._body


# ── CRM side ────────────────────────────────────────────────────────────────

def test_fetch_pages_by_position_until_empty(monkeypatch):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    calls = []
    pages = [Resp(200, {"ok": True, "count": 2, "next_after_position": 9, "leads": [row(position=3), row(position=9)]}),
             Resp(200, {"ok": True, "count": 0, "next_after_position": 9, "leads": []})]

    def get(url, headers, timeout, params):
        assert url.endswith("/api/public/leads-unenriched") and headers == {"x-webhook-secret": "s"}
        calls.append(params)
        return pages.pop(0)
    monkeypatch.setattr(backfill.requests, "get", get)
    assert [r["position"] for r in backfill.fetch_unenriched()] == [3, 9]
    assert [c["after_position"] for c in calls] == [0, 9] and calls[0]["include_attempted"] == "false"


def test_fetch_errors_are_explicit(monkeypatch):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    monkeypatch.setattr(backfill.requests, "get", lambda *a, **k: Resp(401, {"error": "unauthorized"}))
    with pytest.raises(backfill.CrmError, match="401"):
        backfill.fetch_unenriched()
    object.__setattr__(settings, "crm_webhook_secret", None)
    with pytest.raises(backfill.CrmError, match="CRM_WEBHOOK_SECRET"):
        backfill.fetch_unenriched()


def test_post_chunks_of_200_and_reindexes(monkeypatch):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    bodies = []

    def post(url, headers, timeout, json):
        bodies.append(json)
        n = len(json["leads"])
        return Resp(200, {"ok": True, "dry_run": json["dry_run"], "received": n, "updated": n, "unchanged": 0,
                          "errors": 0, "not_found": [], "ambiguous": [{"index": 0, "candidates": 2}], "rejected": [],
                          "results": [{"index": i, "id": l["id"], "status": "updated", "fields": ["address"]}
                                      for i, l in enumerate(json["leads"])]})
    monkeypatch.setattr(backfill.requests, "post", post)
    leads = [{"id": f"id-{i}", "address": "1 A St, Miami, FL 33130"} for i in range(250)]
    out = backfill.post_enrich(leads, "enrich-x", dry_run=True)
    assert [len(b["leads"]) for b in bodies] == [200, 50] and all(b["dry_run"] and b["batch_id"] == "enrich-x"
                                                                  for b in bodies)
    assert out["updated"] == 250 and [a["index"] for a in out["ambiguous"]] == [0, 200]
    assert out["results"][-1] == {"index": 249, "id": "id-249", "status": "updated", "fields": ["address"]}


def test_papers_sent_and_live_calls_go_first():
    rows = [row(id=str(i), position=p, status=s) for i, (p, s) in enumerate(
        [(1, "new"), (2, "callback"), (3, "papers_sent"), (4, "future"), (5, "papers_sent"), (6, "no_contact")])]
    assert [r["position"] for r in backfill.select(rows, None, None)] == [3, 5, 2, 6, 4, 1]
    assert [r["position"] for r in backfill.select(rows, list(backfill.ACTIVE), 3)] == [3, 5, 2]
    assert [r["status"] for r in backfill.select(rows, ["future"], None)] == ["future"]


@pytest.mark.parametrize("name,types,firm", [
    ("Courtesy Property Management", None, True), ("Isram Realty & Management", None, True),
    ("PMI Miami Coast", None, True), ("FCI Residential", None, True), ("Falke HOA", None, True),
    ("The Allen Morris Company", None, True), ("Ardent Miami", ["real_estate_agency"], True),
    ("Turnberry On the Green Condominium", None, False), ("Midtown Aventura", None, False),
    ("Parc Central East Leasing Office", None, False), ("Marbella Park Homeowners", None, False),
    ("Town Aventura Luxury Apartments", None, False), ("Mystic Pointe Tower 100", None, False),
    ("Avanti Way", ["condominium_complex"], False),
    ("Premier Association Management", ["condominium_complex"], False),      # Places says it is a building
    ("RealManage Coral Springs", ["service"], True), ("Condominium Associates", None, True),
    ("Privé Condominium Management", ["condominium_complex"], False),
    ("Lago Grande Homeowners Associates", None, False), ("Grove at Grand Bay Condominium Association", None, False),
])
def test_firm_or_building(name, types, firm):
    assert backfill.is_firm(name, types) is firm


# ── identity ────────────────────────────────────────────────────────────────

def _search_returns(monkeypatch, places):
    seen = []

    def search(q, tok, with_reviews, page_size=20):
        seen.append(q)
        return {"places": places}
    monkeypatch.setattr(gp, "_search", search)
    return seen


def test_identity_by_phone_skips_the_garage(monkeypatch):
    garage = _place("G", "Oceanside Plaza Parking Garage", "5555 Collins Ave, Miami Beach, FL 33140, USA",
                    "(305) 866-1301")
    garage["types"] = ["parking"]
    q = _search_returns(monkeypatch, [garage, PLACES[0]])
    found = backfill.identify(row(), "onsite")
    assert found.source_id == "A" and found.evidence["places_match"] == "phone"
    assert q == ["Oceanside Plaza, Miami-Dade Broward FL"]


def test_identity_by_site_domain_or_unique_name_only(monkeypatch):
    other = _place("X", "Oceanside Plaza", "1 Other St, Miami Beach, FL 33140, USA", "(305) 111-2222",
                   "https://www.oceansideplaza.org/contact")
    _search_returns(monkeypatch, [other])
    assert backfill.identify(row(phone="+13050000000"), "onsite").evidence["places_match"] == "website"
    twin = _place("Y", "Oceanside Plaza", "9 Twin St, Miami Beach, FL 33140, USA", "(305) 111-3333")
    _search_returns(monkeypatch, [{**other, "websiteUri": None}, twin])
    assert backfill.identify(row(phone="+13050000000", website_url=None), "onsite") is None     # two look-alikes
    _search_returns(monkeypatch, [twin])
    assert backfill.identify(row(phone="+13050000000", website_url=None), "onsite").source_id == "Y"
    _search_returns(monkeypatch, [_place("Z", "Blue Lagoon Tower", "1 A St, Miami, FL 33130, USA", "(305) 1112222")])
    assert backfill.identify(row(phone="+13050000000", website_url=None), "onsite") is None


def test_dbpr_name_lookup_needs_a_clear_unique_name(tmp_path):
    from test_pipeline_e2e import FIXTURE
    conn = dbpr.connect(tmp_path / "d.db")
    dbpr.load_csv(FIXTURE, conn)
    name = conn.execute("SELECT condo_name FROM dbpr_condos WHERE UPPER(county) IN ('DADE','MIAMI-DADE','BROWARD') "
                        "LIMIT 1").fetchone()[0]
    hit = backfill.dbpr_by_name(name, conn)
    assert hit and hit["condo_name"] == name
    assert backfill.dbpr_by_name("Totally Unknown Residences", conn) is None


# ── enrichment + payload ────────────────────────────────────────────────────

def test_building_gets_ficha_firm_zip_and_keeps_the_crm_phone(wired, monkeypatch):  # noqa: F811
    object.__setattr__(settings, "google_places_api_key", "k")
    _search_returns(monkeypatch, PLACES)
    lead = backfill.enrich_row(row(), use_ai=False, nearby=False)
    assert lead.evidence["lead_kind"] == "building" and lead.evidence["places_match"] == "phone"
    assert lead.phone == "+13058661301"                                 # the CRM line is never replaced
    p = backfill.payload_row(row(), lead, "enrich-t")
    assert set(p) <= ALLOWED and p["id"] == row()["id"]
    assert p["address"] == "5555 Collins Ave, Miami Beach, FL 33140" and p["management_company"] == "Castle Management LLC"
    hook, ficha = p["sales_strategy"].split("\nFicha: ")
    assert "Gran Comunidad" in ficha and "est. US$" in ficha and "score" in ficha and "Plan B:" in ficha
    assert "management +13058661300" in ficha or "+13058661300" in ficha    # the office line goes to the Ficha
    assert p["email"] == "manager@oceansideplaza.org" and p["maps_url"].startswith("https://maps.google.com")


def test_management_firm_is_its_own_company_with_portfolio(wired, monkeypatch):  # noqa: F811
    object.__setattr__(settings, "google_places_api_key", "k")
    firm = _place("F", "Castle Management", "12270 SW 3rd St, Plantation, FL 33325, USA", "(954) 792-6000",
                  "https://www.castlegroup.com/")
    firm["types"] = ["real_estate_agency", "point_of_interest"]
    _search_returns(monkeypatch, [firm])
    monkeypatch.setattr(pipeline.appraiser, "lookup", lambda a: pytest.fail("an office is not a property to wash"))
    seen = {}
    monkeypatch.setattr(dbpr, "cluster", lambda **kw: seen.update(kw) or {
        "by": "company", "key": "KEY:CASTLEMANAGEMENT", "count": 2, "total_units": 299,
        "siblings": [{"name": "Oceanside Plaza", "address": "5555 COLLINS AVE", "units": 299, "zip": "33140"}]})
    monkeypatch.setattr(pipeline, "crawl", lambda url, max_pages=5: parse_page(
        "<html><body><p>Laura Vega, Director of Operations</p></body></html>", url))
    r = row(name="Castle Management", phone="+19547926000", website_url="https://www.castlegroup.com/")
    lead = backfill.enrich_row(r, use_ai=False, nearby=False)
    assert lead.evidence["lead_kind"] == "management_firm" and lead.strategy_kind == "portfolio"
    p = backfill.payload_row(r, lead, "enrich-t")
    assert p["management_company"] == "Castle Management" and p["address"].endswith("FL 33325")
    ficha = p["sales_strategy"].split("\nFicha: ")[1]
    assert ficha.startswith("Administradora de propiedades") and "Gran Comunidad" not in ficha
    assert "administra 1 condominio registrado en DBPR en la zona (incl. Oceanside Plaza)" in ficha   # DBPR portfolio
    assert lead.properties_count == 1 and seen["company"] == "Castle Management"
    assert p["contact_name"] == "Laura Vega" and p["contact_title"] == "Director of Operations"


def test_unidentified_lead_is_not_sent(monkeypatch):
    object.__setattr__(settings, "google_places_api_key", "k")
    _search_returns(monkeypatch, [])
    r = row(name="Ghost Residences", phone="+13054279999", website_url=None)
    lead = backfill.enrich_row(r, use_ai=False, nearby=False)
    assert backfill.payload_row(r, lead, "enrich-t") is None


def test_firm_plan_b_never_sends_the_setter_to_a_concierge(monkeypatch):
    object.__setattr__(settings, "google_places_api_key", None)
    monkeypatch.setattr(pipeline, "crawl", lambda url, max_pages=5: parse_page("<html><body>About us</body></html>", url))
    monkeypatch.setattr(dbpr, "cluster", lambda **kw: None)
    r = row(name="Keystone Property Management", website_url="https://keystonepm.example/")
    lead = backfill.enrich_row(r, use_ai=False, nearby=False)
    assert "Director de Operaciones" in lead.brief() and "conserjería" not in lead.brief()


# ── run ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_crm(monkeypatch):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    state = type("CRM", (), {"posts": []})()
    rows = [row(), row(id="0b9a7f5e-1111-4c2a-9d1e-000000000002", position=2, name="Ghost Residences", phone="+13054279999",
                       website_url=None, status="new")]
    monkeypatch.setattr(backfill, "fetch_unenriched", lambda include_attempted=False: rows)

    def post(leads, batch_id, dry_run):
        state.posts.append((dry_run, [l["id"] for l in leads]))
        return {"ok": True, "dry_run": dry_run, "received": len(leads), "updated": len(leads), "unchanged": 0,
                "errors": 0, "not_found": [], "ambiguous": [], "rejected": [],
                "results": [{"index": i, "id": l["id"], "status": "updated",
                             "fields": sorted(set(l) - {"id", "phone", "name"})} for i, l in enumerate(leads)]}
    monkeypatch.setattr(backfill, "post_enrich", post)
    return state


def test_run_dry_by_default_then_commit(wired, fake_crm, monkeypatch):  # noqa: F811
    object.__setattr__(settings, "google_places_api_key", "k")
    _search_returns(monkeypatch, PLACES)
    costs.tracker.reset()
    rep = backfill.run(limit=5, use_ai=False, nearby=False, out_dir=wired / "o")
    assert fake_crm.posts == [(True, [row()["id"]])]                      # the unidentified lead is not sent
    assert rep["selected"] == 2 and rep["sent"] == 1 and rep["not_sent_unidentified"] == 1 and not rep["committed"]
    assert rep["crm"]["ficha"] == 1 and rep["crm"]["plan_b"] == 1 and rep["crm"]["address_zip"] == 1
    assert "total_usd" in rep["costs"]
    out = wired / "o" / rep["batch_id"]
    assert {"backfill.jsonl", "enrich_payload.json", "crm_dry_run.json", "backfill_report.json"} <= {
        p.name for p in out.iterdir()}
    rep = backfill.run(statuses=["papers_sent"], limit=5, commit=True, use_ai=False, nearby=False, out_dir=wired / "o")
    assert fake_crm.posts[-2:] == [(True, [row()["id"]]), (False, [row()["id"]])] and rep["committed"]


def test_cli_active_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(backfill, "run", lambda **kw: seen.update(kw) or {"ok": 1})
    from lead_engine.__main__ import main
    assert main(["backfill-crm", "--active", "--limit", "50", "--commit"]) == 0
    assert seen["statuses"][0] == "papers_sent" and seen["limit"] == 50 and seen["commit"]
    monkeypatch.setattr(backfill, "run", lambda **kw: (_ for _ in ()).throw(backfill.CrmError("HTTP 401")))
    assert main(["backfill-crm"]) == 1


def test_network_errors_surface_as_crm_errors(monkeypatch):
    object.__setattr__(settings, "crm_webhook_secret", "s")
    monkeypatch.setattr(backfill.requests, "post", lambda *a, **k: Resp(500, {"error": "enrich_failed"}))
    with pytest.raises(backfill.CrmError, match="500"):
        backfill.post_enrich([{"id": "x", "address": "1 A St, Miami, FL 33130"}], "b", True)
    assert requests                                                        # imported for monkeypatch target


@pytest.mark.parametrize("name,ok", [("Laura Vega", True), ("Ana M. Diaz", True), ("Important Links", False),
                                     ("Property Management Group", False), ("Contact Us", False), ("Owner", False),
                                     ("Keystone Property", False), ("Board Of Directors", False), ("john smith", False),
                                     ("Free Rental Analysis", False), ("Order Estoppel", False),
                                     ("Corporate Profiles", False)])
def test_contact_names_must_be_people(name, ok):
    assert backfill.plausible_person(name, "Keystone Property Management") is ok


def test_firm_hook_comes_from_facts_not_marketing():
    from lead_engine.models import Lead
    l = Lead(source="crm", source_id="crm:x", strategy_kind="portfolio", company_name="Keystone Property Management")
    l.portfolio_cluster = {"count": 21, "siblings": [{"name": "Space 01"}, {"name": "Flamingo Place"}]}
    assert backfill.firm_hook(l) == ("Administradora con 21 condominios registrados en DBPR en la zona (incl. Space 01 "
                                     "y Flamingo Place): pedir por el director de operaciones / regional manager y "
                                     "proponer un proveedor único de softwash con precio por portafolio y una prueba "
                                     "en Space 01.")


def test_commit_saved_sends_exactly_the_reviewed_payload(wired, fake_crm, monkeypatch):  # noqa: F811
    object.__setattr__(settings, "google_places_api_key", "k")
    _search_returns(monkeypatch, PLACES)
    rep = backfill.run(limit=5, use_ai=False, nearby=False, out_dir=wired / "o")
    out = wired / "o" / rep["batch_id"]
    monkeypatch.setattr(backfill, "enrich_row", lambda *a, **k: pytest.fail("nothing is recomputed"))
    sent = json.loads((out / "enrich_payload.json").read_text())["leads"]
    rep2 = backfill.commit_saved(out)
    assert fake_crm.posts[-2:] == [(True, [l["id"] for l in sent]), (False, [l["id"] for l in sent])]
    assert rep2["committed"] and (out / "crm_commit.json").exists() and rep2["costs"] == rep["costs"]


def test_units_are_stripped_from_building_addresses():
    assert backfill.building_address("800 Claughton Island Dr APT 603, Miami, FL 33131") == \
        "800 Claughton Island Dr, Miami, FL 33131"
    assert backfill.building_address("600 NE 36th St Apt C11, Miami, FL 33137") == "600 NE 36th St, Miami, FL 33137"
    assert backfill.building_address("1 Ocean Dr, Miami Beach, FL 33139") == "1 Ocean Dr, Miami Beach, FL 33139"


def test_a_realtor_line_printed_on_several_building_sites_is_dropped():
    from lead_engine.models import Lead
    agent = {"number": "+15612991145", "label": "leasing", "source": "tel_link", "context": ""}
    leads = []
    for i, name in enumerate(("Icon", "Grove at Grand Bay", "The Point")):
        l = Lead(source="crm", source_id=f"crm:{i}", strategy_kind="onsite", company_name=name,
                 phone=f"+1305695691{i}", email="inquiry@condomatch.com" if i < 2 else "pm@thepoint.org",
                 email_status="mx")
        l.phones = [{"number": l.phone, "label": "google_main", "source": "google_places", "context": ""}, dict(agent),
                    {"number": f"+1305111222{i}", "label": "management", "source": "text", "context": ""}]
        leads.append(l)
    rep = backfill.drop_shared_boilerplate(leads)
    assert rep["phones"] == [phone_key("+15612991145")] and "inquiry@condomatch.com" in rep["emails"]
    assert all("+15612991145" not in [p["number"] for p in l.phones] for l in leads)
    assert all(len(l.phones) == 2 for l in leads)                     # own lines survive
    assert [l.email for l in leads] == [None, None, "pm@thepoint.org"]


@pytest.mark.parametrize("raw,clean", [("Guillermo Cancio-", "Guillermo Cancio"), ("Ana Diaz ,", "Ana Diaz"),
                                       ("– Luis Paz:", "Luis Paz"), ("John Smith Jr.", "John Smith Jr."),
                                       ("  Mary   Ann  Lee • ", "Mary Ann Lee")])
def test_loose_punctuation_is_trimmed_from_names(raw, clean):
    from lead_engine.models import Lead, clean_person_name
    assert clean_person_name(raw) == clean
    l = Lead(source="crm", source_id="crm:x", strategy_kind="onsite", company_name="X")
    l.add_contact(raw, "President of the Board", "website")
    assert l.contacts[0]["name"] == clean


def test_firm_without_site_uses_the_verified_catalog(monkeypatch):
    object.__setattr__(settings, "google_places_api_key", "k")
    grs = _place("G1", "GRS Community Management", "3900 Woodlake Blvd, Lake Worth, FL 33463, USA", "(561) 641-8554",
                 "https://www.grsmgt.com/")
    grs["types"] = ["real_estate_agency"]
    _search_returns(monkeypatch, [grs])
    crawled = []
    monkeypatch.setattr(pipeline, "crawl", lambda url, max_pages=5: crawled.append(url) or parse_page(
        "<html><body>GRS</body></html>", url))
    monkeypatch.setattr(dbpr, "cluster", lambda **kw: None)
    r = row(name="GRS Community Management", phone="+13050000000", website_url=None)
    lead = backfill.enrich_row(r, use_ai=False, nearby=False)
    assert crawled == ["https://grsmgt.com/"] and lead.evidence["places_match"] == "website"
    p = backfill.payload_row(r, lead, "t")
    assert p["website_url"] == "https://grsmgt.com/" and p["address"].endswith("FL 33463")
    r2 = row(name="Unknown Firm Management", phone="+13050000000", website_url=None)
    _search_returns(monkeypatch, [])
    crawled.clear()
    assert backfill.payload_row(r2, backfill.enrich_row(r2, use_ai=False, nearby=False), "t") is None and not crawled


def test_out_of_state_headquarters_and_realtor_emails_are_not_sent():
    from lead_engine.models import Lead
    firm = Lead(source="crm", source_id="crm:x", strategy_kind="portfolio", company_name="The Management Trust",
                address="8485 W Sunset Rd #100, Las Vegas, NV 89113", website_url="https://managementtrust.com/",
                management_company="The Management Trust", management_type="third_party")
    firm.evidence["lead_kind"], firm.web_text = "management_firm", "site"
    p = backfill.payload_row(row(name="The Management Trust"), firm, "t")
    assert "address" not in p and p["website_url"] == "https://managementtrust.com/"
    tower = Lead(source="crm", source_id="crm:y", strategy_kind="onsite", company_name="Ponce Towers",
                 address="134 Madeira Ave, Miami, FL 33134", email="b@magiccityrealtors.com", email_status="mx")
    tower.evidence["lead_kind"], tower.web_text = "building", "site"
    p = backfill.payload_row(row(name="Ponce Towers"), tower, "t")
    assert "email" not in p and p["address"] == "134 Madeira Ave, Miami, FL 33134"
    tower.email = "board@poncetowers.org"
    assert backfill.payload_row(row(name="Ponce Towers"), tower, "t")["email"] == "board@poncetowers.org"
    assert not backfill.plausible_person("Financial Reporting")


def test_rebuild_reapplies_rules_without_calling_apis(wired, fake_crm, monkeypatch):  # noqa: F811
    object.__setattr__(settings, "google_places_api_key", "k")
    _search_returns(monkeypatch, PLACES)
    rep = backfill.run(limit=5, use_ai=False, nearby=False, out_dir=wired / "o")
    out = wired / "o" / rep["batch_id"]
    rows = [json.loads(l) for l in (out / "backfill.jsonl").read_text().splitlines()]
    rows[0]["lead"]["contacts"].append({"name": "Financial Reporting", "title": "Owner", "role": "exec",
                                        "source": "website"})
    (out / "backfill.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(backfill, "enrich_row", lambda *a, **k: pytest.fail("no API is called again"))
    monkeypatch.setattr(gp, "_search", lambda *a, **k: pytest.fail("no API is called again"))
    rep2 = backfill.rebuild_saved(out)
    sent = json.loads((out / "enrich_payload.json").read_text())["leads"]
    assert rep2["rebuilt"] and len(sent) == 1 and "Financial Reporting" not in json.dumps(sent)
    assert "\nFicha: " in sent[0]["sales_strategy"] and fake_crm.posts[-1][0] is True


def test_dropping_a_fake_plan_b_contact_restores_the_generic_one():
    from lead_engine.models import Lead
    l = Lead(source="crm", source_id="crm:n", strategy_kind="portfolio", company_name="Novel Management")
    l.evidence["lead_kind"] = "management_firm"
    l.add_contact("Financial Reporting", "Owner", "website")
    backfill.drop_implausible_contacts(l)
    assert l.contacts == [] and "Plan B: pedir por el Director de Operaciones" in l.brief()
