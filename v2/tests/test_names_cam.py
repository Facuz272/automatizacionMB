"""Name keys and the DBPR CAM licence registry (real CAB rows + synthetic CAM people)."""

from datetime import date
from pathlib import Path

import pytest

from lead_engine import cam, dbpr
from lead_engine.names import firm_key, firm_match_score, person_key, split_person

FIXTURE = Path(__file__).parent / "fixtures" / "cam_lic38cam_sample.csv"


@pytest.mark.parametrize("a,b", [
    ("Castle Management, LLC", "CASTLE MGMT LLC"),
    ("Premier Assn. Services Inc", "PREMIER ASSOCIATION SERVICES"),
    ("Miami Mangement, INC.", "Miami Management Inc"),
    ("V.T.J. Management & Maintenance, INC.", "Vtj Management and Maintenance"),
])
def test_firm_key_equivalences(a, b):
    assert firm_key(a) == firm_key(b)


def test_firm_match_score_uses_distinctive_words():
    assert firm_match_score(firm_key("Carribbean Property Mgmt"), firm_key("Caribbean Property Management Inc"))[1] == "spelling"
    assert firm_match_score(firm_key("Atlas Property Management"), firm_key("Alsop Property Management LLC")) is None
    assert firm_match_score(firm_key("Kw Property Management"), firm_key("J & L Property Management, Inc.")) is None
    assert firm_match_score(firm_key("Florida Property Management"), firm_key("Floridian Property Management")) is None
    assert firm_match_score(firm_key("Crest Mgmt"), firm_key("Crest Management Group Inc")) == (0.85, "same_word")


@pytest.mark.parametrize("name,expected", [
    ("Orly Iglesias", ("IGLESIAS", "ORLY")),
    ("IGLESIAS, ORLY M", ("IGLESIAS", "ORLY")),
    ("Maria J. Lopez", ("LOPEZ", "MARIA")),
    ("Peter Batty Jr", ("BATTY", "PETER")),
    ("Madonna", None),
])
def test_split_person(name, expected):
    assert split_person(name) == expected
    assert (person_key(name) is None) == (expected is None)


@pytest.fixture()
def conn(tmp_path):
    c = dbpr.connect(tmp_path / "dbpr.db")
    assert cam.load_csv(FIXTURE, c) == 12
    return c


def test_verify_firm_exact_dba_and_spelling(conn):
    v = cam.verify_firm("Castle Management LLC", conn)
    assert (v["license_id"], v["active"], v["matched_by"]) == ("CAB3838", True, "exact")
    assert cam.verify_firm("C/O FIRSTSERVICE RESIDENTIAL".replace("C/O ", ""), conn)["license_id"] == "CAB2893"   # via DBA
    assert cam.verify_firm("Carribbean Property Mgmt", conn)["matched_by"].startswith("spelling")
    assert cam.verify_firm("KW Property Mgmt", conn)["license_id"] == "CAB3320"
    assert cam.verify_firm("First Service Residential", conn)["license_id"] == "CAB2893"   # spacing variant
    assert cam.verify_firm("Totally Unknown Realty", conn) is None


def test_verify_person_active_homonyms_and_expired(conn):
    v = cam.verify_person("Ana Diaz", conn)
    assert v["license_id"] == "CAM9990001" and v["active"] and v["county"] == "Miami-Dade"   # Miami-Dade preferred
    assert v["homonyms"] == 1 and v["name"] == "Ana Maria Diaz"
    expired = cam.verify_person("Mario Perez", conn)
    assert expired and expired["active"] is False
    inactive = cam.verify_person("Gary Agnello", conn)
    assert inactive and inactive["active"] is False                     # secondary status "I" (real row)
    assert cam.verify_person("Nobody Here", conn) is None


def test_is_active_rules():
    assert cam.is_active("C", "A", "2028-09-30", date(2026, 11, 8))
    assert cam.is_active("C", "", "2027-09-30", date(2026, 11, 8))      # firms have blank secondary status
    assert not cam.is_active("C", "I", "2028-09-30", date(2026, 11, 8))
    assert not cam.is_active("S", "A", "2028-09-30", date(2026, 11, 8))
    assert not cam.is_active("C", "A", "2020-09-30", date(2026, 11, 8))
