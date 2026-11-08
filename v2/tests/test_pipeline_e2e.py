"""Whole run offline: fake Places + sites + Overpass + DBPR (real rows) + search + DNS.

Checks the gates, the labelled primary phone, DBPR/search attribution, the
physical brief, MX filtering, ordering by lead_score and quality_report.json.
"""

import json
from pathlib import Path

import pytest

from lead_engine import cam, costs, dbpr, email_verify, geo, municipal_signals, pipeline, search
from lead_engine.collectors import google_places
from lead_engine.state import Ledger
from lead_engine.web_extract import parse_page

from test_geo import ELEMENTS, LAT, LON
from test_search import RESULTS

FIXTURE = Path(__file__).parent / "fixtures" / "dbpr_condo_md_sample.csv"
CAM_FIXTURE = Path(__file__).parent / "fixtures" / "cam_lic38cam_sample.csv"
# Shape of appraiser.lookup for 5555 Collins Ave (live values 2026-11-08).
CADASTRE_5555 = {"kind": "condominium", "folio": "0232140100001", "units": 297, "year_built": 1967, "floors": 17,
                 "floors_source": "unit_numbers", "building_sqft": 310037, "assessed_value": 116528282,
                 "land_use": "RESIDENTIAL - TOTAL VALUE : CONDOMINIUM - RESIDENTIAL", "vertical": "community",
                 "source": "miami_dade_property_appraiser"}
RECERT = {"category": "municipal_recert", "label": "recertificación 40 años pendiente", "weight": 6.0,
          "quote": "City of Miami: 5555 COLLINS AV — recertificación 2015 pendiente (submitted and NOT approved)",
          "date": "2015", "source": "municipal", "layer": "miami_recert", "ref": "x"}

OCEANSIDE_SITE = """
<html><body><h1>Oceanside Plaza</h1>
<p>Resort-style pool deck, covered parking garage and oceanfront walkways.</p>
<p>Management Office: <a href="tel:3058661300">305-866-1300</a> · Front desk 305-866-1301</p>
<p>Ana Diaz, Property Manager</p><a href="mailto:manager@oceansideplaza.org">Write us</a>
</body></html>
"""


def _place(pid, name, addr, phone, website=None, reviews=(), loc=(LAT, LON)):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": addr, "nationalPhoneNumber": phone,
            "websiteUri": website, "googleMapsUri": f"https://maps.google.com/?cid={pid}", "rating": 4.0,
            "userRatingCount": 50, "businessStatus": "OPERATIONAL", "types": ["condominium_complex"],
            "location": {"latitude": loc[0], "longitude": loc[1]},
            "reviews": [{"text": {"text": t}, "rating": 2, "relativePublishTimeDescription": "2 months ago"} for t in reviews]}


PLACES = [
    _place("A", "Oceanside Plaza Condo", "5555 Collins Ave, Miami Beach, FL 33140, USA", "(305) 866-1301",
           "https://www.oceansideplaza.org/", ["Garage has oil stains and black mold in the hallways."]),
    _place("B", "Oceania II", "16485 Collins Ave, Sunny Isles Beach, FL 33160, USA", "(305) 949-1000"),
    _place("C", "Inland Villas", "9280 SW 150th Ave, Miami, FL 33196, USA", "(305) 386-2000", loc=(25.68, -80.43)),
    _place("D", "No Number HOA", "Miami Lakes, FL 33014, USA", "(305) 820-0000"),
]


