"""Deal estimator, DBPR seed collector and portfolio clusters."""

import csv
import io
from pathlib import Path

import pytest

from lead_engine import dbpr, scoring, valuation
from lead_engine.collectors import dbpr_seed
from lead_engine.models import Lead

FIXTURE = Path(__file__).parent / "fixtures" / "dbpr_condo_md_sample.csv"


def test_estimate_scales_with_size_amenities_and_salt():
    tower = {"levels": 32, "footprint_m2": 2000, "units": 299, "coastal": True,
             "amenities": {"parking_garage": "osm", "pool_deck": "osm", "tennis_court": "osm"}}
    e = valuation.estimate(tower)
    assert 55_000 < e["low"] < e["high"] < 130_000
    assert set(e["components"]) == {"facade", "walkways", "garage", "pool_deck", "courts"}
    assert e["basis"]["rope_access"] and e["basis"]["footprint_source"] == "osm"
    inland = valuation.estimate({**tower, "coastal": False})
    assert inland["high"] < e["high"]
    small = valuation.estimate({"units": 40, "levels": 3})
    assert 2_000 < small["low"] < small["high"] < 6_000 and small["basis"]["footprint_source"] == "units"
    huge = valuation.estimate({"units": 1266})
    assert huge["basis"]["levels"] == 20 and huge["high"] < 90_000          # several towers, not a 159-storey one
    assert valuation.estimate({}) is None and valuation.estimate({"levels": 5}) is None
    assert valuation.label({"low": 4500, "high": 7200}) == "est. US$ 4.5k–7.2k/año"
    assert valuation.label({"low": 63000, "high": 116000}) == "est. US$ 63k–116k/año"


def test_rank_uses_deal_as_tiebreak():
    a = Lead(source="s", source_id="a", strategy_kind="onsite", company_name="A")
    b = Lead(source="s", source_id="b", strategy_kind="onsite", company_name="B")
    c = Lead(source="s", source_id="c", strategy_kind="onsite", company_name="C")
    a.lead_score = b.lead_score = 60
    c.lead_score = 70
    a.estimated_deal, b.estimated_deal = {"low": 5000, "high": 9000}, {"low": 20000, "high": 40000}
    assert [l.source_id for l in scoring.rank([a, b, c])] == ["c", "b", "a"]


# Synthetic registry rows (real column layout) to exercise the seed filters and clusters.
SYNTH = [
    ("PRX1", "BLUE TOWER, A CONDO", "100 OCEAN DR, MIAMI BEACH, FL 33139", 210, "C/O MANAGEMENT OFFICE", "100 OCEAN DR"),
    ("PRX2", "GREEN TOWER CONDO", "200 OCEAN DR, MIAMI BEACH, FL 33139", 90, "", "200 OCEAN DR"),          # same address -> onsite
    ("PRX3", "RED TOWER CONDO", "300 OCEAN DR, MIAMI BEACH, FL 33139", 120, "C/O ACME PROPERTY MGMT", "9 FIRM ST"),
    ("PRX4", "PINK TOWER CONDO", "400 BRICKELL AVE, MIAMI, FL 33131", 300, "C/O ACME PROPERTY MANAGEMENT, LLC", "9 FIRM ST"),
    ("PRX5", "GREY HOUSE CONDO", "500 NW 7 ST, MIAMI, FL 33125", 400, "C/O ACME PROPERTY MANAGEMENT", "9 FIRM ST"),
    ("PRX6", "TINY CONDO", "600 OCEAN DR, MIAMI BEACH, FL 33139", 20, "C/O MANAGEMENT OFFICE", "600 OCEAN DR"),
    ("PRX7", "FAR CONDO", "700 MAIN ST, ORLANDO, FL 32801", 500, "C/O ACME PROPERTY MANAGEMENT", "9 FIRM ST"),
]


@pytest.fixture()
def conn(tmp_path):
    c = dbpr.connect(tmp_path / "dbpr.db")
    dbpr.load_csv(FIXTURE, c)
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL)
    w.writerow(["Project Number", "File Number", "Condo Name", "County", "Street City State Zip", "Units", "Recorded Date",
                "Primary Status", "Secondary Status", "Managing Entity Number", "Managing Entity Name",
                "Managing Entity Route", "Managing Entity Street", "Managing Entity City", "Managing Entity State",
                "Managing Entity Zip"])
    for pn, name, addr, units, route, me_street in SYNTH:
        county = "Orange" if "ORLANDO" in addr else "Dade"
        w.writerow([pn, "1", name, county, addr, units, "01/01/2000", "Approved", "Recorded", "MA1", name + " ASSN",
                    route, me_street, addr.split(",")[1].strip(), "FL", addr[-5:]])
    buf.seek(0)
    dbpr.load_rows(c, csv.DictReader(buf))
    return c


def test_seed_onsite_filters_area_units_and_type(conn):
    cands = dbpr_seed.candidates("onsite", conn, cities=["Miami Beach", "Sunny Isles Beach"], min_units=50)
    names = [d["condo_name"] for d in cands]
    assert names[0] == "OCEANIA II, A CONDO"              # 330 units (II + III share the address), ≥80 first
    assert "BLUE TOWER, A CONDO" in names and "GREEN TOWER CONDO" in names
    assert "TINY CONDO" not in names and "RED TOWER CONDO" not in names   # <50 units / external firm
    assert next(d for d in cands if d["condo_name"] == "OCEANIA II, A CONDO")["units_total"] == 330


def test_seed_portfolio_and_brickell_alias(conn):
    cands = dbpr_seed.candidates("portfolio", conn, cities=["Brickell Miami", "Miami Beach"], min_units=50)
    assert [d["condo_name"] for d in cands] == ["PINK TOWER CONDO", "OCEANSIDE PLAZA CONDO", "RED TOWER CONDO"]


def test_seed_collect_builds_leads(conn):
    leads = list(dbpr_seed.collect("onsite", 2, conn, cities=["Miami Beach", "Sunny Isles Beach"]))
    top = leads[0]
    assert top.source == "dbpr" and top.company_name == "Oceania II"
    assert top.address == "16485 Collins Avenue, Miami Beach, FL 33160"
    assert top.public_records["dbpr"]["units"] == 330 and top.management_type == "onsite"


def test_cluster_counts_same_firm_in_coverage_and_orders_by_proximity(conn):
    cl = dbpr.cluster(company="Acme Property Mgmt", exclude_project="PRX3", near_zip="33139", near_city="Miami Beach", conn=conn)
    assert cl["by"] == "company" and cl["count"] == 3                     # Orlando (Orange county) is outside coverage
    assert [s["name"] for s in cl["siblings"]] == ["Grey House", "Pink Tower"]   # no same-ZIP sibling -> by units
    assert dbpr.cluster(company="Nobody Mgmt", conn=conn) is None
    lead = Lead(source="s", source_id="x", strategy_kind="onsite", company_name="Red Tower", management_company="Acme Property Mgmt",
                portfolio_cluster=cl, lead_score=70)
    assert "Acme Property Mgmt administra 3 condominios en la zona (incl. Grey House, Pink Tower)" in lead.brief()
