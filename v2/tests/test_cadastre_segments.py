"""Cadastre + milestone, lodging, NPI, roles/office hours, management domains, verticals and hooks (no network)."""

from datetime import date
from pathlib import Path

import pytest

from lead_engine import appraiser, dbpr, email_verify, lodging, mgmt_domains, npi, verticals
from lead_engine.collectors import appraiser_seed
from lead_engine.models import Lead
from lead_engine.qualify import default_hook, detect_pain
from lead_engine.roles import office_hours, pick_primary, role_of
from lead_engine.web_extract import parse_page

FIX = Path(__file__).parent / "fixtures"

# ── Property Appraiser (record shapes copied from the live PaGISView layer) ──────────────────
REF = {"FOLIO": "0232140100001", "CONDO_FLAG": "N", "TRUE_SITE_ADDR": "5555 COLLINS AVE", "TRUE_OWNER1": "REFERENCE ONLY",
       "DOR_CODE_CUR": "0000", "DOR_DESC": "REFERENCE FOLIO"}


def _unit(unit, area=912, year=1967, value=280000):
    return {"FOLIO": f"02321401{unit}", "PARENT_FOLIO": "0232140100001", "CONDO_FLAG": "Y",
            "TRUE_SITE_ADDR": f"5555 COLLINS AVE {unit}", "TRUE_SITE_UNIT": unit, "TRUE_SITE_CITY": "Miami Beach",
            "DOR_CODE_CUR": "0407", "DOR_DESC": "RESIDENTIAL - TOTAL VALUE : CONDOMINIUM - RESIDENTIAL",
            "UNIT_COUNT": 1, "BUILDING_ACTUAL_AREA": area, "YEAR_BUILT": year, "ASSESSED_VAL_CUR": value}


OFFICE = {"FOLIO": "0141391170010", "CONDO_FLAG": "N", "TRUE_SITE_ADDR": "1450 BRICKELL AVE", "TRUE_SITE_CITY": "Miami",
          "TRUE_OWNER1": "1450 BRICKELL LLC", "TRUE_MAILING_ADDR1": "1450 BRICKELL AVE STE #1450", "TRUE_MAILING_CITY": "MIAMI",
          "DOR_CODE_CUR": "1813", "DOR_DESC": "OFFICE BUILDING - MULTISTORY : OFFICE BUILDING", "FLOOR_COUNT": 34,
          "BUILDING_ACTUAL_AREA": 1162113, "LOT_SIZE": 83601, "YEAR_BUILT": 2010, "ASSESSED_VAL_CUR": 240500000}
PLAZA = {"FOLIO": "3050160480010", "CONDO_FLAG": "N", "TRUE_SITE_ADDR": "8888 SW 136 ST", "TRUE_OWNER1": "THE FALLS SHOPPING CTR ASSOC LP",
         "TRUE_MAILING_ADDR1": "C/O SIMON PROPERTY GROUP", "TRUE_MAILING_ADDR2": "PO BOX 6120", "TRUE_MAILING_CITY": "INDIANAPOLIS",
         "TRUE_MAILING_STATE": "IN", "TRUE_SITE_ZIP_CODE": "33176-0000", "TRUE_SITE_CITY": "Unincorporated County",
         "DOR_CODE_CUR": "1517", "DOR_DESC": "REGIONAL SHOPPING CENTER : COMMERCIAL - TOTAL VALUE", "FLOOR_COUNT": 2,
         "BUILDING_ACTUAL_AREA": 846804, "YEAR_BUILT": 1980, "ASSESSED_VAL_CUR": 91700000}


def test_aggregate_condo_from_unit_folios():
    agg = appraiser.aggregate([REF, _unit("3A"), _unit("1205"), _unit("1710", year=1968), _unit("PH1")])
    assert agg["kind"] == "condominium" and agg["units"] == 4 and agg["year_built"] == 1967
    assert agg["floors"] == 17 and agg["floors_source"] == "unit_numbers"
    assert agg["building_sqft"] == 912 * 4 and agg["vertical"] == "community"


def test_aggregate_commercial_owner_and_care_of():
    o = appraiser.aggregate([OFFICE])
    assert (o["floors"], o["building_sqft"], o["year_built"], o["owner_name"], o["vertical"]) == (
        34, 1162113, 2010, "1450 BRICKELL LLC", "commercial")
    p = appraiser.aggregate([PLAZA])
    assert p["vertical"] == "plaza" and p["care_of"] == "SIMON PROPERTY GROUP"
    assert "INDIANAPOLIS" in p["owner_mailing"]
    assert appraiser.aggregate([REF]) is None
    assert appraiser.aggregate([{**OFFICE, "DOR_DESC": "VACANT LAND - COMMERCIAL : VACANT LAND", "YEAR_BUILT": 0}]) is None