@pytest.fixture()
def wired(monkeypatch, tmp_path):
    conn = dbpr.connect(tmp_path / "dbpr.db")
    dbpr.load_csv(FIXTURE, conn)
    cam.load_csv(CAM_FIXTURE, conn)
    real_person, real_firm, real_cluster = cam.verify_person, cam.verify_firm, dbpr.cluster
    monkeypatch.setattr(pipeline.cam, "verify_person", lambda n, c=None: real_person(n, conn))
    monkeypatch.setattr(pipeline.cam, "verify_firm", lambda n, c=None: real_firm(n, conn))
    monkeypatch.setattr(pipeline.dbpr, "cluster", lambda **kw: real_cluster(**{**kw, "conn": conn}))
    monkeypatch.setattr(pipeline.appraiser, "lookup", lambda address: dict(CADASTRE_5555)
                        if address and address.startswith("5555 Collins") else None)
    monkeypatch.setattr(pipeline.lodging, "match", lambda address, conn=None: None)
    monkeypatch.setattr(pipeline.geocode, "geocode", lambda address: None)
    monkeypatch.setattr(pipeline.npi, "enrich", lambda lead: None)
    monkeypatch.setattr(pipeline.mgmt_domains, "resolve", lambda firm, **kw: {
        "domain": "castlegroup.com", "status": "mx", "source": "catalog", "firm": "Castle Group"}
        if firm and "Castle" in firm else None)
    monkeypatch.setattr(pipeline.municipal_signals, "lookup",
                        lambda address, **kw: [dict(RECERT)] if address and address.startswith("5555 Collins") else [])
    monkeypatch.setattr(google_places, "_search", lambda q, tok, with_reviews: {"places": PLACES})
    monkeypatch.setattr(pipeline, "crawl", lambda url, max_pages=5: parse_page(OCEANSIDE_SITE, url))
    real_match = dbpr.match
    monkeypatch.setattr(pipeline.dbpr, "match", lambda a, n=None, c=None: real_match(a, n, conn))
    monkeypatch.setattr(pipeline.geo, "fetch_profile",
                        lambda lat, lng: geo.profile_from_overpass(ELEMENTS, lat, lng) if lat == LAT else {})
    monkeypatch.setattr(pipeline.search, "discover",
                        lambda name, city: {**search.analyze(RESULTS, name), "query": "q", "provider": "serpapi"})
    monkeypatch.setattr(email_verify, "domain_status", lambda d, timeout=4.0: "mx")
    monkeypatch.setattr(pipeline, "Ledger", lambda: Ledger(tmp_path / "state.db"))
    return tmp_path


