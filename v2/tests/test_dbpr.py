"""DBPR registry: address normalisation, care-of parsing and matching (real rows from Condo_MD.csv)."""

from pathlib import Path

import pytest

from lead_engine import dbpr

FIXTURE = Path(__file__).parent / "fixtures" / "dbpr_condo_md_sample.csv"


@pytest.mark.parametrize("addr,key", [
    ("16375 Collins Ave, Sunny Isles Beach, FL 33160", "16375|COLLINS AVE|33160"),
    ("16375 COLLINS AVE, MIAMI BEACH, FL 33160-4537", "16375|COLLINS AVE|33160"),
    ("16485 Collins Avenue, Sunny Isles Beach, FL 33160, USA", "16485|COLLINS AVE|33160"),
    ("8760 S.W. 133 AVENUE ROAD, MIAMI, FL 33183", "8760|SW 133 AVE RD|33183"),
    ("8760 SW 133rd Ave Rd, Miami, FL 33183", "8760|SW 133 AVE RD|33183"),
    ("2401 SW 112TH AVE #64, MIAMI, FL 33165-2237", "2401|SW 112 AVE|33165"),
    ("13110-12 SW 68TH ST, MIAMI, FL 33183", "13110|SW 68 ST|33183"),
    ("1321 West 44th Street, Hialeah, FL 33012", "1321|W 44 ST|33012"),
    ("NE 207TH STREET, NORTH MIAMI BEACH, FL 33180", None),
    ("6XX NW 109TH AVE, MIAMI, FL 33172-5208", None),
])
def test_address_key(addr, key):
    assert dbpr.address_key(addr) == key


@pytest.mark.parametrize("route,expected", [
    ("C/O CASTLE MANAGEMENT LLC", {"management_company": "Castle Management LLC", "management_type": "third_party"}),
    ("C/O MANAGEMENT OFFICE - 2ND FLOOR", {"management_type": "onsite"}),
    ("C/O MANAGER OFFICE", {"management_type": "onsite"}),
    ("C/O ADMINISTRATION OFFICE", {"management_type": "onsite"}),
    ("C/O ISEL ZILKA /  RITZ MGMT, INC", {"management_company": "Ritz Mgmt, INC", "contact_name": "Isel Zilka",
                                          "management_type": "third_party"}),
    ("C/O SPM GROUP INC / VIVIAN GARCIA", {"management_company": "Spm Group INC", "contact_name": "Vivian Garcia",
                                            "management_type": "third_party"}),
    ("C/O PETER BATTY, PRESIDENT", {"contact_name": "Peter Batty", "contact_title": "Board President",
                                     "management_type": "self_managed"}),
    ("C/O JOE MAURA, ASSN PRESIDENT", {"contact_name": "Joe Maura", "contact_title": "Board President",
                                        "management_type": "self_managed"}),
    ("C/O LISETTE RICHEMOND - TREASURER", {"contact_name": "Lisette Richemond", "contact_title": "Board Treasurer",
                                            "management_type": "self_managed"}),
    ("C/O CHERYL J LEVIN PA, REGISTERED AGT", {"registered_agent": "Cheryl J Levin PA"}),
    ("C/O LIZ MACDONALD", {"contact_name": "Liz Macdonald"}),
    ("C/O PREMIER ASSN SERVICES, ORLY IGLESIAS", {"management_company": "Premier Assn Services", "contact_name": "Orly Iglesias",
                                                   "management_type": "third_party"}),
    ("C/O TOP SERVICE PROPERTY MANAGEMENT, LLC", {"management_company": "Top Service Property Management, LLC",
                                                   "management_type": "third_party"}),
    ("C/O AGE RE SERVICES LLC, REGD AGENT", {"registered_agent": "Age Re Services LLC"}),
    ("C/O ASSOCIATION LAW GROUP PL, RGD AGENT", {"registered_agent": "Association Law Group Pl"}),
    ("C/O MGMT OFFICE, ANN LEFKOWITZ", {"management_type": "onsite", "contact_name": "Ann Lefkowitz",
                                         "contact_title": "Oficina de administración (DBPR)"}),
    ("C/O SoBe Management c/o Philippe Lamery", {"management_company": "Sobe Management", "contact_name": "Philippe Lamery",
                                                 "management_type": "third_party"}),
    ("ATTN: LOBBY", {"management_type": "onsite"}),
    ("C/O ATTENTION MANAGEMENT OFFICE", {"management_type": "onsite"}),
    ("C/O PROPERTY MANAGER", {"management_type": "onsite"}),
    ("C/O GENERAL MANAGER, JOHN DOE", {"management_type": "onsite", "contact_name": "John Doe",
                                        "contact_title": "Oficina de administración (DBPR)"}),
    ("C/O PROPERTY MANAGERS INC", {"management_company": "Property Managers INC", "management_type": "third_party"}),
    ("ATTN: MGMT. OFFICE", {"management_type": "onsite"}),
    ("C/O OFFICE-UNIT #203", {"management_type": "onsite"}),
    ("C/O BOARD OF DIRECTORS #105", {"management_type": "self_managed"}),
    ("C/O AKAM", {"management_company": "AKAM", "management_type": "third_party"}),
    ("", {}),
])
def test_parse_route(route, expected):
    got = {k: v for k, v in dbpr.parse_route(route).items() if v}
    assert got == expected


