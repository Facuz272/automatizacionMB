"""Commercial operators, Fort Lauderdale permits / BSIP, registry Ads audience and the HTML batch panel (no network)."""

import csv
import json
import re
from datetime import date

import pytest

from lead_engine import (ads_export, batch_report, commercial_mgmt as cm, dbpr, lodging, municipal_signals as ms,
                         outreach_pack, phone_resolver, pipeline, portfolio_consolidation as pc)
from lead_engine.config import settings
from lead_engine.models import Lead
from lead_engine.pdf_i18n import municipal_line

from test_pdf_route_intel import LEAD
from test_pipeline_e2e import FIXTURE, wired  # noqa: F401  (fixture)


# ── 1. commercial operators ─────────────────────────────────────────────────

def test_catalog_is_well_formed_and_only_florida_numbers_are_dialled():
    ops = cm.catalog()
    assert len(ops) >= 10 and all(op["domain"] and "." in op["domain"] for op in ops)
    for op in ops:
        assert op["phone"] is None or re.fullmatch(r"\+1\d{10}", op["phone"])
        assert op["phone_scope"] in (None, "florida", "corporate")
        assert (cm.dial_phone(op) is not None) == (op["phone_scope"] == "florida" and bool(op["phone"]))
    assert cm.dial_phone(next(o for o in ops if o["name"] == "Thor Equities")) is None      # NY headquarters


@pytest.mark.parametrize("owner,care,mail,expected", [
    ("RK CAUSEWAY PLAZA LLC", None, "17100 COLLINS AVE STE 225, SUNNY ISLES BEACH, FL 33160", "RK Centers"),
    ("CCB ASSOCIATES 39 LLC", None, "7978 COOPER CREEK BLVD, UNIVERSITY PARK, FL 34201", "Benderson Development"),
    ("SFA ATLANTIS ASSOC LP", None, "500 N BROADWAY #201, JERICHO, NY 11753", "Kimco Realty"),
    ("EQUITY ONE (SHERIDAN PLAZA) LLC", "REGENCY CENTERS", None, "Regency Centers"),
    ("PARADISE PLAZA ASSOCIATES LLC", None, "3841 NE 2 AVE, MIAMI, FL 33137", "Dacra"),
    ("SMITH FAMILY TRUST", None, "123 MAIN ST, MIAMI, FL 33101", None),
])
def test_operator_from_owner_care_of_or_mailing(owner, care, mail, expected):
    op = cm.match_record(owner, care, mail)
    assert (op["name"] if op else None) == expected


def _plaza(i, mailing, owner="X PLAZA LLC", sqft=100000, lot=400000):
    l = Lead(source="pa", source_id=f"pa:{i}", strategy_kind="onsite", company_name=f"Plaza {i}",
             address=f"{i}00 Collins Ave, Sunny Isles Beach, FL 33160")
    l.vertical = "plaza"
    l.cadastre = {"kind": "property", "owner_name": owner, "owner_mailing": mailing, "building_sqft": sqft,
                  "lot_sqft": lot, "source": "miami_dade_property_appraiser"}
    return l


RK_MAIL = "17100 COLLINS AVE STE 225, SUNNY ISLES BEACH, FL 33160"


def test_operator_phone_enters_the_cascade_only_when_allowed(monkeypatch):
    monkeypatch.setattr(phone_resolver.lodging, "building_phone", lambda a, n, **k: None)
    object.__setattr__(settings, "allow_mgmt_firm_phone", False)
    lead = _plaza(1, RK_MAIL)
    assert phone_resolver.resolve(lead) is None and lead.phone is None
    object.__setattr__(settings, "allow_mgmt_firm_phone", True)
    assert phone_resolver.resolve(lead, steps=("firm_central",)) == "firm_central"
    assert lead.phone == "+13059494110" and lead.management_company == "RK Centers"
    assert lead.phones[-1]["label"] == "firm_central" and lead.phones[-1]["source"] == "operator_site:rkcenters.com"
    corporate = _plaza(2, "600 FIFTH AVE 9TH FLR, NEW YORK, NY 10020", owner="THOR GALLERY LLC")
    assert phone_resolver.resolve(corporate, steps=("firm_central",)) is None
    assert corporate.phone is None and corporate.management_company == "Thor Equities"    # named, never dialled


