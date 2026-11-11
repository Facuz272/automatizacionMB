"""find_place: a cadastre / DBPR building matches the property, never a business inside it (no network)."""

import pytest

from lead_engine.collectors import google_places as gp


def _p(name, addr, phone="(954) 927-7879", types=("establishment",), pid=None):
    return {"id": pid or name, "displayName": {"text": name}, "formattedAddress": addr, "nationalPhoneNumber": phone,
            "types": list(types), "businessStatus": "OPERATIONAL", "location": {"latitude": 26.0, "longitude": -80.1}}


@pytest.mark.parametrize("place,seed,reason", [
    (_p("1740 Polk St Parking", "1740 Polk St, Hollywood, FL 33020", types=["parking"]), "Hc Real Property LLC",
     "tipo parking"),
    (_p("Margaritaville Hollywood Beach Wedding and Meeting Venue", "1111 N Ocean Dr, Hollywood, FL 33019",
        types=["event_venue"]), "Margaritaville Hollywood Beach Resort", "tipo event_venue"),
    (_p("Sea Grill", "1111 N Ocean Dr, Hollywood, FL 33019", types=["point_of_interest"]), "Ocean Tower", "nombre «Grill»"),
    (_p("Ocean Tower Garage", "1 Ocean Dr, Miami Beach, FL 33139"), "Ocean Tower", "nombre «Garage»"),
    (_p("Ocean Tower", "1 Ocean Dr, Miami Beach, FL 33139", types=["condominium_complex"]), "Ocean Tower", None),
    (_p("Brickell Parking Plaza", "1 Brickell Ave, Miami, FL 33131"), "Brickell Parking Plaza LLC", None),  # its own name
    (_p("The Galleria at Fort Lauderdale", "2414 E Sunrise Blvd, Fort Lauderdale, FL 33304",
        types=["shopping_mall", "restaurant"]), "Keystone-florida Property Hold", None),          # the mall itself
])
def test_sub_listing_reason(place, seed, reason):
    assert gp.sub_listing_reason(place, seed) == reason


def _search_returns(monkeypatch, places):
    monkeypatch.setattr(gp, "_search", lambda q, tok, with_reviews, page_size=20: {"places": places})


def test_property_wins_over_its_garage_and_venue(monkeypatch):
    _search_returns(monkeypatch, [
        _p("1740 Polk St Parking", "1740 Polk St, Hollywood, FL 33020, USA", types=["parking"]),
        _p("Wedding Venue at Polk", "1740 Polk St, Hollywood, FL 33020, USA", types=["event_venue"]),
        _p("Hollywood Circle Apartments", "1740 Polk St, Hollywood, FL 33020, USA", phone="(954) 920-1111",
           types=["apartment_complex"])])
    lead = gp.find_place("Hc Real Property LLC", "1740 Polk St, Hollywood, FL 33020", "onsite", vertical="plaza")
    assert lead.company_name == "Hollywood Circle Apartments" and lead.phone == "+19549201111"
    assert lead.evidence["places_match"] == "address" and len(lead.evidence["places_skipped"]) == 2


def test_only_sub_listings_means_no_match(monkeypatch):
    _search_returns(monkeypatch, [_p("1740 Polk St Parking", "1740 Polk St, Hollywood, FL 33020, USA", types=["parking"])])
    assert gp.find_place("Hc Real Property LLC", "1740 Polk St, Hollywood, FL 33020", "onsite", vertical="plaza") is None


def test_hotel_seed_matches_the_hotel_not_its_event_venue(monkeypatch):
    _search_returns(monkeypatch, [
        _p("Margaritaville Hollywood Beach Wedding and Meeting Venue", "1111 N Ocean Dr, Hollywood, FL 33019, USA",
           types=["event_venue"]),
        _p("Margaritaville Hollywood Beach Resort", "1111 N Ocean Dr, Hollywood, FL 33019, USA", phone="(954) 874-4444",
           types=["lodging", "resort_hotel"])])
    hotel = gp.find_place("Margaritaville Hollywood Beach Resort", "1111 N Ocean Dr, Hollywood, FL 33019", "onsite",
                          vertical="commercial")
    assert hotel.company_name == "Margaritaville Hollywood Beach Resort"
    # condo seeds keep excluding hotel listings (a hotel at the address is not the condo association)
    assert gp.find_place("Some Condo", "1111 N Ocean Dr, Hollywood, FL 33019", "onsite", vertical="community") is None


def test_same_address_beats_a_name_match(monkeypatch):
    _search_returns(monkeypatch, [
        _p("Ocean Tower Condo", "9 Other St, Miami Beach, FL 33139, USA", phone="(305) 111-2222"),
        _p("Ocean Tower", "1 Ocean Dr, Miami Beach, FL 33139, USA", phone="(305) 333-4444",
           types=["condominium_complex"])])
    lead = gp.find_place("Ocean Tower Condo", "1 Ocean Dr, Miami Beach, FL 33139", "onsite")
    assert lead.phone == "+13053334444" and lead.evidence["places_match"] == "address"
