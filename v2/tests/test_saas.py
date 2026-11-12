"""English PDF + QR, tenant profiles, free phone cascade, memory between runs and portfolio dossiers (no network)."""

import json
from pathlib import Path

import pytest

from lead_engine import cam, dbpr, lodging, pdf_common, pdf_generator, phone_resolver, portfolio_pdf, portfolios
from lead_engine import crm_importer, pipeline, tenant, valuation
from lead_engine.config import settings
from lead_engine.models import Lead
from lead_engine.state import Ledger

from test_pdf_route_intel import LEAD, _pages_and_text
from test_pipeline_e2e import CAM_FIXTURE, FIXTURE, wired  # noqa: F401  (fixture)

pypdf = pytest.importorskip("pypdf")


@pytest.fixture()
def company(monkeypatch):
    """Restore every company / tenant field and the valuation rates after the test."""
    keys = ("company_name", "company_phone", "company_email", "company_website", "company_booking_url",
            "documents_available", "brand_color", "pdf_lang", "target_cities", "min_units", "pvp_tiers",
            "allow_mgmt_firm_phone", "mgmt_phones", "tenant")
    saved = {k: getattr(settings, k) for k in keys}
    rates = dict(valuation.RATES)
    monkeypatch.delenv("INSPECTORS", raising=False)
    yield settings
    for k, v in saved.items():
        object.__setattr__(settings, k, v)
    valuation.RATES.clear()
    valuation.RATES.update(rates)


def _set(**kw):
    for k, v in kw.items():
        object.__setattr__(settings, k, v)


# ── English PDF + QR ────────────────────────────────────────────────────────

def test_english_pdf_uses_florida_terminology_and_a_qr_to_the_booking_url(tmp_path, company, monkeypatch):
    _set(company_name="Acme Softwash", company_booking_url="https://acme.example/book", company_website=None,
         company_phone=None, documents_available=["COI", "W-9"], pdf_lang="en")
    seen = []
    real = pdf_common.qr_drawing
    monkeypatch.setattr(pdf_common, "qr_drawing", lambda v, s: seen.append(v) or real(v, s))
    n, text = _pages_and_text(pdf_generator.build(LEAD, tmp_path / "en.pdf"))
    assert n == 2
    for s in ("PRELIMINARY EXTERIOR MAINTENANCE", "Oceanside Plaza Condo", "Milestone", "SB 4-D", "1967",
              "310,037 ft", "$116,528,282", "$37,000 – $68,000", "Softwash", "elastomeric", "Rope access",
              "Certificate of Insurance (COI) and W-9 available upon request", "Scan to book",
              "https://acme.example/book", "Page 2 of 2", "Florida-licensed engineer"):
        assert s in text, s
    assert "2 months ago" in text and "hace 2 meses" not in text
    assert "US$" not in text and "Página" not in text and "Construido" not in text and "Permiso" not in text
    assert seen == ["https://acme.example/book"]                    # only on page 2


def test_qr_target_falls_back_to_website_then_phone_and_documents_are_never_invented(company):
    _set(company_booking_url=None, company_website="https://acme.example", company_phone="(305) 555-0100",
         documents_available=[])
    assert pdf_common.qr_target() == "https://acme.example"
    _set(company_website=None)
    assert pdf_common.qr_target() == "tel:3055550100"
    _set(company_phone=None)
    assert pdf_common.qr_target() is None
    assert pdf_generator.documents_line("en") is None
    assert pdf_common.qr_drawing("https://acme.example", 72)._qr_value == "https://acme.example"


def test_pdf_without_any_contact_target_has_no_qr(tmp_path, company, monkeypatch):
    _set(company_booking_url=None, company_website=None, company_phone=None)
    monkeypatch.setattr(pdf_common, "qr_drawing", lambda v, s: pytest.fail("no QR expected"))
    n, text = _pages_and_text(pdf_generator.build(LEAD, tmp_path / "n.pdf", lang="en"))
    assert n == 2 and "Scan to book" not in text


# ── tenants ─────────────────────────────────────────────────────────────────

def test_mb_softwash_profile_applies_branding_rates_and_cities(company):
    data = tenant.use("mb_softwash")
    assert settings.company_name == "MB Softwash Miami" and settings.tenant == "mb_softwash"
    assert settings.company_website == "https://mbsoftwashmiami.com" and settings.company_booking_url is None
    assert settings.pdf_lang == "en" and settings.documents_available == []
    assert valuation.RATES["facade"] == tuple(data["valuation_rates"]["facade"])
    from lead_engine.config import cities_for
    assert cities_for("onsite") == data["target_cities"]["onsite"]


