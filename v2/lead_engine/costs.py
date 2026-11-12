"""Per-run API usage counters and an estimated cost (USD).

Prices are list prices at the time of writing and can be overridden with
PRICE_<KEY> env vars (e.g. PRICE_SERPAPI=0.01). They are an estimate for the
quality report, not an invoice.
"""

from __future__ import annotations

import os
from collections import Counter
from threading import Lock

# USD per unit.
DEFAULT_PRICES = {
    "places_text_search": 0.035,           # Places API (New) Text Search, Enterprise fields (phone/website)
    "places_text_search_reviews": 0.040,   # + reviews (Enterprise + Atmosphere)
    "places_nearby_reviews": 0.040,        # Nearby Search with reviews (Enterprise + Atmosphere), zone hook
    "claude_input_token": 2.0 / 1_000_000,   # Claude Sonnet 5.5
    "claude_output_token": 10.0 / 1_000_000,
    "serpapi": 0.015,
    "google_cse": 0.005,
    "brave": 0.005,
    "apollo_search": 0.0,                  # plan credits, counted not priced
    "apollo_reveal": 0.0,
    "overpass": 0.0,
    "playwright_render": 0.0,
    "dns": 0.0,
}


class CostTracker:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._lock = Lock()

    def reset(self) -> None:
        with self._lock:
            self.counts.clear()

    def add(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.counts[key] += n

    def price(self, key: str) -> float:
        raw = os.getenv(f"PRICE_{key.upper()}")
        try:
            return float(raw) if raw else DEFAULT_PRICES.get(key, 0.0)
        except ValueError:
            return DEFAULT_PRICES.get(key, 0.0)

    def report(self) -> dict:
        lines = {k: {"count": v, "usd": round(v * self.price(k), 4)} for k, v in sorted(self.counts.items())}
        return {"by_api": lines, "total_usd": round(sum(x["usd"] for x in lines.values()), 4)}


tracker = CostTracker()
