"""Google Places API (New) Text Search collector.

Needs "Places API (New)" enabled on the key (the v1 scraper used the legacy API
through the `googlemaps` package). One searchText request returns name, address,
phone, website, rating and Maps URL, so there is no per-place Details call.
Reviews are requested only for the `pain` strategy (more expensive SKU).
"""

from __future__ import annotations

import time
from typing import Iterator

import requests
from loguru import logger

from ..config import QUERIES, cities_for, settings
from ..models import Lead
from ..normalize import clean_address, extract_domain, is_company_site, to_e164

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

BASE_FIELDS = [
    "places.id", "places.displayName", "places.formattedAddress", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber", "places.websiteUri", "places.googleMapsUri", "places.rating",
    "places.userRatingCount", "places.businessStatus", "places.types", "nextPageToken",
]
REVIEW_FIELDS = ["places.reviews"]

# Place types that are never a condo/HOA/management office.
EXCLUDED_TYPES = {
    "lodging", "hotel", "real_estate_agency_only", "restaurant", "store", "car_dealer",
    "school", "church", "hospital", "storage", "moving_company",
}


class PlacesError(RuntimeError):
    pass


def _search(query: str, page_token: str | None, with_reviews: bool) -> dict:
    if not settings.google_places_api_key:
        raise PlacesError("Falta GOOGLE_PLACES_API_KEY")
    body: dict = {"textQuery": query, "regionCode": "us", "languageCode": "en", "pageSize": 20}
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
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** attempt)
            continue
        msg = r.text[:300]
        if r.status_code == 403:
            msg += " — ¿está habilitada 'Places API (New)' para esta clave?"
        raise PlacesError(f"Places HTTP {r.status_code}: {msg}")
    raise PlacesError("Places no respondió tras 3 intentos")


def _to_lead(place: dict, strategy: str) -> Lead | None:
    if place.get("businessStatus") not in (None, "OPERATIONAL"):
        return None
    types = place.get("types") or []
    if EXCLUDED_TYPES.intersection(types):
        return None
    website = place.get("websiteUri")
    phone_raw = place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber")
    reviews = [
        ((rv.get("text") or {}).get("text") or "").strip()
        for rv in (place.get("reviews") or [])
    ]
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
        reviews=[r for r in reviews if r],
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
                data = _search(query, token, with_reviews=strategy == "pain")
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