def test_tenant_rejects_unknown_keys_bad_rates_and_bad_slugs(tmp_path, company, monkeypatch):
    monkeypatch.setattr(tenant, "TENANTS_DIR", tmp_path)
    (tmp_path / "bad.json").write_text(json.dumps({"company": {"name": "X"}, "colour": "red"}))
    with pytest.raises(tenant.TenantError, match="colour"):
        tenant.load("bad")
    (tmp_path / "rates.json").write_text(json.dumps({"valuation_rates": {"rocket": [1, 2, 3]}}))
    with pytest.raises(tenant.TenantError, match="rocket"):
        tenant.use("rates")
    with pytest.raises(tenant.TenantError):
        tenant.path_for("../etc/passwd")
    with pytest.raises(tenant.TenantError):
        tenant.load("missing")
    assert tenant.use(None) is None


# ── phone cascade ───────────────────────────────────────────────────────────

def _lodging(tmp_path, rows):
    conn = dbpr.connect(tmp_path / "dbpr.db")
    lodging.ensure(conn)
    for r in rows:
        conn.execute("INSERT INTO dbpr_lodging VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            r["lic"], r["rank"], r["licensee"], r.get("business"), None, r["address"], dbpr.address_key(r["address"]),
            "MIAMI BEACH", "33140", "Dade", r["phone"], r.get("units", 10), r.get("status", "20")))
    conn.commit()
    return conn


def test_lodging_phone_only_for_operators_or_the_association_itself(tmp_path):
    addr = "5555 Collins Ave, Miami Beach, FL 33140"
    conn = _lodging(tmp_path, [
        {"lic": "1", "rank": "CNDO", "licensee": "JOHN SMITH", "address": addr, "phone": "+13055550001"},
        {"lic": "2", "rank": "CNDO", "licensee": "OCEANSIDE PLAZA CONDOMINIUM ASSN INC", "address": addr,
         "phone": "+13055550002"},
    ])
    hit = lodging.building_phone(addr, ["Oceanside Plaza Condo"], conn)
    assert hit["phone"] == "+13055550002" and hit["label"] == "hoa"
    assert lodging.building_phone(addr, ["Some Other Tower"], conn) is None      # owner licences never used
    conn.execute("INSERT INTO dbpr_lodging VALUES ('3','HOTL','SEA HOTEL LLC','Sea Hotel',NULL,?,?,'MIAMI BEACH',"
                 "'33140','Dade','+13055550003',120,'20')", (addr, dbpr.address_key(addr)))
    conn.commit()
    assert lodging.building_phone(addr, ["x"], conn)["label"] == "front_desk"


def test_firm_phone_prefers_south_florida_and_tenant_override(company, monkeypatch):
    phones = [{"number": "+18553335149", "context": "Call us"},
              {"number": "+13125550000", "context": "Chicago office"},
              {"number": "+19549262921", "context": "Fort Lauderdale office"},
              {"number": "+13055559999", "label": "fax", "context": "Fax"}]
    assert phone_resolver.pick_firm_phone(phones)["number"] == "+19549262921"
    _set(mgmt_phones={"Castle Group": "(954) 926-2921"})
    assert phone_resolver.firm_phone("CASTLE GROUP")["number"] == "+19549262921"


def _seed_lead():
    lead = Lead(source="dbpr", source_id="dbpr:1", strategy_kind="portfolio", company_name="Blue Tower",
                address="100 Ocean Dr, Miami Beach, FL 33139")
    lead.management_company = "FirstService Residential"
    return lead


def test_firm_central_phone_needs_explicit_opt_in_and_is_labelled(company, monkeypatch):
    monkeypatch.setattr(phone_resolver.lodging, "building_phone", lambda a, n, **k: None)
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: {"number": "+19549262921", "source": "firm_website",
                                                                    "firm": "FirstService Residential",
                                                                    "url": "https://fsresidential.com"})
    _set(allow_mgmt_firm_phone=False)
    lead = _seed_lead()
    assert phone_resolver.resolve(lead) is None and lead.phone is None
    _set(allow_mgmt_firm_phone=True)
    assert phone_resolver.resolve(lead) == "firm_central"
    assert lead.phone == "+19549262921" and lead.phones[-1]["label"] == "firm_central"
    assert lead.evidence["phone_from"] == "firm_central"
    from lead_engine.models import PHONE_LABEL
    assert PHONE_LABEL["firm_central"] == "central administradora"


