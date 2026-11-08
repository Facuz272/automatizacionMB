"""Free geocoding with the US Census Geocoder (public, no key; verified live 2026-11-08).

https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?address=…&benchmark=Public_AR_Current&format=json
Used when a lead has no coordinates (DBPR / cadastre / NPI seeds without Google Places) so the
building profile, the zone competitor analysis and the visit router still work. Results are
cached per address in the local state database.
"""

from __future__ import annotations

import sqlite3
from functools import lru_cache
from pathlib import Path

import requests
from loguru import logger

from . import costs
from .config import settings

URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def parse(data: dict) -> tuple[float, float] | None:
    """(lat, lng) of the first match in a Census response (pure)."""
    matches = ((data or {}).get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    c = matches[0].get("coordinates") or {}
    if "x" in c and "y" in c:
        return round(float(c["y"]), 6), round(float(c["x"]), 6)
    return None


def _db() -> sqlite3.Connection:
    p = Path(settings.state_db).with_name("geocode.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE IF NOT EXISTS geocode (address TEXT PRIMARY KEY, lat REAL, lng REAL)")
    return conn


@lru_cache(maxsize=4096)
def geocode(address: str | None) -> tuple[float, float] | None:
    if not address or not settings.geocode_enabled:
        return None
    key = " ".join(address.upper().split())
    try:
        conn = _db()
        row = conn.execute("SELECT lat, lng FROM geocode WHERE address = ?", (key,)).fetchone()
        if row:
            return (row[0], row[1]) if row[0] is not None else None
    except sqlite3.Error:
        conn = None
    try:
        r = requests.get(URL, params={"address": address, "benchmark": "Public_AR_Current", "format": "json"},
                         timeout=25, headers={"User-Agent": settings.user_agent})
        costs.tracker.add("census_geocoder")
        r.raise_for_status()
        out = parse(r.json())
    except (requests.RequestException, ValueError) as exc:
        logger.debug("Census geocoder {}: {}", address, exc)
        return None
    if conn is not None:
        conn.execute("INSERT OR REPLACE INTO geocode VALUES (?,?,?)", (key, *(out or (None, None))))
        conn.commit()
    return out