def test_plazas_of_one_operator_are_consolidated_on_the_biggest(wired, monkeypatch):  # noqa: F811
    from lead_engine import config
    s = type(config.settings)(**{**config.settings.__dict__, "google_places_api_key": None, "crm_webhook_secret": None,
                                 "allow_mgmt_firm_phone": True})
    monkeypatch.setattr(pipeline, "settings", s)
    object.__setattr__(settings, "allow_mgmt_firm_phone", True)
    plazas = [_plaza(1, RK_MAIL, sqft=80000, lot=300000), _plaza(2, RK_MAIL, sqft=250000, lot=900000),
              _plaza(3, RK_MAIL, sqft=40000, lot=150000)]
    monkeypatch.setattr(pipeline, "_candidates", lambda *a, **k: iter(plazas))
    monkeypatch.setattr(pipeline.osm, "lookup_address", lambda address, name=None: None)
    monkeypatch.setattr(phone_resolver.lodging, "building_phone", lambda a, n, **k: None)
    monkeypatch.setattr(pc.municipal_signals, "lookup", lambda a, **k: [])
    out = pipeline.run(strategy="onsite", limit=5, sources=["pa"], use_ai=False, use_apollo=False, precheck=False,
                       use_search=False, out_dir=wired / "o")
    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text().splitlines()]
    assert [l["company_name"] for l in leads] == ["Plaza 2"] and leads[0]["phone"] == "+13059494110"
    assert [p["name"] for p in leads[0]["portfolio_siblings"]] == ["Plaza 1", "Plaza 3"]
    assert all(p["est_high"] > 0 for p in leads[0]["portfolio_siblings"])           # tickets from built area + lot
    payload = json.loads((out / "crm_payload.json").read_text())["leads"][0]
    assert payload["sales_strategy"].startswith("Teléfono = central de RK Centers")


# ── 2. Fort Lauderdale permits / Building Safety Inspection ────────────────

TODAY = date(2026, 11, 9)
MS = 1763942400000            # 2025-11-24


def _ftl(desc, status="In Process", addr="3900  GALT OCEAN DR", when=MS, pid="BLD-BSIP-251"):
    return {"PERMITID": pid, "PERMITDESC": desc, "PERMITSTAT": status, "SUBMITDT": when, "FULLADDR": addr}


def test_open_bsip_is_a_recertification_signal():
    s = ms.classify_ftl_permit(_ftl("Building Safety Inspection Program Application (BSIP)"), TODAY)
    assert s["category"] == "municipal_recert" and s["weight"] == 6.0 and s["data"]["program"] == "bsip"
    assert "3900 GALT OCEAN DR" in s["quote"] and s["date"] == "2025-11-24"
    label, text = municipal_line(s, "en")
    assert label == "Fort Lauderdale Building Safety Inspection (40/50-year) in progress"
    assert "application filed 2025-11-24 (status: In Process)" in text
    h = outreach_pack.hook({"municipal": [s]})
    assert h["kind"] == "recert" and "Building Safety Inspection" in h["en"] and "BSIP" in h["es"]


@pytest.mark.parametrize("rec", [
    _ftl("Building Safety Inspection Program Application (BSIP)", status="Complete"),
    _ftl("Building Safety Inspection Program Application (BSIP)", status="Void"),
    _ftl("Building Safety Inspection Program Application (BSIP)", when=1577836800000),       # 2020: too old
    _ftl("Structural Permit", addr="3000 HOLIDAY DR #1406", pid="BLD-RALT-250"),             # one unit
    _ftl("Structural Permit", addr="2414 E SUNRISE BLVD", pid="BLD-TENT-251"),               # a tent
    _ftl("Plumbing Permit", addr="2414 E SUNRISE BLVD", pid="PLB-1"),
])
def test_records_that_are_not_signals(rec):
    assert ms.classify_ftl_permit(rec, TODAY) is None


def test_building_level_exterior_permit():
    s = ms.classify_ftl_permit(_ftl("Walk-Thru - Re-Roof", status="Issued", addr="3700 GALT OCEAN DR",
                                    pid="BLD-RRF-25"), TODAY)
    assert s["category"] == "municipal_permit" and s["data"]["work"] == "Walk-Thru - Re-Roof"


