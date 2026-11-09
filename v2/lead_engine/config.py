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
    # ── deep enrichment ──
    # Places reviews (Enterprise + Atmosphere SKU) feed the pain detector for every strategy.
    places_reviews: bool = field(default_factory=lambda: _bool("PLACES_REVIEWS", True))
    geo_enabled: bool = field(default_factory=lambda: _bool("GEO_ENABLED", True))
    overpass_url: str = field(default_factory=lambda: os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"))
    render_js: str = field(default_factory=lambda: os.getenv("RENDER_JS", "auto").strip().lower())  # off|auto|always
    playwright_chromium_path: str | None = field(default_factory=lambda: os.getenv("PLAYWRIGHT_CHROMIUM_PATH") or None)
    search_provider: str = field(default_factory=lambda: os.getenv("SEARCH_PROVIDER", "auto").strip().lower())
    serpapi_key: str | None = field(default_factory=lambda: os.getenv("SERPAPI_KEY") or None)
    google_cse_key: str | None = field(default_factory=lambda: os.getenv("GOOGLE_CSE_KEY") or None)
    google_cse_cx: str | None = field(default_factory=lambda: os.getenv("GOOGLE_CSE_CX") or None)
    brave_search_key: str | None = field(default_factory=lambda: os.getenv("BRAVE_SEARCH_KEY") or None)
    dbpr_groups: list[str] = field(default_factory=lambda: _list("DBPR_GROUPS") or ["MD", "PB"])
    # Counties (DBPR spelling) that count as "the area" for portfolio clusters.
    cluster_counties: list[str] = field(default_factory=lambda: _list("CLUSTER_COUNTIES") or ["Dade", "Broward", "Palm Beach"])
    # DBPR seed: minimum units and the size that ranks first.
    min_units: int = field(default_factory=lambda: int(os.getenv("MIN_UNITS", "50")))
    priority_units: int = field(default_factory=lambda: int(os.getenv("PRIORITY_UNITS", "80")))
    municipal_signals: bool = field(default_factory=lambda: _bool("MUNICIPAL_SIGNALS", True))
    appraiser_enabled: bool = field(default_factory=lambda: _bool("APPRAISER_ENABLED", True))
    npi_enabled: bool = field(default_factory=lambda: _bool("NPI_ENABLED", True))
    # CRM segment: "pain" (Dolor Urgente / Gran Comunidad, what the CRM accepts today) or "vertical"
    # (Gran Comunidad / Shopping/Plaza Comercial / Edificio Comercial / Clínica/Hospital) once the CRM enum has them.
    # --seed pa: which vertical to pull from the cadastre (plaza | commercial | clinic)
    seed_vertical: str = field(default_factory=lambda: os.getenv("SEED_VERTICAL", "plaza").strip().lower())
    geocode_enabled: bool = field(default_factory=lambda: _bool("GEOCODE_ENABLED", True))
    # zone competitor intelligence
    competitor_radius_m: float = field(default_factory=lambda: float(os.getenv("COMPETITOR_RADIUS_M", "1500")))
    competitor_nearby: bool = field(default_factory=lambda: _bool("COMPETITOR_NEARBY", False))
    # branding for the pre-sale PDF (only what is set here is printed; nothing is invented)
    company_name: str = field(default_factory=lambda: os.getenv("COMPANY_NAME", "MB Softwash Miami"))
    company_phone: str | None = field(default_factory=lambda: os.getenv("COMPANY_PHONE") or None)
    company_email: str | None = field(default_factory=lambda: os.getenv("COMPANY_EMAIL") or None)
    company_website: str | None = field(default_factory=lambda: os.getenv("COMPANY_WEBSITE") or None)
    company_booking_url: str | None = field(default_factory=lambda: os.getenv("COMPANY_BOOKING_URL") or None)
    # Documents the company really has; the PDF only says "available upon request" for these (never "attached").
    documents_available: list[str] = field(default_factory=lambda: _list("COMPANY_DOCUMENTS") or [])
    brand_color: str = field(default_factory=lambda: os.getenv("BRAND_COLOR", "#0E5A8A"))
    pdf_lang: str = field(default_factory=lambda: os.getenv("PDF_LANG", "en").strip().lower())
    # Preferred Vendor Program tiers for portfolio dossiers: [[min_buildings, discount_pct], …] (tenant-editable)
    pvp_tiers: list = field(default_factory=lambda: _tiers(os.getenv("PVP_TIERS")) or [[3, 5], [10, 8], [25, 12]])
    # Phone cascade without Places: use the management firm's office line as last resort (labelled).
    allow_mgmt_firm_phone: bool = field(default_factory=lambda: _bool("ALLOW_MGMT_FIRM_PHONE", False))
    # Phones the tenant verified itself: {"Firm name": "+1…"} (override the website lookup)
    mgmt_phones: dict = field(default_factory=dict)
    # city -> allowed ZIPs (east/coastal corridor); records of that city outside them are out of the area
    zip_restrictions: dict = field(default_factory=dict)
    # inspector_sheet.FIELD_DEFAULTS overrides (production rates, mixes, crew, floor price, annual discount)
    field_rates: dict = field(default_factory=dict)
    # `daily` multi-vertical mix: [{"name", "share", "seed": dbpr|pa|npi, "strategy", "vertical"?}, …]
    daily_mix: list = field(default_factory=list)
    exclude_zips: list = field(default_factory=list)
    # Per-strategy city lists from a tenant profile (wins over TARGET_CITIES env)
    target_cities: dict = field(default_factory=dict)
    tenant: str | None = field(default_factory=lambda: os.getenv("TENANT") or None)
    crm_segment_mode: str = field(default_factory=lambda: os.getenv("CRM_SEGMENT_MODE", "pain").strip().lower())
    email_mx_check: bool = field(default_factory=lambda: _bool("EMAIL_MX_CHECK", True))
    crm_include_brief: bool = field(default_factory=lambda: _bool("CRM_INCLUDE_BRIEF", True))
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


def _tiers(raw: str | None) -> list[list[int]]:
    """'3:5,10:8' -> [[3, 5], [10, 8]] (minimum buildings : % off)."""
    out = []
    for part in (raw or "").split(","):
        if ":" in part:
            n, pct = part.split(":", 1)
            out.append([int(n), int(pct)])
    return out


def cities_for(strategy: str) -> list[str]:
    tenant = settings.target_cities if "settings" in globals() else {}
    return (_list(f"TARGET_CITIES_{strategy.upper()}") or tenant.get(strategy) or tenant.get("all")
            or _list("TARGET_CITIES") or _DEFAULT_CITIES[strategy])


def service_area() -> list[str]:
    """Every city the company serves: union of the per-strategy target lists (tenant profile, then .env,
    then the defaults), in order, without repeats."""
    seen, out = set(), []
    tenant = settings.target_cities or {}
    lists = list(tenant.values()) if tenant else [cities_for(s) for s in ("onsite", "portfolio", "pain")]
    for cities in lists:
        for c in cities:
            if c.upper() not in seen:
                seen.add(c.upper())
                out.append(c)
    return out


def _bbox() -> tuple[float, float, float, float]:
    raw = _list("OSM_BBOX")
    if raw and len(raw) == 4:
        s, w, n, e = (float(x) for x in raw)
        return (s, w, n, e)
    return (25.55, -80.50, 26.35, -80.05)  # Miami-Dade + Broward


# Bounding box (south, west, north, east) for OpenStreetMap.
OSM_BBOX = _bbox()

settings = Settings()
