"""Whole run offline: fake Places + sites + Overpass + DBPR (real rows) + search + DNS.

Checks the gates, the labelled primary phone, DBPR/search attribution, the
physical brief, MX filtering, ordering by lead_score and quality_report.json.
"""

import json
from pathlib import Path

import pytest

from lead_engine import costs, dbpr, email_verify, geo, pipeline, search
from lead_engine.collectors import google_places
from lead_engine.state import Ledger
from lead_engine.web_extract import parse_page

from test_geo import ELEMENTS, LAT, LON
from test_search import RESULTS

FIXTURE = Path(__file__).parent / "fixtures" / "dbpr_condo_md_sample.csv"

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
                       out_dir=wired / "out", max_candidates=4)

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
    assert "hace 2 meses" in payload[0]["sales_strategy"] and "techo hormigón plano" in payload[0]["sales_strategy"]
    inland = next(l for l in leads if l["company_name"] == "Inland Villas")
    assert (inland["management_company"], inland["contact_name"]) == ("Premier Assn Services", "Orly Iglesias")

    oceania = next(l for l in leads if l["company_name"] == "Oceania II")
    assert oceania["management_type"] in ("onsite", "third_party")
    assert oceania["management_company"] == "FirstService Residential"                    # search, tied to the building
    assert oceania["public_records"]["dbpr"]["units"] == 330

    assert report["accepted"] == 3 and report["pct"]["dbpr_match"] == 100.0
    assert report["lead_score"]["max"] == 100
    assert report["cost_estimate_usd"]["by_api"]["dns"]["count"] >= 1   # Places is faked here; its counter is unit-tested