def test_ftl_layer_is_fast_and_only_for_fort_lauderdale(monkeypatch):
    layer = next(l for l in ms.LAYERS if l.key == "ftl_permits")
    assert layer.timeout == 5.0 and layer.county == "broward" and layer.city == "FORT LAUDERDALE"
    calls = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"features": [{"attributes": _ftl("Building Safety Inspection Program Application (BSIP)",
                                                     when=int(1000 * 1762000000))}]}
    monkeypatch.setattr(ms.requests, "get", lambda url, params, timeout, headers: calls.append(timeout) or R())
    got = ms.lookup("3900 Galt Ocean Dr, Fort Lauderdale, FL 33308", layers=[layer])
    assert calls == [5.0] and got and got[0]["category"] == "municipal_recert"
    assert ms.lookup("1201 S Ocean Dr, Hollywood, FL 33019", layers=[layer]) == []           # other city: not queried
    assert calls == [5.0]


# ── 3. registry audience: lodging, NPI, firms ───────────────────────────────

def _lodging_db(tmp_path):
    conn = dbpr.connect(tmp_path / "dbpr.db")
    lodging.ensure(conn)
    rows = [("H1", "HOTL", "SEA HOTEL LLC", "Sea Hotel", "6565 COLLINS AVE, MIAMI BEACH, FL 33141", "MIAMI BEACH", "33141",
             "+13058661637", 268),
            ("C1", "CNDO", "JOHN SMITH", None, "5555 COLLINS AVE, MIAMI BEACH, FL 33140", "MIAMI BEACH", "33140",
             "+13059491000", 1),                                                     # an owner renting a unit
            ("C2", "CNDO", "OCEAN TOWER CONDOMINIUM ASSN INC", None, "100 OCEAN DR, MIAMI BEACH, FL 33139", "MIAMI BEACH",
             "33139", "+13058661300", 120),
            ("D1", "DWEL", "JANE DOE", None, "7 PALM AVE, MIAMI BEACH, FL 33139", "MIAMI BEACH", "33139", "+13054279999", 1),
            ("H2", "HOTL", "FAR AWAY INN", "Far Away Inn", "1 MAIN ST, ORLANDO, FL 32801", "ORLANDO", "32801",
             "+14075550000", 50)]
    for lic, rank, lic_name, biz, addr, city, zipc, phone, units in rows:
        conn.execute("INSERT INTO dbpr_lodging VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (lic, rank, lic_name, biz, None, addr, dbpr.address_key(addr), city, zipc, "Dade", phone, units, "20"))
    conn.commit()
    return conn


def test_lodging_rows_are_business_licences_only(tmp_path):
    rows = ads_export._lodging_rows(_lodging_db(tmp_path))
    assert sorted(r["company"] for r in rows) == ["Far Away Inn", "Ocean Tower Condominium Assn INC", "Sea Hotel"]
    sea = next(r for r in rows if r["company"] == "Sea Hotel")
    assert (sea["phone"], sea["ct"], sea["zip"], sea["_source"]) == ("+13058661637", "miamibeach", "33141",
                                                                      "dbpr_lodging:HOTL")
    assert sea["value"]                                                              # rooms → annual estimate


def test_npi_rows_use_the_authorized_official(monkeypatch):
    object.__setattr__(settings, "target_cities", {"onsite": ["Aventura"]})
    org = {"npi": "1", "organization": "AVENTURA HOSPITAL & MEDICAL CENTER", "address": "20900 Biscayne Blvd, Aventura, FL 33180",
           "phone": "+13056827000", "official_name": "Nodilee James", "official_phone": "+13054664008"}
    calls = []
    monkeypatch.setattr("lead_engine.npi.search", lambda **kw: calls.append(kw) or [dict(org)])
    rows = ads_export._npi_rows(max_pages=1)
    assert len(rows) == 1 and (rows[0]["fn"], rows[0]["ln"], rows[0]["phone"]) == ("nodilee", "james", "+13054664008")
    assert {c.get("city") for c in calls if "city" in c} == {"AVENTURA"} and any("postal_code" in c for c in calls)


def test_registry_audience_counts_sources_and_never_uses_individual_owners(tmp_path, monkeypatch):
    object.__setattr__(settings, "target_cities", {"onsite": ["Miami Beach"]})
    conn = _lodging_db(tmp_path)
    dbpr.load_csv(FIXTURE, conn)
    monkeypatch.setattr(phone_resolver, "firm_phone", lambda firm: {"number": "+19549262921"})
    rows = ads_export.from_registry(min_units=10, conn=conn, include_npi=False)
    stats = ads_export.write_csv(rows, tmp_path / "a.csv")
    out = list(csv.DictReader(open(tmp_path / "a.csv")))
    assert list(out[0]) == ads_export.COLUMNS
    phones = {r["phone"] for r in out}
    assert "+13059491000" not in phones and "+13054279999" not in phones             # unit owners never exported
    assert {"+13058661637", "+13058661300"} <= phones and "+14075550000" not in phones  # Orlando is outside the area
    assert stats["by_source"]["dbpr_lodging:HOTL"] == 1 and stats["by_source"]["commercial_operator"] >= 5
    firms = [r for r in out if r["company_website"]]
    assert firms and all(r["phone"] for r in firms)


# ── 4. HTML panel ───────────────────────────────────────────────────────────

def test_batch_panel_is_self_contained_and_links_files(tmp_path):
    recs = [{**LEAD, "phone": "+13058661300", "phones": [{"number": "+13058661300", "label": "management"}]},
            {**LEAD, "company_name": "Blue <Plaza>", "lead_score": 40, "vertical": "plaza", "municipal": [], "pain": []}]
    (tmp_path / "pdf").mkdir()
    (tmp_path / "campo").mkdir()
    (tmp_path / "pdf" / "Oceanside_Plaza_Condo.pdf").write_bytes(b"%PDF")
    (tmp_path / "campo" / "Oceanside_Plaza_Condo_campo.pdf").write_bytes(b"%PDF")
    packs = [outreach_pack.messages(recs[0], "Oceanside_Plaza_Condo.pdf")]
    page = batch_report.build(tmp_path, recs, quality={"strategy": "onsite", "rejected": 3, "pdfs": 1}, outreach=packs)
    text = page.read_text()
    assert page.name == "index.html" and "<script" in text and "http" not in text.replace("http-equiv", "")
    data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', text, re.S).group(1))
    a, b = data["rows"]
    assert (a["pdf"], a["campo"], a["phone_label"]) == ("pdf/Oceanside_Plaza_Condo.pdf",
                                                         "campo/Oceanside_Plaza_Condo_campo.pdf", "administración")
    assert a["email"]["subject"] and a["sms"] and a["signals"][0].startswith("ventana legal de Milestone")
    assert b["pdf"] is None and b["name"] == "Blue <Plaza>" and "<Plaza>" not in text.split('id="data">')[0]
    assert "Rechazados" in text and "</script" not in data["rows"][1]["name"]


def test_file_names_follow_the_pdf_batch_order():
    recs = [{"company_name": "Same", "lead_score": 10}, {"company_name": "Same", "lead_score": 90}]
    assert batch_report.file_names(recs) == {1: "Same", 0: "Same_2"}


def test_portfolio_panel(tmp_path):
    firms = [{"firm": "Castle Management", "license": "CAB3838", "buildings": 6, "units": 1467, "cities": ["Miami Beach"],
              "total_estimated_low": 117100, "total_estimated_usd": 213800, "milestone_window": 2, "municipal_records": 2,
              "urgent": [{"name": "South Bay Club", "signals": [{"label": "x"}]}]}]
    (tmp_path / "pdf").mkdir()
    (tmp_path / "pdf" / "Castle_Management.pdf").write_bytes(b"%PDF")
    text = batch_report.build_portfolios(tmp_path, firms).read_text()
    assert '"pdf": "pdf/Castle_Management.pdf"' in text and "South Bay Club" in text and "$213,800" in text


def test_export_ads_cli_flags():
    from lead_engine.__main__ import main
    with pytest.raises(SystemExit) as e:
        main(["export-ads", "--help"])
    assert e.value.code == 0
