"""Overpass failover, cadastre surface inference, Broward plaza seed, preflight and the ads audience (no network)."""

import csv
import json

import pytest
import requests

from lead_engine import (ads_export, appraiser, broward, dbpr, geo, inspector_sheet as sheet, overpass, pdf_generator,
                         pipeline, preflight, tenant, valuation)
from lead_engine.collectors import appraiser_seed
from lead_engine.config import settings

from test_pipeline_e2e import FIXTURE, wired  # noqa: F401  (fixture)


class _Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.data


@pytest.fixture(autouse=True)
def _fresh_overpass(monkeypatch):
    overpass.reset()
    monkeypatch.delenv("OVERPASS_MIRRORS", raising=False)
    yield
    overpass.reset()


# ── 1a. Overpass mirrors ────────────────────────────────────────────────────

def test_failover_to_a_mirror_and_stick_to_it(monkeypatch):
    calls = []

    def post(url, data, headers, timeout):
        calls.append((url.split("/")[2], timeout))
        if "overpass-api.de" in url and "lz4" not in url:
            raise requests.ConnectionError("Connection reset by peer")
        return _Resp({"elements": [{"id": 1}]})
    monkeypatch.setattr(overpass.requests, "post", post)
    assert overpass.query("q") == [{"id": 1}]
    assert calls == [("overpass-api.de", (6.0, 45)), ("overpass.kumi.systems", (6.0, 15.0))]
    calls.clear()
    overpass.query("q2")
    assert calls == [("overpass.kumi.systems", (6.0, 45))]              # the mirror that answered goes first


def test_rate_limit_and_server_errors_move_on_and_a_dead_osm_is_switched_off(monkeypatch):
    monkeypatch.setenv("OVERPASS_MIRRORS", "https://m1.example/api/interpreter")
    seen = []

    def post(url, data, headers, timeout):
        seen.append(url)
        return _Resp({}, status=429 if "overpass-api.de" in url else 504)
    monkeypatch.setattr(overpass.requests, "post", post)
    for _ in range(overpass.MAX_ROUNDS):
        with pytest.raises(overpass.OverpassUnavailable):
            overpass.query("q")
    assert len(seen) == 2 * overpass.MAX_ROUNDS
    monkeypatch.setattr(overpass.requests, "post", lambda *a, **k: pytest.fail("disabled for the run"))
    with pytest.raises(overpass.OverpassUnavailable, match="desactivado"):
        overpass.query("q")
    assert geo.fetch_profile(25.8, -80.1) == {}                         # callers degrade to an empty profile


def test_mirror_list_is_configurable():
    assert overpass.endpoints()[1:] == list(overpass.DEFAULT_MIRRORS)
    import os
    os.environ["OVERPASS_MIRRORS"] = " https://a/api/interpreter ,https://b/api/interpreter"
    try:
        assert overpass.endpoints() == [settings.overpass_url, "https://a/api/interpreter", "https://b/api/interpreter"]
    finally:
        del os.environ["OVERPASS_MIRRORS"]


# ── 1b. cadastre / DBPR surface inference ──────────────────────────────────

def test_plaza_parking_from_the_cadastral_lot():
    inf = valuation.infer_surfaces({"vertical": "plaza", "building_sqft": 291551, "lot_sqft": 1291179, "levels": 1})
    assert inf == {"parking_lot": {"ft2": round((1291179 - 291551) * 0.5, -1), "source": "estimado por lote catastral"}}
    assert valuation.infer_surfaces({"vertical": "plaza", "building_sqft": 100000, "lot_sqft": 120000, "levels": 1}) == {}
    assert valuation.infer_surfaces({"vertical": "commercial", "building_sqft": 80000, "lot_sqft": None}) == {}