# ── memory between runs ─────────────────────────────────────────────────────

def test_ledger_memory_by_address_and_source_never_downgrades(tmp_path):
    led = Ledger(tmp_path / "s.db")
    a = Lead(source="dbpr", source_id="dbpr:1", strategy_kind="onsite", company_name="A",
             address="5555 Collins Ave, Miami Beach, FL 33140")
    same_building = Lead(source="places", source_id="place:XYZ", strategy_kind="onsite", company_name="Oceanside",
                         address="5555 Collins Avenue, Miami Beach, FL 33140")
    assert not led.should_skip(a)
    led.mark_seen(a, "imported", "b1")
    led.mark_seen(a, "rejected", "b2")                        # never downgrades
    assert led.seen_status(a) == "imported"
    assert led.should_skip(same_building)                     # same tower from another source
    assert not led.should_skip(same_building, ignore_seen=True)
    b = Lead(source="dbpr", source_id="dbpr:2", strategy_kind="onsite", company_name="B",
             address="1 Main St, Miami, FL 33101")
    led.mark_seen(b, "needs_phone", "b1")
    assert led.should_skip(b) and not led.should_skip(b, retry_needs_phone=True)
    assert led.seen_counts() == {"imported": 1, "needs_phone": 1}


def _seed_wiring(wired, monkeypatch, secret=None):
    from lead_engine import config
    from lead_engine.collectors import dbpr_seed
    s = type(config.settings)(**{**config.settings.__dict__, "google_places_api_key": None, "crm_webhook_secret": secret,
                                 "min_units": 100, "priority_units": 150})
    for mod in (pipeline, dbpr_seed, crm_importer):
        monkeypatch.setattr(mod, "settings", s)
    conn = dbpr.connect(wired / "dbpr.db")
    real_collect = dbpr_seed.collect
    monkeypatch.setattr(pipeline.dbpr_seed, "collect",
                        lambda strategy, n: real_collect(strategy, n, conn, cities=["Miami Beach", "Sunny Isles Beach"]))
    monkeypatch.setattr(pipeline.osm, "lookup_address", lambda address, name=None: {
        "lat": 25.93, "lng": -80.121, "phone": "+13059491000", "website": None, "name": "Oceania II",
        "levels": "28", "osm_id": "way/1"} if address.startswith("16485") else None)
    return dict(strategy="onsite", limit=5, use_ai=False, use_apollo=False, precheck=False, use_search=False)


def _names(out):
    return [json.loads(l)["company_name"] for l in (out / "leads.jsonl").read_text().splitlines()]


def test_dry_runs_never_consume_buildings(wired, monkeypatch):  # noqa: F811
    kw = _seed_wiring(wired, monkeypatch)
    assert _names(pipeline.run(**kw, out_dir=wired / "o1")) == ["Oceania II"]
    assert _names(pipeline.run(**kw, out_dir=wired / "o2", send=True)) == ["Oceania II"]     # dry-run to the CRM
    assert pipeline.Ledger().seen_counts() == {}                                            # nothing recorded


def test_a_committed_batch_is_consumed_only_after_the_crm_confirms(wired, monkeypatch):  # noqa: F811
    kw = _seed_wiring(wired, monkeypatch, secret="s")
    calls = []

    def ok(payloads, batch_id, dry_run, client, **k):
        calls.append(dry_run)
        return crm_importer.ImportReport(batch_id=batch_id, dry_run=dry_run, sent=len(payloads), inserted=len(payloads))
    monkeypatch.setattr(crm_importer, "import_leads", ok)
    first = pipeline.run(**kw, out_dir=wired / "o1", send=True, commit=True)
    assert _names(first) == ["Oceania II"] and calls == [False]
    second = pipeline.run(**kw, out_dir=wired / "o2", send=True, commit=True)
    assert _names(second) == []
    assert json.loads((second / "quality_report.json").read_text())["skipped_seen_in_earlier_runs"] >= 1
    assert pipeline.Ledger().seen_counts()["imported"] == 1
    third = pipeline.run(**kw, out_dir=wired / "o3", ignore_seen=True)    # comes back, but its phone is in the CRM
    assert _names(third) == [] and "teléfono repetido" in (third / "rejected.jsonl").read_text()


