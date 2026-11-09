"""Physical profile of the property from OpenStreetMap (Overpass) + site text.

Given the Places coordinates it finds the building polygon that contains the
point (or the nearest one), and reads:
  levels      building:levels (or height / 3.2 m)
  footprint   polygon area in m² (local equirectangular projection, shoelace)
  roof        roof:material / roof:shape / roof:colour when mapped
  amenities   parking_garage, pool_deck, tennis_court, sidewalks, gatehouse
              (OSM features within AMENITY_RADIUS_M, plus mentions on the website)
  coastal     natural=coastline within COASTAL_DISTANCE_M (distance kept)
Everything except ``fetch_profile`` is pure and tested with fixtures.
"""

from __future__ import annotations

import math
import re
from typing import Any

import requests
from loguru import logger

from . import costs
from .config import settings

EARTH_R = 6_371_000.0
BUILDING_RADIUS_M = 60
AMENITY_RADIUS_M = 150
COASTAL_DISTANCE_M = 1200
LARGE_LEVELS = 8
LARGE_FOOTPRINT_M2 = 2000
LARGE_UNITS = 100

AMENITY_KEYS = ("parking_garage", "pool_deck", "tennis_court", "sidewalks", "gatehouse")
TEXT_AMENITIES = {
    "parking_garage": r"parking garage|covered parking|garage parking|valet parking garage|multi[- ]level parking|garaje|estacionamiento cubierto",
    "pool_deck": r"pool deck|swimming pool|heated pool|infinity pool|resort[- ]style pool|\bpools?\b|piscina",
    "tennis_court": r"tennis|pickleball|padel|paddle court|cancha de tenis",
    "sidewalks": r"sidewalks?|walkways?|pavers?|boardwalk|promenade|aceras|caminos peatonales",
    "gatehouse": r"gatehouse|guard ?house|guard gate|gated community|24[- /]hour security gate|garita",
}
_TEXT_RX = {k: re.compile(rf"(?i)\b(?:{v})") for k, v in TEXT_AMENITIES.items()}


# ── geometry (pure) ─────────────────────────────────────────────────────────