@pytest.mark.parametrize("b,keys", [
    ({"levels": 12, "units": 40}, {"parking_garage", "pool_deck"}),          # ≥6 floors
    ({"levels": 3, "units": 80}, {"parking_garage", "pool_deck"}),           # ≥75 units
    ({"levels": 2, "units": 20, "coastal": True}, {"parking_garage", "pool_deck"}),
    ({"levels": 3, "units": 40}, set()),                                      # small inland building
    ({"levels": 8}, {"pool_deck"}),                                           # no units → no garage size
])
def test_condo_scale_inference(b, keys):
    inf = valuation.infer_surfaces(b, None, "community")
    assert set(inf) == keys
    assert all(v["source"] == "est. por escala catastral — validar en sitio" for v in inf.values())
    if "parking_garage" in inf:
        assert inf["parking_garage"]["ft2"] == b["units"] * valuation.PARKING_FT2_PER_UNIT


def test_hotels_use_the_same_scale_rule_and_offices_do_not():
    assert set(valuation.infer_surfaces({"levels": 10, "units": 200}, None, "commercial", hotel=True)) == {
        "parking_garage", "pool_deck"}
    assert valuation.infer_surfaces({"levels": 10, "units": 200}, None, "commercial") == {}


def test_inferred_surfaces_enter_the_estimate_and_are_flagged_in_the_pm_pdf():
    b = {"levels": 12, "units": 151, "coastal": True}
    plain = valuation.estimate(b)
    b["inferred"] = valuation.infer_surfaces(b, None, "community")
    est = valuation.estimate(b)
    assert {"garage", "pool_deck"} <= set(est["components"]) and not {"garage", "pool_deck"} & set(plain["components"])
    assert est["basis"]["inferred"] == ["parking_garage", "pool_deck"]
    rows = pdf_generator.deal_rows({"estimated_deal": est}, "en")
    labels = [r[0] for r in rows]
    assert any(l.startswith("Parking garage") and l.endswith("(est., to be confirmed on site)") for l in labels)
    assert any("(est., to be confirmed on site)" in l for l in labels if "ool" in l)
    assert not any("(est." in l for l in labels if l.startswith("Facade"))


def test_field_sheet_never_misses_garage_or_pool_for_a_tower():
    rec = {"company_name": "The Summit", "address": "1201 S Ocean Dr, Hollywood, FL 33019",
           "building": {"levels": 25, "units": 375, "coastal": True}}      # no OSM amenities, no stored inference
    surf = sheet.surfaces(rec)
    assert surf["garage"]["inferred"] and surf["pool_deck"]["inferred"]
    assert "est. por escala catastral — validar en sitio" in surf["garage"]["label"]
    assert "Pool deck / áreas recreativas" in surf["pool_deck"]["label"]
    # an amenity confirmed by OSM/site is never duplicated by the inference
    confirmed = {**rec, "building": {**rec["building"], "amenities": {"pool_deck": "osm"}}}
    assert sheet.surfaces(confirmed)["pool_deck"]["label"] == "Pool deck"


def test_field_sheet_plaza_lot_uses_its_own_production_rate():
    rec = {"company_name": "Sheridan Plaza", "address": "4901 Sheridan St, Hollywood, FL 33021", "vertical": "plaza",
           "building": {"vertical": "plaza", "building_sqft": 291551, "lot_sqft": 1291179, "levels": 1}}
    surf = sheet.surfaces(rec)
    assert surf["lot"]["kind"] == "lot" and "estimado por lote catastral" in surf["lot"]["label"]
    row = next(r for r in sheet.operation(surf)["rows"] if r["key"] == "lot")
    assert row["crew_hours"] == round(surf["lot"]["ft2"] / sheet.FIELD_DEFAULTS["lot_ft2_h"], 1)


