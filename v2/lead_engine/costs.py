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
    "places_text_search_ids": 0.0,         # Text Search Essentials (IDs Only): no charge, no monthly cap
    "places_details_enterprise": 0.020,    # Place Details Enterprise (name/address/types/phone/website, no reviews)
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


# Google Maps Platform free usage per SKU and billing month (pricing since March 2025): what a key can call
# each month before anything is billed. Used for the "net" cost; budgets always use list prices.
FREE_MONTHLY = {
    "places_text_search": 1000,            # Text Search Enterprise
    "places_text_search_reviews": 1000,    # Text Search Enterprise + Atmosphere
    "places_nearby_reviews": 1000,         # Nearby Search Enterprise + Atmosphere
    "places_details_enterprise": 1000,     # Place Details Enterprise
}


class BudgetExceeded(RuntimeError):
    pass


class CostTracker:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._lock = Lock()
        self.budget_usd: float | None = None      # list-price ceiling for the current run (None = no ceiling)

    def total(self) -> float:
        return round(sum(v * self.price(k) for k, v in self.counts.items()), 4)

    def over_budget(self) -> bool:
        return self.budget_usd is not None and self.total() >= self.budget_usd

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


def net_of_free_tier(counts: dict[str, int], used_before: dict[str, int] | None = None) -> float:
    """USD after the monthly free usage: `used_before` = calls already made this billing month per key."""
    used_before = used_before or {}
    total = 0.0
    for key, n in counts.items():
        free_left = max(0, FREE_MONTHLY.get(key, 0) - used_before.get(key, 0))
        total += max(0, n - free_left) * tracker.price(key)
    return round(total, 4)