def _project(lat0: float, pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    k = math.cos(math.radians(lat0))
    return [(math.radians(lon) * EARTH_R * k, math.radians(lat) * EARTH_R) for lat, lon in pts]


def polygon_area_m2(pts: list[tuple[float, float]]) -> float:
    """Area of a (lat, lon) ring in m²."""
    if len(pts) < 3:
        return 0.0
    xy = _project(sum(p[0] for p in pts) / len(pts), pts)
    s = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]))
    return abs(s) / 2


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lon: float, pts: list[tuple[float, float]]) -> bool:
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i]
        yj, xj = pts[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _ring(el: dict) -> list[tuple[float, float]]:
    return [(g["lat"], g["lon"]) for g in el.get("geometry") or [] if "lat" in g]


def _center(el: dict) -> tuple[float, float] | None:
    if "lat" in el and "lon" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    if c:
        return c["lat"], c["lon"]
    ring = _ring(el)
    if ring:
        return sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    return None


def min_distance_to_line_m(lat: float, lon: float, el: dict) -> float | None:
    ring = _ring(el)
    if not ring:
        c = _center(el)
        return haversine_m(lat, lon, *c) if c else None
    return min(haversine_m(lat, lon, a, b) for a, b in ring)


def _levels(tags: dict) -> int | None:
    for key in ("building:levels", "levels"):
        raw = tags.get(key)
        if raw:
            m = re.match(r"\s*(\d+(?:\.\d+)?)", str(raw))
            if m:
                return int(float(m.group(1)))
    h = tags.get("height") or tags.get("building:height")
    if h:
        m = re.match(r"\s*(\d+(?:\.\d+)?)", str(h))
        if m:
            return max(1, round(float(m.group(1)) / 3.2))
    return None


def _amenity_of(tags: dict) -> str | None:
    if tags.get("leisure") == "swimming_pool" or tags.get("amenity") == "swimming_pool":
        return "pool_deck"
    if (tags.get("amenity") == "parking" and tags.get("parking") in ("multi-storey", "underground", "rooftop")) \
            or tags.get("building") in ("parking", "garage", "garages") or tags.get("parking") == "multi-storey":
        return "parking_garage"
    if tags.get("leisure") == "pitch" and re.search(r"tennis|pickleball|padel", tags.get("sport", "")):
        return "tennis_court"
    if tags.get("barrier") in ("gate", "lift_gate") or tags.get("building") in ("gatehouse", "guardhouse") \
            or tags.get("amenity") == "security_booth":
        return "gatehouse"
    if tags.get("footway") == "sidewalk" or tags.get("highway") == "footway":
        return "sidewalks"
    return None


def profile_from_overpass(elements: list[dict], lat: float, lon: float) -> dict[str, Any]:
    """Pick the main building and nearby washable features from Overpass JSON elements."""
    buildings, amenities, coast = [], {}, None
    for el in elements:
        tags = el.get("tags") or {}
        if tags.get("natural") == "coastline":
            d = min_distance_to_line_m(lat, lon, el)
            if d is not None and (coast is None or d < coast):
                coast = d
            continue
        kind = _amenity_of(tags)
        if kind:
            c = _center(el)
            if c and haversine_m(lat, lon, *c) <= AMENITY_RADIUS_M:
                amenities.setdefault(kind, "osm")
        if tags.get("building") and _ring(el):
            buildings.append(el)

    main = None
    for b in buildings:
        if point_in_polygon(lat, lon, _ring(b)):
            main = b
            break
    if main is None and buildings:
        near = sorted(buildings, key=lambda b: haversine_m(lat, lon, *_center(b)))
        if haversine_m(lat, lon, *_center(near[0])) <= BUILDING_RADIUS_M:
            main = near[0]

    prof: dict[str, Any] = {"amenities": amenities, "source": "osm"}
    if main:
        t = main.get("tags") or {}
        prof["osm_id"] = f"{main.get('type')}/{main.get('id')}"
        prof["levels"] = _levels(t)
        prof["footprint_m2"] = round(polygon_area_m2(_ring(main)))
        roof = " ".join(x for x in (t.get("roof:material"), t.get("roof:shape"), t.get("roof:colour")) if x)
        prof["roof"] = roof or None
        if t.get("building") not in ("yes", None):
            prof["building_type"] = t["building"]
        # Other building polygons of the same complex (towers, garages) add to the washable area.
        others = [b for b in buildings if b is not main and haversine_m(lat, lon, *_center(b)) <= BUILDING_RADIUS_M * 2]
        if others:
            prof["complex_footprint_m2"] = prof["footprint_m2"] + round(sum(polygon_area_m2(_ring(b)) for b in others))
    if coast is not None:
        prof["coast_distance_m"] = round(coast)
        prof["coastal"] = coast <= COASTAL_DISTANCE_M
    return {k: v for k, v in prof.items() if v not in (None, "")}


# ── text amenities (pure) ───────────────────────────────────────────────────

def amenities_from_text(text: str) -> dict[str, str]:
    return {k: "website" for k, rx in _TEXT_RX.items() if rx.search(text or "")}


COASTAL_HINTS = re.compile(
    r"(?i)\b(collins ave|ocean (?:dr|drive|blvd|boulevard)|a1a|gulf (?:shore|blvd)|beach(?:front)?|"
    r"oceanfront|ocean ?view|bayfront|waterfront|intracoastal|bay ?harbor|key biscayne|sunny isles|surfside|bal harbour)\b")


def merge_profile(osm: dict[str, Any], site_text: str, address: str | None, units: int | None) -> dict[str, Any]:
    prof = dict(osm or {})
    am = dict(prof.get("amenities") or {})
    for k, v in amenities_from_text(site_text).items():
        am.setdefault(k, v)
    prof["amenities"] = am
    if units:
        prof["units"] = units
    if "coastal" not in prof and COASTAL_HINTS.search(f"{address or ''} {site_text[:3000]}"):
        prof["coastal"] = True
        prof["coastal_source"] = "heuristic"
    prof["large"] = bool(
        (prof.get("levels") or 0) >= LARGE_LEVELS
        or (prof.get("complex_footprint_m2") or prof.get("footprint_m2") or 0) >= LARGE_FOOTPRINT_M2
        or (units or 0) >= LARGE_UNITS
    )
    return prof


# ── network ─────────────────────────────────────────────────────────────────

def overpass_query(lat: float, lon: float) -> str:
    b, a, c = BUILDING_RADIUS_M * 2, AMENITY_RADIUS_M, COASTAL_DISTANCE_M
    return f"""
[out:json][timeout:30];
(
  way(around:{b},{lat},{lon})["building"];
  nwr(around:{a},{lat},{lon})["leisure"="swimming_pool"];
  nwr(around:{a},{lat},{lon})["amenity"="parking"]["parking"~"multi-storey|underground|rooftop"];
  nwr(around:{a},{lat},{lon})["leisure"="pitch"]["sport"~"tennis|pickleball|padel"];
  nwr(around:{a},{lat},{lon})["barrier"~"^(gate|lift_gate)$"];
  nwr(around:{a},{lat},{lon})["amenity"="security_booth"];
  way(around:{a},{lat},{lon})["footway"="sidewalk"];
  way(around:{c},{lat},{lon})["natural"="coastline"];
);
out tags geom;
"""


def fetch_profile(lat: float | None, lon: float | None) -> dict[str, Any]:
    if lat is None or lon is None or not settings.geo_enabled:
        return {}
    from .overpass import OverpassUnavailable, query

    try:
        return profile_from_overpass(query(overpass_query(lat, lon)), lat, lon)
    except OverpassUnavailable as exc:
        logger.warning("Overpass falló para ({}, {}): {}", lat, lon, exc)
        return {}