def test_pipeline_infers_surfaces_when_osm_gave_nothing(wired, monkeypatch):  # noqa: F811
    from lead_engine import config
    from lead_engine.collectors import dbpr_seed
    s = type(config.settings)(**{**config.settings.__dict__, "google_places_api_key": None, "crm_webhook_secret": None,
                                 "min_units": 100, "priority_units": 150})
    for mod in (pipeline, dbpr_seed):
        monkeypatch.setattr(mod, "settings", s)
    conn = dbpr.connect(wired / "dbpr.db")
    real_collect = dbpr_seed.collect
    monkeypatch.setattr(pipeline.dbpr_seed, "collect",
                        lambda strategy, n: real_collect(strategy, n, conn, cities=["Miami Beach", "Sunny Isles Beach"]))
    monkeypatch.setattr(pipeline.osm, "lookup_address", lambda address, name=None: {
        "lat": 25.9301, "lng": -80.1211, "phone": "+13059491000", "website": None, "osm_id": "way/1"}
        if address.startswith("16485") else None)
    monkeypatch.setattr(pipeline.geo, "fetch_profile", lambda lat, lng: {})            # OSM down for this run
    out = pipeline.run(strategy="onsite", limit=5, use_ai=False, use_apollo=False, precheck=False, use_search=False,
                       out_dir=wired / "o")
    lead = json.loads((out / "leads.jsonl").read_text().splitlines()[0])
    assert lead["company_name"] == "Oceania II"                          # 330 units, OSM profile empty here
    assert set(lead["building"]["inferred"]) == {"parking_garage", "pool_deck"}
    assert "parking_garage" not in (lead["building"].get("amenities") or {})   # never presented as observed
    assert lead["estimated_deal"]["basis"]["inferred"] == ["parking_garage", "pool_deck"]


# ── 2. Broward plaza seed ───────────────────────────────────────────────────

BCPA = [
    {"FOLIO_NUMBER": "514206084890", "NAME_LINE_1": "EQUITY ONE (SHERIDAN PLAZA) LLC", "NAME_LINE_2": "% REGENCY CENTERS",
     "SITUS_STREET_NUMBER": "4901", "SITUS_STREET_DIRECTION": None, "SITUS_STREET_NAME": "SHERIDAN",
     "SITUS_STREET_TYPE": "ST", "SITUS_ZIP_CODE": 33021, "BLDG_YEAR_BUILT": 1997, "BLDG_TOT_SQ_FOOTAGE": 291551,
     "USE_CODE": "16", "GIS_SQUARE_FOOT": 1291179, "JUST_BUILDING_VALUE": 30000000, "JUST_LAND_VALUE": 20000000,
     "ADDRESS_LINE_1": "1 INDEPENDENT DR", "CITY": "JACKSONVILLE", "ZIP": "32202"},
]


def test_broward_seed_record_and_where():
    tenant.use("mb_softwash")
    w = broward.seed_where("plaza", ["33021", "33009"], 20000)
    assert w == ("SITUS_ZIP_CODE IN (33009,33021) AND USE_CODE IN ('11','12','13','14','15','16') "
                 "AND BLDG_TOT_SQ_FOOTAGE >= 20000")
    r = broward.seed_record(BCPA[0])
    assert r["address"] == "4901 Sheridan St, Hollywood, FL 33021" and r["lot_sqft"] == 1291179
    assert r["care_of"] == "REGENCY CENTERS" and r["assessed_value"] == 50000000 and r["dor_code"] == "16"


