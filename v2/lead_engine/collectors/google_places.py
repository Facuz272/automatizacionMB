"""Google Places API (New) Text Search collector.

Needs "Places API (New)" enabled on the key (the v1 scraper used the legacy API
through the `googlemaps` package). One searchText request returns name, address,
phone, website, rating and Maps URL, so there is no per-place Details call.
Reviews (with their dates) are requested when PLACES_REVIEWS=true (default) or
for the `pain` strategy; they feed the pain detector. `location` gives the
coordinates used for the OpenStreetMap building profile.
"""

from __future__ import annotations

import re
import time
from typing import Iterator

import requests
from loguru import logger

from .. import costs
from ..config import QUERIES, cities_for, settings
from ..models import Lead
from ..normalize import clean_address, extract_domain, is_company_site, to_e164

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

BASE_FIELDS = [
    "places.id", "places.displayName", "places.formattedAddress", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber", "places.websiteUri", "places.googleMapsUri", "places.rating",
    "places.userRatingCount", "places.businessStatus", "places.types", "places.location", "nextPageToken",
]
REVIEW_FIELDS = ["places.reviews"]

# Place types that are never a condo/HOA/management office.
EXCLUDED_TYPES = {
    "lodging", "hotel", "real_estate_agency_only", "restaurant", "store", "car_dealer",
    "school", "church", "hospital", "storage", "moving_company",
}


# Google lists parts of a property as their own places (the garage, the hotel's event venue, a restaurant
# or a shop inside). When looking up a known building (cadastre / DBPR seed) those listings carry the
# phone of that business, not of the property's management, so they are never the match.
SUB_LISTING_TYPES = {
    "parking", "parking_garage", "parking_lot", "event_venue", "wedding_venue", "banquet_hall", "convention_center",
    "restaurant", "bar", "cafe", "coffee_shop", "bakery", "night_club", "meal_takeaway", "meal_delivery",
    "spa", "gym", "fitness_center", "beauty_salon", "hair_salon", "barber_shop", "nail_salon",
    "convenience_store", "grocery_store", "supermarket", "clothing_store", "department_store", "shoe_store",
    "liquor_store", "pharmacy", "drugstore", "car_wash", "car_rental", "storage", "laundry", "atm", "bank",
}
SUB_LISTING_NAME = re.compile(
    r"(?i)\b(?:parking|garage|valet|wedding|weddings|meeting|meetings|events?|venue|ballroom|banquet|conference|"
    r"restaurant|bar|grill|caf[eé]|bistro|kitchen|lounge|pizzeria|sushi|spa|salon|barber|gym|fitness|market|"
    r"shop|store|boutique|deli|pharmacy|laundry|car wash|storage|atm)\b")
LODGING_TYPES = {"lodging", "hotel", "resort_hotel", "motel", "extended_stay_hotel"}


def sub_listing_reason(place: dict, seed_name: str) -> str | None:
    """Why a Places result is a business inside / attached to the property rather than the property (pure).
    A keyword already in the seed's own name ("Brickell Parking LLC") does not count."""
    name = ((place.get("displayName") or {}).get("text") or "")
    types = set(place.get("types") or [])
    bad_types = types & SUB_LISTING_TYPES
    if bad_types and not types & {"condominium_complex", "apartment_building", "apartment_complex", "shopping_mall",
                                  "hospital", "lodging", "hotel"}:
        return "tipo " + ",".join(sorted(bad_types))
    seed_words = {w.lower() for w in re.findall(r"[A-Za-z]+", seed_name or "")}
    for m in SUB_LISTING_NAME.finditer(name):
        if m.group(0).lower() not in seed_words:
            return f"nombre «{m.group(0)}»"
    return None


class PlacesError(RuntimeError):
    pass


