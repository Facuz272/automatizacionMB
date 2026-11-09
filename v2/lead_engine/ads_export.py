"""B2B audience for Meta Custom Audiences / Google Customer Match / LinkedIn (`python -m lead_engine export-ads`).

One CSV, `ads_audience.csv`, with the identifier columns the three platforms accept:

  email, phone (E.164), fn, ln, ct, st, zip, country, company, value

normalised the way Meta asks before hashing (lower case, names without punctuation, city without spaces,
2-letter state and country, 5-digit ZIP); the platforms hash the file in their own upload tools. `value`
is the midpoint of the estimated annual contract (`estimated_deal` low/high) for value-based audiences.

Sources:
  * a batch `leads.jsonl` (best match rate: verified emails and labelled phones of the decision maker);
  * the local DBPR registry of the service area (no email/phone: the registered contact person and the
    building ZIP, matched by name + location only — lower match rate).

Only records inside the tenant's service area (target cities, ZIP aliases, east-Broward corridor) are
written; rows with no identifier at all are dropped; duplicates (same email, phone or name+ZIP) are merged.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .config import service_area
from .normalize import to_e164

COLUMNS = ["email", "phone", "fn", "ln", "ct", "st", "zip", "country", "company", "value"]
EMAIL_OK = {"mx", "a_only", "unchecked"}       # same rule as the CRM payload


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def norm_name(s: str | None) -> str:
    return re.sub(r"[^a-z\-]", "", _ascii(s or "").lower())


def norm_city(s: str | None) -> str:
    return re.sub(r"[^a-z]", "", _ascii(s or "").lower())


def split_name(full: str | None) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p and not p.endswith(".")]
    parts = [p for p in parts if p.lower() not in ("jr", "sr", "ii", "iii", "mr", "mrs", "ms", "dr")]
    if len(parts) < 2:
        return "", ""
    return norm_name(parts[0]), norm_name(parts[-1])


def address_parts(address: str | None) -> tuple[str, str, str]:
    """('miamibeach', 'fl', '33140') from 'street, City, FL 33140[-1234]'."""
    parts = [p.strip() for p in (address or "").split(",")]
    m = re.search(r"\b([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$", (address or "").upper())
    city = parts[-2] if len(parts) >= 3 else ""
    return norm_city(city), (m.group(1).lower() if m else ""), (m.group(2) if m else "")


def in_service_area(address: str | None, area=None) -> bool:
    from .collectors.dbpr_seed import area_filter, in_area

    parts = [p.strip() for p in (address or "").split(",")]
    _, _, zipc = address_parts(address)
    city = parts[-2] if len(parts) >= 3 else ""
    return in_area(city, zipc, area or area_filter(service_area()))


def value_of(deal: dict[str, Any] | None) -> int | None:
    if not deal or not deal.get("high"):
        return None
    return int(round(((deal.get("low") or 0) + deal["high"]) / 2))


def row(*, email: str | None, phone: str | None, name: str | None, address: str | None, company: str | None,
        value: int | None) -> dict[str, Any]:
    fn, ln = split_name(name)
    ct, st, zipc = address_parts(address)
    return {"email": (email or "").strip().lower(), "phone": (to_e164(phone) or "") if phone else "", "fn": fn, "ln": ln,
            "ct": ct, "st": st, "zip": zipc, "country": "us", "company": (company or "").strip(),
            "value": value if value is not None else ""}


def has_identifier(r: dict[str, Any]) -> bool:
    return bool(r["email"] or r["phone"] or (r["fn"] and r["ln"] and r["zip"]))


def from_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Batch leads (leads.jsonl rows) → audience rows, primary contact first, then the other named contacts."""
    out = []
    for rec in records:
        email = rec.get("email") if rec.get("email_status") in EMAIL_OK else None
        company = rec.get("management_company") or rec.get("company_name")
        value = value_of(rec.get("estimated_deal"))
        out.append({**row(email=email, phone=rec.get("phone"), name=rec.get("contact_name"),
                          address=rec.get("address"), company=company, value=value), "_address": rec.get("address")})
        for c in rec.get("contacts") or []:
            if c.get("name") and c["name"] != rec.get("contact_name"):
                out.append({**row(email=None, phone=c.get("phone"), name=c["name"], address=rec.get("address"),
                                  company=company, value=value), "_address": rec.get("address")})
    return out


def from_registry(min_units: int | None = None, conn=None) -> list[dict[str, Any]]:
    """DBPR condos of the service area with a registered contact person (name + building location)."""
    from . import valuation
    from .collectors import dbpr_seed
    from .pipeline import COASTAL_3MI_ZIPS

    out = []
    for d in dbpr_seed.candidates("onsite", conn, cities=service_area(), min_units=min_units) + \
            dbpr_seed.candidates("portfolio", conn, cities=service_area(), min_units=min_units):
        if not d.get("person"):
            continue
        address = dbpr_seed._clean_address(d["address"])
        units = d.get("units_total") or 0
        est = valuation.estimate({"units": units, "coastal": (d.get("zip") or "") in COASTAL_3MI_ZIPS}) if units else None
        out.append({**row(email=None, phone=None, name=d["person"], address=address,
                          company=d.get("company") or dbpr_seed._short_condo_name(d.get("condo_name")),
                          value=value_of(est)), "_address": address})
    return out


def write_csv(rows: list[dict[str, Any]], path: str | Path) -> dict[str, int]:
    """Filter to the service area, drop rows without identifiers, merge duplicates, write the CSV."""
    from .collectors.dbpr_seed import area_filter

    area = area_filter(service_area())
    seen: set[str] = set()
    kept, outside, empty, dup = [], 0, 0, 0
    for r in rows:
        if not in_service_area(r.get("_address"), area):
            outside += 1
            continue
        if not has_identifier(r):
            empty += 1
            continue
        keys = {k for k in (r["email"] and f"e:{r['email']}", r["phone"] and f"p:{r['phone']}",
                            r["fn"] and r["ln"] and f"n:{r['fn']}|{r['ln']}|{r['zip']}") if k}
        if keys & seen:
            dup += 1
            continue
        seen |= keys
        kept.append({k: r[k] for k in COLUMNS})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(kept)
    return {"rows": len(kept), "outside_area": outside, "no_identifier": empty, "duplicates": dup,
            "with_email": sum(1 for r in kept if r["email"]), "with_phone": sum(1 for r in kept if r["phone"])}