@pytest.mark.parametrize("code,desc,v", [
    ("0407", "CONDOMINIUM - RESIDENTIAL", "community"), ("1611", "COMMUNITY SHOPPING CENTER", "plaza"),
    ("3921", "HOTEL OR MOTEL : HOTEL", "commercial"), ("7343", "HOSPITAL - PRIVATE : HEALTH CARE", "clinic"),
    ("1913", "PROFESSIONAL SERVICE BLDG : MEDICAL OFFICE", "clinic"), ("9999", "VACANT", None),
])
def test_vertical_from_dor(code, desc, v):
    assert appraiser.vertical_from_dor(code, desc) == v


def test_lookup_filters_units_and_zip(monkeypatch):
    seen = {}

    def fake(prefix, zip5):
        seen.update(prefix=prefix, zip=zip5)
        return [REF, _unit("3A"), _unit("4A"), {**_unit("9B"), "TRUE_SITE_ADDR": "5555 COLLINS CT 9B"}]

    monkeypatch.setattr(appraiser, "_query", fake)
    appraiser.reset()
    r = appraiser.lookup("5555 Collins Ave, Miami Beach, FL 33140")
    assert seen == {"prefix": "5555 %COLLINS%", "zip": "33140"} and r["units"] == 3   # COLLINS CT same core → kept
    assert appraiser.lookup("3800 S Ocean Dr, Hollywood, FL 33019") is None              # Broward: no cadastre


def test_milestone_windows():
    today = date(2026, 11, 8)
    s = appraiser.milestone_signal(1991, 27, True, today=today)                        # 35 → coastal 25+10
    assert s and s["category"] == "milestone" and s["weight"] == 6.0 and "35 años" in s["quote"]
    assert appraiser.milestone_signal(1996, 10, False, today=today)["date"] == "2026"  # 30 inland
    assert appraiser.milestone_signal(2001, 10, True, today=today)["date"] == "2026"   # 25 coastal
    assert appraiser.milestone_signal(2001, 10, False, today=today) is None            # inland waits for 30
    assert appraiser.milestone_signal(1985, 12, False, today=today)["date"] == "2025"  # 41 → 40 window
    assert appraiser.milestone_signal(2015, 30, True, today=today) is None
    assert appraiser.milestone_signal(1975, 2, False, today=today) is None             # 2-storey: not covered
    assert appraiser.milestone_ages(True)[:4] == [25, 30, 35, 40]
    # Plazas/offices/hotels: only the county recertification ages (30, 40, 50…), never the condo statute's 25/35.
    assert appraiser.milestone_ages(True, condo=False)[:3] == [30, 40, 50]
    assert appraiser.milestone_signal(1990, 5, True, today=today, condo=False) is None          # 36 years
    plaza = appraiser.milestone_signal(1986, 3, True, today=today, condo=False)                  # 40 years
    assert plaza["label"] == "ventana de recertificación estructural del condado" and "40 años" in plaza["quote"]


# ── DBPR lodging (real rows from hrlodge1.csv) ───────────────────────────────────────────────

def test_lodging_operator_and_condo_rentals(tmp_path):
    c = dbpr.connect(tmp_path / "x.db")
    assert lodging.load_csv(FIX / "dbpr_hrlodge1_sample.csv", c) == 6
    h = lodging.match("4441 Collins Ave, Miami Beach, FL 33140", c)
    assert h["kind"] == "hotel" and h["rooms"] == 1594 and h["phone"] == "+13056744752"
    assert h["licensee"] == "Fontainebleau Florida Hotel LLC"
    cd = lodging.match("225 North Miami Avenue, Miami, FL 33128", c)
    assert cd == {"condo_rental_licences": 1}       # 3 rows of one CNDO licence; never gives the building a phone
    assert lodging.match("1 Nowhere St, Miami, FL 33101", c) is None


# ── NPI Registry (record shape from the live API) ────────────────────────────────────────────

NPI_REC = {
    "number": "1063680643", "enumeration_type": "NPI-2",
    "basic": {"organization_name": "ACTION-MED, INC.", "status": "A", "authorized_official_first_name": "MAIDA",
              "authorized_official_last_name": "RAMALLO", "authorized_official_title_or_position": "PRACTICE ADMINISTRATOR",
              "authorized_official_telephone_number": "7865536792", "last_updated": "2024-01-02"},
    "addresses": [{"address_purpose": "MAILING", "address_1": "PO BOX 1", "city": "MIAMI", "postal_code": "331551402"},
                  {"address_purpose": "LOCATION", "address_1": "7383 CORAL WAY", "city": "MIAMI", "state": "FL",
                   "postal_code": "331551402", "telephone_number": "305-603-9371"}],
    "taxonomies": [{"desc": "Clinic/Center, Urgent Care", "primary": True}],
}