def test_crm_outage_queues_the_batch_and_the_next_start_resends_it(wired, monkeypatch):  # noqa: F811
    kw = _seed_wiring(wired, monkeypatch, secret="s")

    def down(*a, **k):
        raise crm_importer.CrmError("CRM no respondió: HTTP 503")
    monkeypatch.setattr(crm_importer, "import_leads", down)
    out = pipeline.run(**kw, out_dir=wired / "o1", send=True, commit=True)
    q = json.loads((out / "quality_report.json").read_text())
    assert q["crm"]["pending_resend"] and "503" in q["crm"]["error"]
    assert [p["batch_id"] for p in pipeline.Ledger().pending()] == [out.name]
    assert _names(pipeline.run(**kw, out_dir=wired / "o2")) == ["Oceania II"]     # not consumed while pending
    assert pipeline.resend_pending()["still_pending"][0]["attempts"] == 2            # still down
    got = []

    def ok(payloads, batch_id, dry_run, client, **k):
        got.append((batch_id, dry_run, [p["company_name"] for p in payloads]))
        return crm_importer.ImportReport(batch_id=batch_id, dry_run=dry_run, sent=1, inserted=1)
    monkeypatch.setattr(crm_importer, "import_leads", ok)
    res = pipeline.resend_pending()
    assert res["resent"] == [{"batch_id": out.name, "inserted": 1, "duplicates": 0}]
    assert got == [(out.name, False, ["Oceania II"])] and pipeline.Ledger().pending() == []
    assert (out / "crm_response_commit.json").exists()
    assert _names(pipeline.run(**kw, out_dir=wired / "o3", send=True, commit=True)) == []   # now consumed


def test_old_processed_rows_no_longer_block_a_building(tmp_path):
    led = Ledger(tmp_path / "s.db")
    lead = Lead(source="dbpr", source_id="dbpr:9", strategy_kind="onsite", company_name="X",
                address="1 Ocean Dr, Miami Beach, FL 33139")
    led.mark_seen(lead, "processed", "old")
    assert not led.should_skip(lead)
    led.mark_seen(lead, "imported", "b")
    assert led.should_skip(lead)


# ── portfolios ──────────────────────────────────────────────────────────────

@pytest.fixture()
def registry(tmp_path):
    conn = dbpr.connect(tmp_path / "dbpr.db")
    dbpr.load_csv(FIXTURE, conn)
    cam.load_csv(CAM_FIXTURE, conn)
    dbpr.index_routes(conn)
    return conn


def test_ranking_groups_by_licensed_firm_and_sums_estimates(registry):
    firms = portfolios.rank(registry)
    top = firms[0]
    assert top["firm"] == "Castle Management LLC" and top["license"] == "CAB3838"
    assert top["buildings"] == 1 and top["units"] == 299 and top["cities"] == ["Miami Beach"]
    assert top["total_estimated_usd"] > top["total_estimated_low"] > 0
    assert [f["total_estimated_usd"] for f in firms] == sorted((f["total_estimated_usd"] for f in firms), reverse=True)
    assert any(f["firm_id"].startswith("KEY:") and f["license"] is None for f in firms)   # unlicensed name key


def test_live_analysis_counts_milestone_and_records(registry, monkeypatch):
    firm = portfolios.rank(registry)[0]
    monkeypatch.setattr(portfolios.appraiser, "lookup", lambda a: {"year_built": 1967, "floors": 17})
    monkeypatch.setattr(portfolios.appraiser, "milestone_signal", lambda *a, **k: {
        "category": "milestone", "weight": 6.0, "label": "x",
        "data": {"year_built": 1967, "age": 59, "milestone": 60, "milestone_year": 2027, "coastal": True, "condo": True}})
    monkeypatch.setattr(portfolios.municipal_signals, "lookup", lambda a: [])
    portfolios.analyze_live(firm, depth=5)
    assert (firm["analyzed"], firm["milestone_window"], firm["municipal_records"]) == (1, 1, 0)
    assert firm["urgent"][0]["year_built"] == 1967