def test_pa_seed_merges_miami_dade_and_the_broward_corridor(monkeypatch):
    tenant.use("mb_softwash")
    md_row = {"FOLIO": "3040", "TRUE_SITE_ADDR": "8888 SW 136 ST", "TRUE_SITE_CITY": "Miami", "TRUE_SITE_ZIP_CODE": "33176",
              "TRUE_OWNER1": "DADELAND STATION LLC", "BUILDING_ACTUAL_AREA": 150000, "DOR_CODE_CUR": "1600",
              "DOR_DESC": "COMMUNITY SHOPPING CENTER", "CONDO_FLAG": "N"}
    urls = []

    def get(url, params=None, timeout=None, headers=None, data=None):
        params = params or data
        urls.append(url)
        if url == appraiser.URL:
            return _Resp({"features": [{"attributes": md_row}]})
        assert "SITUS_ZIP_CODE IN (" in params["where"] and "33326" not in params["where"]
        return _Resp({"features": [{"attributes": BCPA[0]}, {"attributes": BCPA[0]}]})   # one folio, two polygons
    monkeypatch.setattr(appraiser_seed.requests, "post", get)
    monkeypatch.setattr(broward.requests, "get", get)
    leads = list(appraiser_seed.collect("onsite", 5, vertical="plaza", cities=["Miami", "Hollywood FL"]))
    assert [(l.source_id, l.cadastre.get("building_sqft")) for l in leads] == [("bcpa:514206084890", 291551),
                                                                             ("pa:3040", 150000)]
    b = leads[0]
    assert b.vertical == "plaza" and b.address == "4901 Sheridan St, Hollywood, FL 33021"
    assert b.cadastre["source"] == "broward_property_appraiser" and b.company_name.startswith("Equity One")

    def md_down(url, params=None, timeout=None, headers=None, data=None):
        if url == appraiser.URL:
            raise requests.ConnectionError("down")
        return get(url, params=params or data, timeout=timeout, headers=headers)
    monkeypatch.setattr(appraiser_seed.requests, "post", md_down)
    assert [l.source_id for l in appraiser_seed.collect("onsite", 5, vertical="plaza", cities=["Hollywood FL"])] == [
        "bcpa:514206084890"]                                             # Broward still seeds


def test_no_broward_query_without_corridor_cities(monkeypatch):
    tenant.use("mb_softwash")
    monkeypatch.setattr(appraiser_seed.requests, "post", lambda *a, **k: _Resp({"features": []}))
    monkeypatch.setattr(broward, "seed", lambda *a, **k: pytest.fail("no corridor city requested"))
    assert list(appraiser_seed.collect("onsite", 5, vertical="plaza", cities=["Doral"])) == []


# ── 3. preflight ────────────────────────────────────────────────────────────

def test_config_checks_report_missing_keys_and_tenant_fields(tmp_path):
    tenant.use("mb_softwash")
    object.__setattr__(settings, "google_places_api_key", None)
    object.__setattr__(settings, "anthropic_api_key", "sk-x")
    object.__setattr__(settings, "crm_webhook_secret", None)
    checks = {c.name: c for c in preflight.config_checks(env={})}
    assert checks["GOOGLE_PLACES_API_KEY"].status == preflight.PENDING
    assert checks["ANTHROPIC_API_KEY"].status == preflight.OK and "sk-x" not in checks["ANTHROPIC_API_KEY"].detail
    assert checks["CRM_WEBHOOK_SECRET"].status == preflight.PENDING
    assert checks["booking_url"].status == preflight.PENDING and checks["sitio web"].status == preflight.OK
    assert checks["documents_available"].status == preflight.PENDING
    assert checks["data/dbpr.db"].status == preflight.PENDING                    # isolated empty state dir


def test_registry_counts(tmp_path):
    conn = dbpr.connect(settings.state_db.with_name("dbpr.db"))
    dbpr.load_csv(FIXTURE, conn)
    rows = {c.name: c for c in preflight.registry_checks()}
    assert rows["padrón de condominios"].status == preflight.OK and rows["licencias CAM/CAB"].status == preflight.PENDING


def test_probes_ok_error_and_pending():
    assert preflight.run_probe("red", "x", lambda: "fine", "").status == preflight.OK
    bad = preflight.run_probe("red", "y", lambda: (_ for _ in ()).throw(requests.ConnectTimeout("slow")), "")
    assert bad.status == preflight.ERROR and "ConnectTimeout" in bad.detail
    assert preflight.run_probe("claves", "z", None, "sin clave").status == preflight.PENDING
    object.__setattr__(settings, "google_places_api_key", None)
    probes = {name: probe for _, name, probe, _ in preflight.network_probes()}
    assert probes["Google Places"] is None and probes["Catastro Broward (tax roll)"] is not None
    t = preflight.table([preflight.Check("red", "a", "OK", "x", 12), preflight.Check("claves", "b", "PENDIENTE", "y")])
    assert "[red]" in t and "(12 ms)" in t and t.endswith("OK 1 · PENDIENTE 1 · ERROR 0")