def test_full_run(wired, monkeypatch):
    from lead_engine import config
    s = type(config.settings)(**{**config.settings.__dict__, "google_places_api_key": "k", "crm_webhook_secret": None})
    monkeypatch.setattr(pipeline, "settings", s)
    monkeypatch.setattr(google_places, "settings", s)
    costs.tracker.reset()
    out = pipeline.run(strategy="onsite", limit=5, use_ai=False, use_apollo=False, precheck=False,
                       out_dir=wired / "out", max_candidates=4, make_pdfs=True)

    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text().splitlines()]
    payload = json.loads((out / "crm_payload.json").read_text())["leads"]
    rejected = [json.loads(l) for l in (out / "rejected.jsonl").read_text().splitlines()]
    report = json.loads((out / "quality_report.json").read_text())

    assert [r["reason"] for r in rejected] == ["dirección sin número de calle"]
    assert [l["company_name"] for l in leads][0] == "Oceanside Plaza Condo"         # best score first
    assert [l["lead_score"] for l in leads] == sorted([l["lead_score"] for l in leads], reverse=True)
    assert [p["company_name"] for p in payload] == [l["company_name"] for l in leads]

    top = leads[0]
    assert top["phone"] == "+13058661300"                          # management office beats Google's front desk
    assert top["contact_name"] == "Ana Diaz" and top["contact_title"] == "Property Manager"
    assert top["management_company"] == "Castle Management LLC"     # from the DBPR care-of line
    assert top["building"]["levels"] == 32 and top["building"]["units"] == 299 and top["building"]["coastal"]
    assert {"pool_deck", "parking_garage"} <= set(top["building"]["amenities"])
    assert top["email"] == "manager@oceansideplaza.org" and top["email_verified"]
    assert top["pain_score"] >= 3 and top["lead_score"] == 100
    assert "deck de piscina" in payload[0]["sales_strategy"] and "32 pisos" in payload[0]["sales_strategy"]
    assert "front desk +13058661301" in payload[0]["sales_strategy"]
    assert payload[0]["sales_strategy"].startswith("Registro municipal: recertificación 40 años pendiente")
    assert "techo hormigón plano" in payload[0]["sales_strategy"] and "est. US$" in payload[0]["sales_strategy"]
    assert "firma CAM licenciada CAB3838" in payload[0]["sales_strategy"]
    assert top["pain"][0]["category"] == "municipal_recert" and top["pain"][0]["source"] == "municipal"
    assert top["estimated_deal"]["high"] > top["estimated_deal"]["low"] > 10_000
    assert top["licenses"]["firm"]["license_id"] == "CAB3838"
    # Official cadastre: OSM said 32 levels, the unit numbers say 17 → OSM kept (unit numbers only fill gaps).
    assert top["cadastre"]["year_built"] == 1967 and top["building"]["year_built"] == 1967
    assert [m["category"] for m in top["municipal"]] == ["milestone", "municipal_recert"]
    assert [p["category"] for p in top["pain"]][:2] == ["municipal_recert", "milestone"]
    assert top["vertical"] == "community" and "Gran Comunidad" in payload[0]["sales_strategy"]
    assert top["contacts"][0]["name"] == "Ana Diaz" and top["contacts"][0]["role"] == "pm"
    inland = next(l for l in leads if l["company_name"] == "Inland Villas")
    assert (inland["management_company"], inland["contact_name"]) == ("Premier Assn Services", "Orly Iglesias")

    oceania = next(l for l in leads if l["company_name"] == "Oceania II")
    assert oceania["management_type"] in ("onsite", "third_party")
    assert oceania["management_company"] == "FirstService Residential"                    # search, tied to the building
    assert oceania["public_records"]["dbpr"]["units"] == 330

    assert report["accepted"] == 3 and report["pct"]["dbpr_match"] == 100.0
    assert report["pdfs"] == 3 and (out / "pdf" / "Oceanside_Plaza_Condo.pdf").exists()
    assert report["pct"]["municipal_signal"] == round(100 / 3, 1) and report["pct"]["deal_estimated"] == 100.0
    assert report["estimated_deal_usd"]["sum_high"] >= top["estimated_deal"]["high"]
    assert report["lead_score"]["max"] == 100
    assert report["cost_estimate_usd"]["by_api"]["dns"]["count"] >= 1   # Places is faked here; its counter is unit-tested



def test_dbpr_seed_run_without_places(wired, monkeypatch):
    """--seed dbpr with no Places key: discovery from the registry, phone from Places-less sources."""
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
    # OSM knows Oceania II's phone (tagged), nothing for the rest.
    monkeypatch.setattr(pipeline.osm, "lookup_address", lambda address, name=None: {
        "lat": 25.9300, "lng": -80.1210, "phone": "+13059491000", "website": None, "name": "Oceania II",
        "levels": "28", "osm_id": "way/1"} if address.startswith("16485") else None)
    assert pipeline.default_sources() == ["dbpr"]
    out = pipeline.run(strategy="onsite", limit=5, use_ai=False, use_apollo=False, precheck=False, use_search=False,
                       out_dir=wired / "out2")
    leads = [json.loads(l) for l in (out / "leads.jsonl").read_text().splitlines()]
    assert [l["company_name"] for l in leads] == ["Oceania II"]
    l = leads[0]
    assert l["source"] == "dbpr" and l["phone"] == "+13059491000" and l["management_type"] == "onsite"
    assert l["building"]["units"] == 330 and l["estimated_deal"]["low"] > 0
    assert not (out / "needs_phone.csv").exists()          # Oceania I has no care-of → not an onsite seed
    report = json.loads((out / "quality_report.json").read_text())
    assert report["sources"] == {"dbpr": 1}

    # Portfolio seed: Oceanside Plaza (Castle) qualifies but nothing gives it a phone → manual list.
    out = pipeline.run(strategy="portfolio", limit=5, use_ai=False, use_apollo=False, precheck=False, use_search=False,
                       out_dir=wired / "out3")
    assert (out / "leads.jsonl").read_text() == ""
    needs = (out / "needs_phone.csv").read_text()
    assert "Oceanside Plaza,\"5555 Collins Ave, Miami Beach, FL 33140\",299" in needs and "Castle Management LLC" in needs
