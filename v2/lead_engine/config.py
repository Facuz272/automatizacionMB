"""Runtime configuration (env vars) and the search space per strategy.

Every knob that costs money (Places queries, Apollo credits, Claude calls) lives
here so a dry run of 20 leads can be bounded from the command line.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


def _list(name: str) -> list[str] | None:
    """Comma-separated env list ('Miami Beach, Aventura') or None when unset/empty."""
    items = [x.strip() for x in (os.getenv(name) or "").split(",") if x.strip()]
    return items or None


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    google_places_api_key: str | None = field(default_factory=lambda: os.getenv("GOOGLE_PLACES_API_KEY"))
    apollo_api_key: str | None = field(default_factory=lambda: os.getenv("APOLLO_API_KEY"))
    # Apollo masks emails until a credit is spent; off by default so a test run costs 0 credits.
    apollo_reveal_emails: bool = field(default_factory=lambda: _bool("APOLLO_REVEAL_EMAILS", False))
    anthropic_api_key: str | None = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"))
    anthropic_effort: str = field(default_factory=lambda: os.getenv("ANTHROPIC_EFFORT", "low"))
    crm_base_url: str = field(
        default_factory=lambda: os.getenv("CRM_BASE_URL", "https://mbguiaclientes.lovable.app").rstrip("/")
    )
    crm_webhook_secret: str | None = field(default_factory=lambda: os.getenv("CRM_WEBHOOK_SECRET"))
    # Two-letter state an address must be in ('' disables the check).
    target_state: str = field(default_factory=lambda: os.getenv("TARGET_STATE", "FL").strip().upper())
    out_dir: Path = field(default_factory=lambda: Path(os.getenv("LEAD_ENGINE_OUT", str(BASE_DIR / "out"))))
    state_db: Path = field(default_factory=lambda: Path(os.getenv("LEAD_ENGINE_DB", str(BASE_DIR / "data" / "state.db"))))
    http_timeout_s: float = field(default_factory=lambda: float(os.getenv("HTTP_TIMEOUT_S", "12")))
    user_agent: str = field(
        default_factory=lambda: os.getenv(
            "LEAD_ENGINE_UA", "Mozilla/5.0 (compatible; MBLeadEngine/2.0; +https://mbsoftwashmiami.com)"
        )
    )


# Defaults below are for South Florida. Override per run without code changes:
#   TARGET_CITIES=Miami Beach,Aventura          (all strategies)
#   TARGET_CITIES_ONSITE=… / _PAIN=… / _PORTFOLIO=…  (one strategy; wins over TARGET_CITIES)
#   OSM_BBOX=south,west,north,east
# Coastal / dense Miami-Dade + Broward markets where condos with an on-site
# management office are common. Order matters: the first cities are queried
# first, so a --limit 20 run stays in the highest-converting area.
ONSITE_CITIES = [
    "Miami Beach", "Sunny Isles Beach", "Aventura", "Brickell Miami", "Key Biscayne",
    "Bal Harbour", "North Miami Beach", "Hallandale Beach", "Hollywood FL", "Fort Lauderdale",
]
PORTFOLIO_CITIES = ["Miami", "Doral", "Coral Gables", "Aventura", "Fort Lauderdale", "Hialeah"]

# strategy_kind -> Google text queries ("{query} {city}").
QUERIES: dict[str, list[str]] = {
    # Condo / HOA buildings themselves: the phone is usually the management office.
    "onsite": [
        "condominium association",
        "condo building management office",
        "homeowners association",
        "residential tower condominium",
    ],
    # Third-party management firms (CAM companies) that control several buildings.
    "portfolio": [
        "community association management company",
        "condominium management company",
        "HOA management company",
    ],
    # Same buildings as onsite, but we fetch reviews and keep only the ones that
    # complain about dirt, mold, stains, pressure washing… (see qualify.PAIN_TERMS).
    "pain": [
        "condominium",
        "apartment building",
        "homeowners association",
    ],
}

_DEFAULT_CITIES: dict[str, list[str]] = {
    "onsite": ONSITE_CITIES,
    "pain": ONSITE_CITIES,
    "portfolio": PORTFOLIO_CITIES,
}


def cities_for(strategy: str) -> list[str]:
    return _list(f"TARGET_CITIES_{strategy.upper()}") or _list("TARGET_CITIES") or _DEFAULT_CITIES[strategy]


def _bbox() -> tuple[float, float, float, float]:
    raw = _list("OSM_BBOX")
    if raw and len(raw) == 4:
        s, w, n, e = (float(x) for x in raw)
        return (s, w, n, e)
    return (25.55, -80.50, 26.35, -80.05)  # Miami-Dade + Broward


# Bounding box (south, west, north, east) for OpenStreetMap.
OSM_BBOX = _bbox()

settings = Settings()