def test_portfolio_run_exports_csv_json_and_two_page_dossier(registry, tmp_path, company, monkeypatch):
    _set(company_name="Acme Softwash", company_booking_url="https://acme.example/book", pvp_tiers=[[3, 5], [10, 8]])
    monkeypatch.setattr(portfolios.appraiser, "lookup", lambda a: None)
    monkeypatch.setattr(portfolios.municipal_signals, "lookup", lambda a: [])
    out = portfolios.run(top=2, depth=3, pdf=True, lang="en", out_dir=tmp_path / "out", conn=registry)
    rows = (out / "ranking.csv").read_text().splitlines()
    assert rows[0].startswith("rank,firm,license") and "Castle Management LLC" in rows[1] and len(rows) == 3
    data = json.loads((out / "ranking.json").read_text())
    assert data[0]["building_list"][0]["name"]
    n, text = _pages_and_text(out / "pdf" / "Castle_Management_LLC.pdf")
    assert n == 2
    for s in ("PORTFOLIO DOSSIER", "Castle Management LLC", "CAB3838", "Preferred Vendor Program",
              "5% off for 3+ buildings; 8% off for 10+ buildings", "Most urgent properties", "Suggested pilot",
              "Scan to book a call", "Page 2 of 2"):
        assert s in text, s
    es = portfolio_pdf.build(data[0], tmp_path / "es.pdf", lang="es")
    n, text = _pages_and_text(es)
    assert n == 2 and "Programa de Proveedor Preferente" in text and "Página 2 de 2" in text


def test_display_names_and_catalog_merge_of_registry_spellings(registry, monkeypatch):
    assert portfolios.display("Firstservice Residential") == "FirstService Residential"
    assert portfolios.display("Castle Management LLC") == "Castle Management LLC"
    assert portfolios.display("Grs Community Management") == "GRS Community Management"
    # Pretend the catalog knows "Ritz Mgmt" as a brand of the licensed Castle firm: one firm, licence kept.
    monkeypatch.setattr(portfolios.mgmt_domains, "from_catalog", lambda n: ("Castle Group", "castlegroup.com")
                        if n and ("Castle" in n or "Ritz" in n) else None)
    firms = portfolios.rank(registry)
    castle = next(f for f in firms if f["license"] == "CAB3838")
    assert castle["firm"] == "Castle Group" and castle["buildings"] == 2 and castle["units"] == 299 + 14
    assert not any(f["firm_id"] == "KEY:RITZMANAGEMENT" for f in firms)


def test_pvp_tiers_env_format_and_cli_flags():
    from lead_engine.__main__ import main
    from lead_engine.config import _tiers
    assert _tiers("3:5, 10:8") == [[3, 5], [10, 8]] and _tiers(None) == []
    for argv in (["portfolios", "--help"], ["run", "--help"], ["pdf", "--help"]):
        with pytest.raises(SystemExit) as e:
            main(argv)
        assert e.value.code == 0


def test_single_unit_rental_licences_never_give_the_building_phone(tmp_path):
    """Regression (live smoke test, The Tides 3901 S Ocean Dr): a TAPT licence for one apartment
    ("GURSHUMOV 10D LLC", 1 unit) was taken as the tower's front desk."""
    addr = "3901 S Ocean Dr, Hollywood, FL 33019"
    conn = _lodging(tmp_path, [
        {"lic": "TAP1622174", "rank": "TAPT", "licensee": "GURSHUMOV 10D LLC", "business": "GURSHUMOV 10D LLC",
         "address": addr, "phone": "+13055011111", "units": 1},
        {"lic": "TAP2", "rank": "TAPT", "licensee": "BEACH RENTALS LLC", "address": addr, "phone": "+13055011112",
         "units": 12},                                       # 12 of 722 units: a rental company, not the building
    ])
    assert lodging.building_phone(addr, ["The Tides"], conn, building_units=722) is None
    m = lodging.match(addr, conn, building_units=722)
    assert "phone" not in m and m["unit_licences_skipped"] == 2
    # a real operator of the property still counts (hotel or the apart-hotel running most of the tower)
    conn.execute("INSERT INTO dbpr_lodging VALUES ('TAP3','TAPT','TIDES HOTEL MGMT LLC','The Tides',NULL,?,?,"
                 "'HOLLYWOOD','33019','Broward','+13055011113',400,'20')", (addr, dbpr.address_key(addr)))
    conn.commit()
    assert lodging.building_phone(addr, ["x"], conn, building_units=722)["phone"] == "+13055011113"
    assert lodging.match(addr, conn, building_units=722)["phone"] == "+13055011113"


@pytest.mark.parametrize("rank,units,building,ok", [
    ("TAPT", 1, None, False), ("TAPT", 4, None, False), ("HOTL", 3, None, False), ("NAPT", 5, None, True),
    ("HOTL", 120, None, True), ("TAPT", 30, 722, False), ("TAPT", 80, 722, True), ("TAPT", 8, None, True),
    ("CNDO", 200, None, False),
])
def test_is_building_licence(rank, units, building, ok):
    assert lodging.is_building_licence({"rank": rank, "units": units}, building) is ok