@pytest.fixture()
def conn(tmp_path):
    c = dbpr.connect(tmp_path / "dbpr.db")
    assert dbpr.load_csv(FIXTURE, c) == 9
    return c


def test_match_by_address_picks_managed_project_and_sums_units(conn):
    m = dbpr.match("16485 Collins Ave, Sunny Isles Beach, FL 33160", "Oceania II", conn)
    assert m["matched_by"] == "address"
    assert m["projects_at_address"] == 2 and m["units"] == 330          # Oceania II + III share the address
    assert m["condo_name"] == "OCEANIA II, A CONDO"                     # the one with a care-of line wins
    assert m["management_type"] == "onsite"

    m = dbpr.match("5555 Collins Avenue, Miami Beach, FL 33140, USA", None, conn)
    assert (m["management_company"], m["units"], m["association"]) == ("Castle Management LLC", 299,
                                                                       "Oceanside Plaza Condo Assn INC")


def test_match_by_name_when_registry_has_no_number(conn):
    m = dbpr.match("15500 Miami Lakeway N, Miami Lakes, FL 33014", "Celebration Point Condo Six", conn)
    assert m and m["condo_name"] == "CELEBRATION POINT CONDO # SIX" and m["matched_by"].startswith("name~")
    assert dbpr.match("1 Unknown St, Miami, FL 33014", "Totally Different Tower", conn) is None



@pytest.mark.parametrize("raw,short", [
    ("OCEANIA II, A CONDO", "Oceania II"),
    ("TIDES ON HOLLYWOOD BEACH CONDO, THE", "The Tides On Hollywood Beach"),
    ("RONEY PALACE, A CONDO, THE", "The Roney Palace"),
    ("CLUB AT BRICKELL BAY PLAZA, A CONDO THE", "The Club At Brickell Bay Plaza"),
    ("CONDO OF POINT EAST ONE, A CONDO", "Condo of Point East One"),
    ("BRICKELL ON THE RIVER NORTH TOWER, A CONDO", "Brickell On the River North Tower"),
])
def test_short_condo_name(raw, short):
    assert dbpr._short_condo_name(raw) == short


def test_title_case_keeps_directionals():
    assert dbpr._title_case("31 SE 5TH ST, MIAMI") == "31 SE 5th St, Miami"
    assert dbpr._title_case("2156 N.E. 67TH STREET") == "2156 NE 67th Street"



def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(dbpr.SCHEMA.replace(", firm_id TEXT", ""))     # dbpr_routes without firm_id
    old.close()
    c = dbpr.connect(path)
    assert "firm_id" in {r[1] for r in c.execute("PRAGMA table_info(dbpr_routes)")}
    assert dbpr.load_csv(FIXTURE, c) == 9
    assert c.execute("SELECT firm_id FROM dbpr_routes WHERE project_number = 'PR1S003305'").fetchone()[0] == "KEY:CASTLEMANAGEMENT"
