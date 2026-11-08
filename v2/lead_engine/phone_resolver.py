"""Free phone cascade for registry seeds (DBPR / cadastre) when there is no Google Places key.

Order (first hit wins; each phone keeps a label the setter sees in the Ficha):
  1. DBPR lodging licence at the address: hotel/apartment operator (front desk) or a condo rental
     licence held by the association itself (association office).
  2. OpenStreetMap `phone` / `contact:phone` tags on the building (done by pipeline.resolve_seed).
  3. The building's own website, when one is known (crawl in the pipeline gate).
  4. Last resort, only with ALLOW_MGMT_FIRM_PHONE=true: the management firm's office line —
     a number the tenant verified (tenant.mgmt_phones) or the one published on the firm's official
     site (catalog domain in mgmt_domains), preferring South-Florida area codes. Labelled
     "central administradora". The CRM deduplicates by phone, so one firm number can enter the CRM
     only once: further buildings of the same firm go to needs_phone.csv with that reason.
Nothing is typed in from memory: firm numbers are read from the firm's site (URL kept) or given by the tenant.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from loguru import logger

from . import lodging, mgmt_domains
from .config import settings
from .models import Lead
from .names import compact_key
from .normalize import to_e164

SOFLA_AREA = ("305", "786", "954", "754", "561")
FL_CONTEXT = re.compile(r"(?i)\b(?:miami|fort lauderdale|ft\.? lauderdale|boca raton|broward|dade|palm beach|"
                        r"florida|\bFL\b|hollywood|aventura|doral|coral gables|plantation|sunrise|weston)")


def _db() -> sqlite3.Connection:
    p = Path(settings.state_db).with_name("firm_phones.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE IF NOT EXISTS firm_phones (domain TEXT PRIMARY KEY, phone TEXT, context TEXT, url TEXT,"
                 " fetched_at TEXT)")
    return conn


def rank_phone(p: dict[str, Any]) -> tuple:
    """Lower is better: South-Florida number with Florida context, then any local number, then toll-free."""
    digits = p["number"][2:]
    area = digits[:3]
    toll_free = area in ("800", "833", "844", "855", "866", "877", "888")
    return (p.get("label") in ("fax", "leasing"), not (area in SOFLA_AREA), not FL_CONTEXT.search(p.get("context") or ""),
            toll_free)


def pick_firm_phone(phones: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [p for p in phones if p.get("label") not in ("fax", "leasing")]
    return sorted(usable, key=rank_phone)[0] if usable else None


@lru_cache(maxsize=64)
def firm_site_phone(domain: str) -> dict[str, Any] | None:
    """Office phone published on the firm's official site (cached locally)."""
    try:
        conn = _db()
        row = conn.execute("SELECT phone, context, url FROM firm_phones WHERE domain = ?", (domain,)).fetchone()
        if row:
            return {"number": row[0], "context": row[1], "url": row[2]} if row[0] else None
    except sqlite3.Error:
        conn = None
    from .web_extract import crawl

    facts = crawl(f"https://{domain}", max_pages=4)
    best = pick_firm_phone(facts.phones)
    if conn is not None:
        conn.execute("INSERT OR REPLACE INTO firm_phones VALUES (?,?,?,?,?)",
                     (domain, best and best["number"], best and best.get("context"), facts.pages[0] if facts.pages else None,
                      datetime.now(timezone.utc).isoformat()))
        conn.commit()
    if not best:
        logger.info("Sin teléfono publicado en {}", domain)
        return None
    return {"number": best["number"], "context": best.get("context"), "url": facts.pages[0] if facts.pages else domain}


def firm_phone(firm: str | None) -> dict[str, Any] | None:
    if not firm:
        return None
    for name, number in (settings.mgmt_phones or {}).items():
        if compact_key(name) == compact_key(firm) and to_e164(number):
            return {"number": to_e164(number), "source": "tenant", "firm": name}
    hit = mgmt_domains.from_catalog(firm)
    if not hit:
        return None
    site = firm_site_phone(hit[1])
    if not site:
        return None
    return {"number": site["number"], "source": "firm_website", "firm": hit[0], "url": site["url"],
            "context": site.get("context")}


def resolve(lead: Lead) -> str | None:
    """Fill lead.phone from the cascade; returns the step that found it (lodging | firm_central) or None."""
    if lead.phone:
        return None
    dbpr_rec = lead.public_records.get("dbpr") or {}
    hit = lodging.building_phone(lead.address, [lead.company_name, dbpr_rec.get("association"), dbpr_rec.get("condo_name")])
    if hit:
        lead.phone = hit["phone"]
        lead.phones.append({"number": hit["phone"], "label": hit["label"], "source": "dbpr_lodging",
                            "context": f"{hit['why']} ({hit['licensee']}, {hit['license_number']})"})
        lead.evidence["phone_from"] = "dbpr_lodging"
        return "lodging"
    if settings.allow_mgmt_firm_phone and lead.management_company:
        fp = firm_phone(lead.management_company)
        if fp:
            lead.phone = fp["number"]
            lead.phones.append({"number": fp["number"], "label": "firm_central", "source": fp["source"],
                                "context": f"{fp['firm']} · {fp.get('url') or 'perfil del tenant'}"})
            lead.evidence["phone_from"] = "firm_central"
            return "firm_central"
    return None
