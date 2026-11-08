"""White-label tenant profiles (`--tenant <slug>` or TENANT=<slug>).

tenants/<slug>.json overrides, without touching code:
  company: name, phone, email, website, booking_url, documents_available (["COI", "W-9", …])
  branding: brand_color (hex), pdf_lang ("en" | "es")
  targeting: target_cities {"onsite": [...], "portfolio": [...], "all": [...]}, min_units
  pricing: valuation_rates {"facade": [low, high, per_year], …}, pvp_tiers [[min_buildings, discount_pct], …]
  phones: allow_mgmt_firm_phone, mgmt_phones {"Firm": "+1…"} (numbers the tenant verified itself)
  inspectors: ["Name", …]

Values are applied to the live `settings` object (and valuation.RATES) in place, so every module sees them.
Unknown keys are rejected so a typo in a profile never fails silently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import BASE_DIR, settings

TENANTS_DIR = Path(os.getenv("TENANTS_DIR", str(BASE_DIR / "tenants")))
COMPANY_MAP = {"name": "company_name", "phone": "company_phone", "email": "company_email",
               "website": "company_website", "booking_url": "company_booking_url",
               "documents_available": "documents_available"}
SETTINGS_KEYS = {"brand_color", "pdf_lang", "target_cities", "min_units", "pvp_tiers", "allow_mgmt_firm_phone",
                 "mgmt_phones"}
ALLOWED = {"slug", "company", "valuation_rates", "inspectors", "notes"} | SETTINGS_KEYS


class TenantError(ValueError):
    pass


def path_for(slug: str) -> Path:
    if not slug.replace("_", "").replace("-", "").isalnum():
        raise TenantError(f"slug inválido: {slug!r}")
    return TENANTS_DIR / f"{slug}.json"


def load(slug: str) -> dict[str, Any]:
    p = path_for(slug)
    if not p.exists():
        raise TenantError(f"No existe el perfil {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    unknown = set(data) - ALLOWED
    if unknown:
        raise TenantError(f"Claves desconocidas en {p.name}: {sorted(unknown)}")
    bad_company = set(data.get("company", {})) - set(COMPANY_MAP)
    if bad_company:
        raise TenantError(f"Claves desconocidas en company: {sorted(bad_company)}")
    return data


def apply(data: dict[str, Any], target=None) -> None:
    from . import valuation

    target = target or settings
    for k, field_name in COMPANY_MAP.items():
        if k in data.get("company", {}):
            object.__setattr__(target, field_name, data["company"][k] or ([] if k == "documents_available" else None))
    for k in SETTINGS_KEYS & set(data):
        object.__setattr__(target, k, data[k])
    if data.get("pdf_lang") and data["pdf_lang"] not in ("en", "es"):
        raise TenantError("pdf_lang debe ser 'en' o 'es'")
    for comp, rate in (data.get("valuation_rates") or {}).items():
        if comp not in valuation.RATES or len(rate) != 3:
            raise TenantError(f"valuation_rates.{comp} inválido")
        valuation.RATES[comp] = tuple(float(x) for x in rate)
    if data.get("inspectors"):
        os.environ.setdefault("INSPECTORS", ",".join(data["inspectors"]))
    object.__setattr__(target, "tenant", data.get("slug"))


def use(slug: str | None) -> dict[str, Any] | None:
    slug = slug or os.getenv("TENANT") or None
    if not slug:
        return None
    data = load(slug)
    data.setdefault("slug", slug)
    apply(data)
    return data