def test_preflight_cli_offline_exit_code(capsys):
    from lead_engine.__main__ import main
    assert main(["--tenant", "mb_softwash", "preflight", "--offline"]) == 0
    out = capsys.readouterr().out
    assert "[config]" in out and "[tenant]" in out and "[red]" not in out


# ── 4. ads audience ─────────────────────────────────────────────────────────

REC = {"company_name": "Oceanside Plaza Condo", "address": "5555 Collins Ave, Miami Beach, FL 33140-2559",
       "email": "Manager@OceansidePlaza.org", "email_status": "mx", "phone": "(305) 866-1300",
       "contact_name": "Ana María Díaz", "management_company": "Castle Management LLC",
       "estimated_deal": {"low": 37000, "high": 68000},
       "contacts": [{"name": "Ana María Díaz"}, {"name": "Carlos Ruiz", "phone": "+13059491000"}]}


def test_audience_rows_are_normalised_for_the_platforms():
    rows = ads_export.from_records([REC])
    assert rows[0] | {} == {**rows[0], "email": "manager@oceansideplaza.org", "phone": "+13058661300", "fn": "ana",
                            "ln": "diaz", "ct": "miamibeach", "st": "fl", "zip": "33140", "country": "us",
                            "company": "Castle Management LLC", "value": 52500}
    assert (rows[1]["fn"], rows[1]["ln"], rows[1]["phone"], rows[1]["email"]) == ("carlos", "ruiz", "+13059491000", "")
    assert ads_export.split_name("Dr. John Smith Jr.") == ("john", "smith") and ads_export.split_name("Admin") == ("", "")


def test_audience_csv_keeps_only_the_service_area_with_identifiers(tmp_path):
    tenant.use("mb_softwash")
    recs = [REC,
            {**REC, "company_name": "Bonaventure", "address": "16 Isla Bahia Dr, Fort Lauderdale, FL 33326",
             "email": "pm@bonaventure.org"},                                         # far-west Broward
            {**REC, "company_name": "Dead mail", "email": "x@nowhere.zz", "email_status": "nxdomain", "phone": None,
             "contact_name": None, "contacts": []},                                  # no identifier left
            {**REC, "contacts": []}]                                                 # duplicate of the first
    stats = ads_export.write_csv(ads_export.from_records(recs), tmp_path / "ads_audience.csv")
    rows = list(csv.DictReader(open(tmp_path / "ads_audience.csv")))
    assert list(rows[0]) == ads_export.COLUMNS
    assert [(r["email"], r["fn"]) for r in rows] == [("manager@oceansideplaza.org", "ana"), ("", "carlos")]
    assert stats == {"rows": 2, "outside_area": 2, "no_identifier": 1, "duplicates": 1, "with_email": 1,
                     "with_phone": 2, "by_source": {"batch": 2}}


def test_audience_from_the_registry_uses_contact_names_and_building_zip(tmp_path):
    object.__setattr__(settings, "target_cities", {"onsite": ["Miami", "Miami Beach"]})
    conn = dbpr.connect(tmp_path / "dbpr.db")
    dbpr.load_csv(FIXTURE, conn)
    rows = ads_export.from_registry(min_units=10, conn=conn, include_lodging=False, include_npi=False,
                                    include_firms=False)
    assert rows and all(r["fn"] and r["ln"] and r["zip"] and not r["email"] and not r["phone"] for r in rows)
    assert all(r["value"] for r in rows)