def _search(query: str, page_token: str | None, with_reviews: bool, page_size: int = 20) -> dict:
    if not settings.google_places_api_key:
        raise PlacesError("Falta GOOGLE_PLACES_API_KEY")
    body: dict = {"textQuery": query, "regionCode": "us", "languageCode": "en", "pageSize": page_size}
    if page_token:
        body["pageToken"] = page_token
    headers = {
        "X-Goog-Api-Key": settings.google_places_api_key,
        "X-Goog-FieldMask": ",".join(BASE_FIELDS + (REVIEW_FIELDS if with_reviews else [])),
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(SEARCH_URL, json=body, headers=headers, timeout=settings.http_timeout_s)
        except requests.RequestException as exc:
            logger.warning("Places red (intento {}): {}", attempt, exc)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            costs.tracker.add("places_text_search_reviews" if with_reviews else "places_text_search")
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        msg = r.text[:300]
        if r.status_code == 403:
            msg += " — ¿está habilitada 'Places API (New)' para esta clave?"
        raise PlacesError(f"Places HTTP {r.status_code}: {msg}")
    raise PlacesError("Places no respondió tras 3 intentos")


def _to_lead(place: dict, strategy: str, allow_lodging: bool = False) -> Lead | None:
    if place.get("businessStatus") not in (None, "OPERATIONAL"):
        return None
    types = place.get("types") or []
    excluded = EXCLUDED_TYPES - LODGING_TYPES if allow_lodging else EXCLUDED_TYPES
    if excluded.intersection(types):
        return None
    website = place.get("websiteUri")
    phone_raw = place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber")
    reviews = [
        {
            "text": ((rv.get("originalText") or rv.get("text") or {}).get("text") or "").strip(),
            "rating": rv.get("rating"),
            "published": rv.get("publishTime"),
            "relative": rv.get("relativePublishTimeDescription"),
        }
        for rv in (place.get("reviews") or [])
    ]
    loc = place.get("location") or {}
    return Lead(
        source="google_places",
        source_id=place.get("id", ""),
        strategy_kind=strategy,  # type: ignore[arg-type]
        company_name=((place.get("displayName") or {}).get("text") or "").strip(),
        phone=to_e164(phone_raw),
        address=clean_address(place.get("formattedAddress")),
        # Leads without a website are KEPT (v1 dropped them): the phone is the primary channel.
        website_url=website if is_company_site(website) else None,
        domain=extract_domain(website) if is_company_site(website) else "",
        maps_url=place.get("googleMapsUri"),
        google_rating=place.get("rating"),
        total_reviews=place.get("userRatingCount"),
        place_types=types,
        reviews=[r for r in reviews if r["text"]],
        lat=loc.get("latitude"),
        lng=loc.get("longitude"),
        phones=[{"number": to_e164(phone_raw), "label": "google_main", "source": "google_places", "context": ""}]
        if to_e164(phone_raw) else [],
        evidence={"phone_raw": phone_raw, "website_raw": website},
    )


def collect(strategy: str, max_candidates: int, cities: list[str] | None = None) -> Iterator[Lead]:
    """Yield raw candidates for a strategy, city by city, until max_candidates."""
    seen: set[str] = set()
    emitted = 0
    for city in cities or cities_for(strategy):
        for q in QUERIES[strategy]:
            query = f"{q} {city}"
            token: str | None = None
            for page in range(3):  # Google caps text search at 60 results
                data = _search(query, token, with_reviews=settings.places_reviews or strategy == "pain")
                places = data.get("places") or []
                logger.info("Places '{}' pág {}: {} resultados", query, page + 1, len(places))
                for p in places:
                    pid = p.get("id")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    lead = _to_lead(p, strategy)
                    if lead and lead.company_name:
                        yield lead
                        emitted += 1
                        if emitted >= max_candidates:
                            return
                token = data.get("nextPageToken")
                if not token:
                    break
                time.sleep(2)  # page tokens need a moment to become valid


def find_place(name: str, address: str, strategy: str, vertical: str | None = None) -> Lead | None:
    """One targeted lookup for a known building (DBPR / cadastre seed): "<name>, <address>".

    Accepts a result only when it is the property itself: same street address (number + street + ZIP)
    or, within the same ZIP, clearly the same name — and never a business inside or attached to it
    (garage, event venue, restaurant, shop… see sub_listing_reason). Same-address matches win over
    name matches. Hotels (commercial seeds) may match lodging listings. One request per building.
    """
    from ..dbpr import address_key, name_similarity, zip5

    try:
        data = _search(f"{name}, {address}", None, with_reviews=settings.places_reviews, page_size=5)
    except PlacesError as exc:
        logger.warning("Places find_place {}: {}", name, exc)
        return None
    want_key, want_zip = address_key(address), zip5(address)
    by_name: Lead | None = None
    skipped = []
    for p in data.get("places") or []:
        reason = sub_listing_reason(p, name)
        if reason:
            skipped.append(f"{(p.get('displayName') or {}).get('text')} ({reason})")
            continue
        lead = _to_lead(p, strategy, allow_lodging=vertical in ("commercial", "clinic"))
        if not lead:
            continue
        if want_key and address_key(lead.address) == want_key:
            lead.evidence["places_match"] = "address"
            if skipped:
                lead.evidence["places_skipped"] = skipped
            return lead
        if by_name is None and zip5(lead.address) == want_zip and name_similarity(name, lead.company_name) >= 0.5:
            by_name = lead
    if by_name is not None:
        by_name.evidence["places_match"] = "name"
        if skipped:
            by_name.evidence["places_skipped"] = skipped
        return by_name
    if skipped:
        logger.info("Places: sin la propiedad para {} (descartados: {})", name, "; ".join(skipped))
    return None
    want_key, want_zip = address_key(address), zip5(address)
    for p in data.get("places") or []:
        lead = _to_lead(p, strategy)
        if not lead:
            continue
        same_addr = want_key and address_key(lead.address) == want_key
        same_name = zip5(lead.address) == want_zip and name_similarity(name, lead.company_name) >= 0.5
        if same_addr or same_name:
            lead.evidence["places_match"] = "address" if same_addr else "name"
            return lead
    return None
