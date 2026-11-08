"""Municipal signals: address parsing, jurisdiction, classifiers on real record shapes, graceful degradation."""

from datetime import date

import pytest
import requests

from lead_engine import municipal_signals as ms

# Attribute shapes copied from the live layers (2026-11-08).
RECERT_PENDING = {"RecertificateYear": "2015", "CertificationStatus": "Pending",
                  "RecertificationProcessStatus": "submitted and NOT approved", "Address": "1865 BRICKELL AV",
                  "FolioNumber": "0141390000000", "PlanStatus": "In Review", "XLongitude": -80.19631594, "YLatitude": 25.75508038}
RECERT_DONE = {**RECERT_PENDING, "CertificationStatus": "Completed", "RecertificationProcessStatus": "submitted and approved"}
UNSAFE = {"CASE_NUM": "20250240812", "CASE_TYPE": "Unsafe Structure", "PROP_ADDR": "8912 SW 17 TER ", "OPEN_DATE": 1754884800000}
EXPIRED = {**UNSAFE, "CASE_TYPE": "Expired Permit"}
CODE = {"CASE_NUM": "202603002943", "CASE_DATE": 1791259200000, "ADDRESS": "3200 SW 122 AVE        ",
        "PROBLEM_DESC": "Structure Maintenance - Upkeep                    "}
CODE_VEHICLE = {**CODE, "PROBLEM_DESC": "Commercial Vehicle - Unauthorized"}
PERMIT = {"PermitNumber": "2027005974", "PermitIssuedDate": "2026-10-28", "ApplicationTypeDescription": "REPAIR",
          "DetailDescriptionComments": "CONCRETE RESTOR", "PropertyAddress": "6960 MIAMI GARDENS DR"}
DOOR = {**PERMIT, "ApplicationTypeDescription": "ALTER - EXTERIOR", "DetailDescriptionComments": "INSTALL 1 DOOR"}


def test_street_parts_and_same_street():
    assert ms.street_parts("1865 Brickell Ave, Miami, FL 33129") == ("1865", "BRICKELL")
    assert ms.street_parts("1865 BRICKELL AV") == ("1865", "BRICKELL")
    assert ms.street_parts("5900 COLLINS AVE             402") == ("5900", "COLLINS")
    assert ms.street_parts("250 NW 108 TER ") == ("250", "NW 108")
    assert ms.same_street("8912 SW 17th Terrace, Miami, FL 33155", "8912 SW 17 TER ")
    assert not ms.same_street("8912 SW 17th Ter, Miami, FL", "8912 SW 171 TER")
    assert ms.street_parts("Collins Ave & 25th St, Miami Beach") is None


def test_county_of():
    assert ms.county_of("1 X St, Miami, FL 33131") == "miami-dade"
    assert ms.county_of("1 X St, Hallandale, FL 33009") == "broward"
    assert ms.county_of("1 X St, Fort Lauderdale, FL 33304") == "broward"
    assert ms.county_of("1 X St", "Dade") == "miami-dade"
    assert ms.county_of("no zip") is None


def test_classifiers():
    r = ms.classify_recert(RECERT_PENDING)
    assert r["category"] == "municipal_recert" and r["weight"] == 6.0 and "1865 BRICKELL AV" in r["quote"]
    assert ms.classify_recert(RECERT_DONE) is None
    assert ms.classify_building_violation(UNSAFE)["category"] == "municipal_unsafe"
    assert ms.classify_building_violation(EXPIRED) is None
    assert ms.classify_code_case(CODE)["category"] == "municipal_code"
    assert ms.classify_code_case(CODE_VEHICLE) is None
    assert ms.classify_permit(PERMIT, date(2026, 11, 8))["quote"] == "Permiso 2027005974 (2026-10-28): CONCRETE RESTOR"
    assert ms.classify_permit(DOOR, date(2026, 11, 8)) is None
    assert ms.classify_permit({**PERMIT, "PermitIssuedDate": "2021-01-01"}, date(2026, 11, 8)) is None


def test_lookup_filters_city_coords_and_degrades(monkeypatch):
    calls = []

    def fake_query(layer, prefix):
        calls.append((layer.key, prefix))
        if layer.key == "mdc_code":
            raise requests.ConnectionError("down")
        if layer.key == "miami_recert":
            return [RECERT_PENDING, {**RECERT_PENDING, "Address": "1865 BRICKELL KEY"}]
        if layer.key == "mdc_permits":
            return [{**PERMIT, "PropertyAddress": "1865 BRICKELL AVE"}]
        return []

    monkeypatch.setattr(ms, "_query", fake_query)
    ms.reset()
    sigs = ms.lookup("1865 Brickell Ave, Miami, FL 33129", lat=25.7551, lng=-80.1963)
    assert [s["category"] for s in sigs] == ["municipal_recert", "municipal_permit"]   # sorted by weight
    assert "mdc_code" in ms._disabled                                                  # failing layer switched off
    assert ("miami_recert", "1865 BRICKELL") in calls
    calls.clear()
    assert ms.lookup("1865 Brickell Ave, Miami, FL 33129", lat=25.80, lng=-80.13) and all(c[0] != "mdc_code" for c in calls)
    # recert layer is City of Miami only; far coordinates drop the record
    assert [s["category"] for s in ms.lookup("1865 Brickell Ave, Miami, FL 33129", lat=25.90, lng=-80.12)] == ["municipal_permit"]
    calls.clear()
    assert ms.lookup("3800 S Ocean Dr, Hollywood, FL 33019") == [] and calls == []             # Broward: no layers
    calls.clear()
    ms.lookup("16485 Collins Ave, Sunny Isles Beach, FL 33160")
    assert "miami_recert" not in [c[0] for c in calls]                                      # not City of Miami
    ms.reset()