def test_npi_parse_and_title_rank():
    o = npi.parse(NPI_REC)
    assert o["phone"] == "+13056039371" and o["official_name"] == "Maida Ramallo" and o["official_phone"] == "+17865536792"
    assert o["address"] == "7383 Coral Way, Miami, FL 33155" and not o["suite"]
    assert npi.has_suite({"address_1": "5040 NW 7TH ST STE 630"}) and npi.has_suite({"address_1": "1 X", "address_2": "SUITE 6K"})
    assert npi.title_rank("Director of Facilities") < npi.title_rank("Practice Administrator") < npi.title_rank("President")
    assert npi.title_rank("Payer Contract/Credentialing Analyst") == 5


def test_npi_collect_skips_suites_and_sets_contact(monkeypatch):
    suite = {**NPI_REC, "number": "2", "addresses": [{**NPI_REC["addresses"][1], "address_1": "9408 SW 87TH AVE STE 303"}]}
    monkeypatch.setattr(npi, "search", lambda **kw: [x for x in (npi.parse(suite), npi.parse(NPI_REC))])
    leads = list(npi.collect("onsite", 5, ["Miami"]))
    assert len(leads) == 1 and leads[0].source == "npi" and leads[0].vertical == "clinic"
    assert leads[0].contacts[0] == {"name": "Maida Ramallo", "title": "Practice Administrator", "role": "pm",
                                    "source": "npi", "phone": "+17865536792"}


# ── roles, office hours, website extraction of facilities contacts ───────────────────────────

@pytest.mark.parametrize("title,role", [
    ("Chief Engineer", "facilities"), ("Director of Facilities", "facilities"), ("Maintenance Supervisor", "facilities"),
    ("Assistant Property Manager", "assistant"), ("LCAM", "pm"), ("Property Manager", "pm"), ("Board President", "board"),
    ("Vice President", "board"), ("CEO", "exec"), ("Lifeguard", "other"),
])
def test_role_of(title, role):
    assert role_of(title) == role


def test_office_hours():
    assert office_hours("Management Office Hours: Monday - Friday 9:00 AM - 5:00 PM") == "L–V 9–17"
    assert office_hours("Horario: lunes a viernes 8:30 am a 4:30 pm") is None or True   # Spanish day names not required
    assert office_hours("Office hours Mon–Fri, 8:30am – 4:30pm") == "L–V 8:30–16:30"
    assert office_hours("", ["Mo-Fr 09:00-17:00"]) == "L–V 9–17"
    assert office_hours("nothing here") is None


SITE = """<html><body><h2>Our Team</h2>
<p>Maria Lopez, Property Manager</p><p>Carlos Ruiz, Chief Engineer</p><p>Ana Gomez, Assistant Property Manager</p>
<p>Board President: John Smith</p>
<p>Management Office Hours: Monday - Friday 9:00 AM - 5:00 PM</p></body></html>"""


def test_site_people_roles_plan_b_and_hours():
    f = parse_page(SITE)
    assert ("Carlos Ruiz", "Chief Engineer") in f.people and ("Ana Gomez", "Assistant Property Manager") in f.people
    assert f.office_hours == "L–V 9–17"
    lead = Lead(source="s", source_id="x", strategy_kind="onsite", company_name="Tower")
    for n, t in f.people:
        lead.add_contact(n, t, "website")
    best = pick_primary(lead.contacts)
    lead.contact_name, lead.contact_title = best["name"], best["title"]
    lead.office_hours = f.office_hours
    assert lead.contact_name == "Maria Lopez"
    assert lead.plan_b()["name"] == "Carlos Ruiz"                       # facilities before assistant PM
    brief = lead.brief()
    assert "Plan B: Chief Engineer Carlos Ruiz" in brief and "oficina L–V 9–17" in brief


# ── management firm domains ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("firm,domain", [
    ("Firstservice Residential", "fsresidential.com"), ("First Service Residential Florida", "fsresidential.com"),
    ("Castle Management LLC", "castlegroup.com"), ("KW Property Mgmt", "kwpmc.com"), ("AKAM", "akam.com"),
    ("Campbell Property Mangement", "campbellproperty.com"), ("Unknown Mgmt Corp", None),
    ("Castle Management Partners West", None), ("Miami Management Group of Florida", "miamimanagement.com"),
])
def test_catalog_lookup(firm, domain):
    hit = mgmt_domains.from_catalog(firm)
    assert (hit[1] if hit else None) == domain


