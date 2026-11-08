"""Deterministic estimate of the annual exterior-cleaning contract (USD range).

Inputs come from the physical profile (OSM + site text) and the DBPR registry:
levels, footprint (m²), units, amenities and coastal exposure. Each component is
area (ft²) × price per ft² per service (low/high) × services per year:

  facade      perimeter ≈ 4·√footprint, height ≈ 3.0 m/level → wall area; soft wash,
              1×/yr inland, 1.5×/yr coastal (salt); ×1.3 rope access from 8 floors
  walkways    2,000 ft² + 20 ft²/unit, 4×/yr (always present on a condo)
  garage      250 ft²/unit of parking decks, 2×/yr        (parking_garage)
  pool deck   3,000–6,000 ft² by size, 4×/yr               (pool_deck)
  courts      7,200 ft² per court, 2×/yr                   (tennis_court)
  gatehouse   flat USD 150–300 per service, 4×/yr          (gatehouse)

When the footprint is unknown it is derived from units (≈95 m² gross per unit,
spread over the known levels, or levels ≈ units/12 capped at 20 — large unit
counts are usually several towers, not one 100-storey building). Without footprint and units
there is no estimate (None). Rates are list-price assumptions for South Florida
softwash/pressure cleaning, kept here in one table so they can be tuned with the
first real quotes; the output is a range, not a quote.
"""

from __future__ import annotations

import math
from typing import Any

M2_TO_FT2 = 10.7639
FLOOR_HEIGHT_M = 3.0
GROSS_M2_PER_UNIT = 95.0
MAX_ASSUMED_LEVELS = 20

# component -> (low $/ft², high $/ft², services per year)
RATES: dict[str, tuple[float, float, float]] = {
    "facade": (0.12, 0.22, 1.0),
    "walkways": (0.10, 0.18, 4.0),
    "garage": (0.08, 0.15, 2.0),
    "pool_deck": (0.15, 0.25, 4.0),
    "courts": (0.10, 0.18, 2.0),
}
COASTAL_FACADE_FREQ = 1.5
ROPE_ACCESS_LEVELS = 8
ROPE_ACCESS_FACTOR = 1.3
GATEHOUSE_FLAT = (150.0, 300.0, 4.0)


def _geometry(building: dict[str, Any]) -> tuple[int | None, float | None]:
    """(levels, footprint m²) with fallbacks from units."""
    levels = building.get("levels")
    footprint = building.get("footprint_m2")
    units = building.get("units")
    if not footprint and units:
        lv = levels or min(MAX_ASSUMED_LEVELS, max(1, math.ceil(units / 12)))
        footprint = units * GROSS_M2_PER_UNIT / lv
        levels = levels or lv
    if footprint and not levels:
        levels = max(1, round((units or 0) * GROSS_M2_PER_UNIT / footprint)) if units else 3
    return levels, footprint


def estimate(building: dict[str, Any]) -> dict[str, Any] | None:
    """{"low", "high", "components": {...}, "basis": {...}} in USD per year, or None."""
    levels, footprint = _geometry(building or {})
    if not footprint or not levels:
        return None
    units = (building or {}).get("units") or 0
    amenities = (building or {}).get("amenities") or {}
    coastal = bool((building or {}).get("coastal"))
    comps: dict[str, tuple[float, float]] = {}

    perimeter = 4 * math.sqrt(footprint)
    facade_ft2 = perimeter * levels * FLOOR_HEIGHT_M * M2_TO_FT2
    lo, hi, freq = RATES["facade"]
    freq = COASTAL_FACADE_FREQ if coastal else freq
    rope = ROPE_ACCESS_FACTOR if levels >= ROPE_ACCESS_LEVELS else 1.0
    comps["facade"] = (facade_ft2 * lo * freq * rope, facade_ft2 * hi * freq * rope)

    walk_ft2 = 2000 + 20 * units
    lo, hi, freq = RATES["walkways"]
    comps["walkways"] = (walk_ft2 * lo * freq, walk_ft2 * hi * freq)

    if "parking_garage" in amenities:
        g_ft2 = 250 * (units or max(20, footprint * M2_TO_FT2 / 400))
        lo, hi, freq = RATES["garage"]
        comps["garage"] = (g_ft2 * lo * freq, g_ft2 * hi * freq)
    if "pool_deck" in amenities:
        p_ft2 = 6000 if units >= 150 or levels >= 15 else 3000
        lo, hi, freq = RATES["pool_deck"]
        comps["pool_deck"] = (p_ft2 * lo * freq, p_ft2 * hi * freq)
    if "tennis_court" in amenities:
        lo, hi, freq = RATES["courts"]
        comps["courts"] = (7200 * lo * freq, 7200 * hi * freq)
    if "gatehouse" in amenities:
        lo, hi, freq = GATEHOUSE_FLAT
        comps["gatehouse"] = (lo * freq, hi * freq)

    low = sum(c[0] for c in comps.values())
    high = sum(c[1] for c in comps.values())
    return {
        "low": int(round(low, -2)),
        "high": int(round(high, -2)),
        "components": {k: [int(round(v[0], -1)), int(round(v[1], -1))] for k, v in comps.items()},
        "basis": {"levels": levels, "footprint_m2": round(footprint), "units": units or None, "coastal": coastal,
                  "facade_ft2": round(facade_ft2), "rope_access": rope > 1,
                  "footprint_source": "osm" if (building or {}).get("footprint_m2") else "units"},
    }


def fmt_usd_k(n: int) -> str:
    """4500 -> '4.5k', 72000 -> '72k', 900 -> '0.9k'."""
    k = n / 1000
    return f"{k:.1f}k".replace(".0k", "k") if k < 10 else f"{round(k)}k"


def label(est: dict[str, Any] | None) -> str | None:
    if not est:
        return None
    return f"est. US$ {fmt_usd_k(est['low'])}–{fmt_usd_k(est['high'])}/año"
