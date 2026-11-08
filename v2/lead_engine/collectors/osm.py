"""OpenStreetMap (Overpass) collector — free, no key.

Finds named residential buildings / condos with a full street address in the
Miami-Dade + Broward bbox. Most OSM buildings have no phone; those with a
website get the phone from the site in the web-extraction step, the rest are
dropped by the contactability gate. Useful to discover buildings that Google
lists poorly (no category, no reviews).
"""

from __future__ import annotations

import re
from typing import Iterator

import requests
from loguru import logger

from ..config import OSM_BBOX, settings
from ..models import Lead
from ..normalize import extract_domain, is_company_site, to_e164

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def _query(limit: int) -> str:
    s, w, n, e = OSM_BBOX
    bbox = f"({s},{w},{n},{e})"
    sel = '["name"]["addr:housenumber"]["addr:street"]'
    return f"""
[out:json][timeout:60];
(
  nwr["building"="apartments"]{sel}["website"]{bbox};
  nwr["building"="apartments"]{sel}["phone"]{bbox};
  nwr["building"="apartments"]{sel}["contact:phone"]{bbox};
  nwr["residential"="condominium"]{sel}{bbox};
  nwr["landuse"="residential"]["name"~"Condo|HOA|Homeowners|Association",i]["website"]{bbox};
);
out tags center {limit};
"""


def _address(t: dict) -> str:
    street = f"{t.get('addr:housenumber', '')} {t.get('addr:street', '')}".strip()
    city = t.get("addr:city", "")
    state = t.get("addr:state", settings.target_state)
    zipc = t.get("addr:postcode", "")
    tail = " ".join(x for x in (state, zipc) if x)
    return ", ".join(x for x in (street, city, tail) if x)


def collect(strategy: str, max_candidates: int) -> Iterator[Lead]:
    try:
        r = requests.post(
            OVERPASS_URL, data={"data": _query(max_candidates * 4)},
            headers={"User-Agent": settings.user_agent}, timeout=90,
        )
        r.raise_for_status()
        elements = r.json().get("elements", [])
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Overpass falló: {}", exc)
        return
    emitted = 0
    for el in elements:
        t = el.get("tags") or {}
        website = t.get("website") or t.get("contact:website")
        phone = to_e164(t.get("phone") or t.get("contact:phone"))
        if not phone and not is_company_site(website):
            continue  # nothing to call and nowhere to find a phone
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        yield Lead(
            source="osm",
            source_id=f"{el.get('type')}/{el.get('id')}",
            strategy_kind=strategy,  # type: ignore[arg-type]
            company_name=t["name"].strip(),
            phone=phone,
            address=_address(t),
            website_url=website if is_company_site(website) else None,
            domain=extract_domain(website) if is_company_site(website) else "",
            maps_url=f"https://www.google.com/maps/search/?api=1&query={lat},{lon}" if lat and lon else None,
            lat=lat,
            lng=lon,
            phones=[{"number": phone, "label": "unlabeled", "source": "osm", "context": ""}] if phone else [],
            evidence={"osm_tags": {k: v for k, v in t.items() if k.startswith(("addr:", "building", "operator"))}},
            management_company=t.get("operator"),
        )
        emitted += 1
        if emitted >= max_candidates:
            return


def address_query(number: str, street_core: str, bbox: tuple[float, float, float, float]) -> str:
    """Overpass: features tagged with this house number on a street whose name contains the core word."""
    s, w, n, e = bbox
    core = street_core.split()[-1] if street_core else ""
    core_rx = re.escape(core.title()) if not core.isdigit() else rf"\b{core}"
    return f"""
[out:json][timeout:30];
nwr["addr:housenumber"="{number}"]["addr:street"~"{core_rx}",i]({s},{w},{n},{e});
out tags center 10;
"""


def pick_address_match(elements: list[dict], name: str | None = None) -> dict | None:
    """Best OSM element for a building: prefers one with phone/website, then a building/name."""
    def rank(el: dict) -> tuple:
        t = el.get("tags") or {}
        return (not (t.get("phone") or t.get("contact:phone")), not (t.get("website") or t.get("contact:website")),
                not t.get("building"), not t.get("name"))
    els = [e for e in elements if e.get("tags")]
    if not els:
        return None
    el = sorted(els, key=rank)[0]
    t = el["tags"]
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    website = t.get("website") or t.get("contact:website")
    return {
        "lat": lat, "lng": lon, "name": t.get("name"),
        "phone": to_e164(t.get("phone") or t.get("contact:phone")),
        "website": website if is_company_site(website) else None,
        "levels": t.get("building:levels"), "osm_id": f"{el.get('type')}/{el.get('id')}",
    }


def lookup_address(address: str | None, name: str | None = None) -> dict | None:
    """Coordinates (and phone/website when mapped) for a street address in OSM_BBOX."""
    from ..municipal_signals import street_parts

    parts = street_parts(address)
    if not parts:
        return None
    try:
        r = requests.post(OVERPASS_URL, data={"data": address_query(parts[0], parts[1], OSM_BBOX)},
                          headers={"User-Agent": settings.user_agent}, timeout=45)
        r.raise_for_status()
        return pick_address_match(r.json().get("elements", []), name)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Overpass (dirección) falló para {}: {}", address, exc)
        return None