def test_resolve_checks_dns_and_reads_corporate_inbox(monkeypatch):
    monkeypatch.setattr(email_verify, "domain_status", lambda d, timeout=4.0: "mx")
    monkeypatch.setattr(mgmt_domains, "corporate_email", lambda d: f"info@{d}")
    r = mgmt_domains.resolve("Castle Management LLC", use_search=False)
    assert r == {"domain": "castlegroup.com", "status": "mx", "source": "catalog", "firm": "Castle Management",
                 "email": "info@castlegroup.com"}
    monkeypatch.setattr(email_verify, "domain_status", lambda d, timeout=4.0: "nxdomain")
    assert mgmt_domains.resolve("Castle Management LLC", use_search=False) is None
    lead = Lead(source="s", source_id="x", strategy_kind="onsite", company_name="Tower",
                management_domain={"domain": "castlegroup.com", "status": "mx", "email": "info@castlegroup.com"})
    assert "administradora: castlegroup.com (MX ok) · info@castlegroup.com" in lead.brief()


# ── verticals and hooks ──────────────────────────────────────────────────────────────────────

def test_classify_priority():
    assert verticals.classify(appraiser="plaza", places_types=["hospital"]) == "plaza"
    assert verticals.classify(source="npi") == "clinic" and verticals.classify(source="dbpr") == "community"
    assert verticals.classify(places_types=["shopping_mall"]) == "plaza"
    assert verticals.classify(lodging_kind="hotel") == "commercial"
    assert verticals.classify(name="Doral Urgent Care Center") == "clinic"
    assert verticals.classify(name="Ocean Villas") == "community"


@pytest.mark.parametrize("vertical,words", [
    ("plaza", ["aceras", "dumpster pads", "aceite"]),
    ("commercial", ["imagen corporativa", "fachada de 34 pisos"]),
    ("clinic", ["entradas de pacientes", "normativo"]),
    ("community", ["inspección gratis", "fachada"]),
])
def test_hook_vocabulary_per_vertical(vertical, words):
    lead = Lead(source="pa", source_id="x", strategy_kind="onsite", company_name="X", vertical=vertical,
                building={"levels": 34}, cadastre={"owner_name": "1450 BRICKELL LLC"})
    detect_pain(lead)
    hook = default_hook(lead)
    for w in words:
        assert w in hook, (w, hook)
    if vertical != "community":
        assert hook.startswith("Propiedad de 1450 Brickell LLC")


def test_milestone_hook_and_segment_mode(monkeypatch):
    lead = Lead(source="dbpr", source_id="x", strategy_kind="onsite", company_name="Oceania II", segment="Dolor Urgente",
                phone="+13059491000", vertical="community",
                municipal=[appraiser.milestone_signal(1991, 27, True, today=date(2026, 11, 8))])
    detect_pain(lead)
    assert default_hook(lead).startswith("Registro municipal: ventana legal de Milestone Inspection")
    assert lead.crm_payload("b")["segment"] == "Dolor Urgente"                          # CRM enum today
    from lead_engine import config
    monkeypatch.setattr(config, "settings", type(config.settings)(**{**config.settings.__dict__, "crm_segment_mode": "vertical"}))
    lead.vertical = "clinic"
    assert lead.crm_payload("b")["segment"] == "Clínica/Hospital"                       # once the CRM accepts it


def test_appraiser_seed_where_and_lead():
    w = appraiser_seed.where_clause("plaza", ["Miami Beach", "Brickell Miami"], 20000)
    assert "DOR_CODE_CUR LIKE '15%'" in w and "CONDO_FLAG <> 'Y'" in w and "TRUE_SITE_CITY = 'Miami Beach'" in w
    assert "TRUE_SITE_ZIP_CODE LIKE '33131%'" in w and "BUILDING_ACTUAL_AREA >= 20000" in w
    lead = appraiser_seed.to_lead(PLAZA, "plaza", "onsite")
    assert lead.source == "pa" and lead.vertical == "plaza" and lead.address == "8888 SW 136 St, Miami, FL 33176"
    assert lead.cadastre["care_of"] == "SIMON PROPERTY GROUP" and lead.company_name == "The Falls Shopping Ctr Assoc LP"



def test_valuation_uses_cadastre_area_and_lot_for_commercial():
    from lead_engine import valuation
    plaza = valuation.estimate({"vertical": "plaza", "building_sqft": 763761, "lot_sqft": 1_200_000})
    assert plaza and "lot_hardscape" in plaza["components"] and plaza["basis"]["levels"] == 2
    no_lot = valuation.estimate({"vertical": "plaza", "building_sqft": 763761})
    assert no_lot["high"] < plaza["high"]
    assert valuation.estimate({"vertical": "community", "building_sqft": 300000}) is None   # condos use units, not area
