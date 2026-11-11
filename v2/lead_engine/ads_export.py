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

COLUMNS = ["email", "phone", "fn", "ln", "ct", "st", "zip", "country", "company", "value", "company_website"]
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
        value: int | None, website: str | None = None, source: str = "") -> dict[str, Any]:
    fn, ln = split_name(name)
    ct, st, zipc = address_parts(address)
    return {"email": (email or "").strip().lower(), "phone": (to_e164(phone) or "") if phone else "", "fn": fn, "ln": ln,
            "ct": ct, "st": st, "zip": zipc, "country": "us", "company": (company or "").strip(),
            "value": value if value is not None else "", "company_website": website or "", "_source": source}


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


def _condo_rows(min_units: int | None, conn) -> list[dict[str, Any]]:
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
                          value=value_of(est), source="dbpr_condo"), "_address": address})
    return out


ASSOCIATION = re.compile(r"(?i)\b(ASSN|ASSOC|ASSOCIATION|CONDOMINIUM|CONDO|HOA|OWNERS)\b")
OPERATOR_RANKS = ("HOTL", "MOTL", "NAPT", "TAPT")


def _lodging_rows(conn) -> list[dict[str, Any]]:
    """Active DBPR lodging licences with a phone: hotels, motels, apartment buildings, apart-hotels, and condo
    rental licences held by the association itself (never individual unit owners: CNDO of a person, DWEL, BNB)."""
    from . import lodging, valuation
    from .dbpr import _title_case

    lodging.ensure(conn)
    out = []
    for r in conn.execute("SELECT * FROM dbpr_lodging WHERE status = '20' AND phone IS NOT NULL").fetchall():
        if not lodging.is_building_licence(r) and not (r["rank"] == "CNDO" and ASSOCIATION.search(r["licensee"] or "")):
            continue
        address = f"{_title_case((r['address'] or '').split(',')[0])}, {_title_case(r['city'] or '')}, FL {r['zip'] or ''}"
        est = valuation.estimate({"units": r["units"]}) if (r["units"] or 0) >= 10 else None
        out.append({**row(email=None, phone=r["phone"], name=None, address=address,
                          company=_title_case(r["business_name"] or r["licensee"] or ""), value=value_of(est),
                          source=f"dbpr_lodging:{r['rank']}"), "_address": address})
    return out


def _npi_rows(max_pages: int = 3) -> list[dict[str, Any]]:
    """Clinics / hospitals of the service area from the NPI Registry: authorized official (name, direct
    phone) at the practice location; suites inside office towers included (they are B2B contacts too)."""
    from concurrent.futures import ThreadPoolExecutor

    from . import npi
    from .collectors.dbpr_seed import _norm_city, area_filter

    area = area_filter(service_area())
    queries = [("city", c) for c in sorted(area.names)] + [("postal_code", z) for z in sorted(area.zips - area.exclude)]

    def fetch(q):
        kind, value = q
        got = []
        for tax in npi.TAXONOMIES:
            for page in range(max_pages):
                res = npi.search(taxonomy=tax, skip=page * 200, **{kind: _norm_city(value) if kind == "city" else value})
                got += res
                if len(res) < 200:
                    break
        return got

    with ThreadPoolExecutor(max_workers=8) as pool:
        orgs = [o for batch in pool.map(fetch, queries) for o in batch]
    out, seen = [], set()
    for o in orgs:
        if o["npi"] in seen:
            continue
        seen.add(o["npi"])
        phone = o.get("official_phone") or o.get("phone")
        out.append({**row(email=None, phone=phone, name=o.get("official_name"), address=o["address"],
                          company=re.sub(r"'S\b", "'s", (o.get("organization") or "").title()), value=None,
                          source="npi"),
                    "_address": o["address"]})
    return out


def _firm_rows(conn) -> list[dict[str, Any]]:
    """One row per management firm / commercial operator active in the area, with its published phone and site."""
    from . import commercial_mgmt, mgmt_domains, phone_resolver
    from .collectors import dbpr_seed

    out, seen = [], set()
    for d in dbpr_seed.candidates("portfolio", conn, cities=service_area()):
        firm = d.get("company")
        hit = mgmt_domains.from_catalog(firm) if firm else None
        if not hit or hit[0] in seen:
            continue
        seen.add(hit[0])
        fp = phone_resolver.firm_phone(firm)
        address = dbpr_seed._clean_address(d["address"])
        out.append({**row(email=None, phone=fp and fp["number"], name=None, address=address, company=hit[0],
                          value=None, website=hit[1], source="mgmt_firm"), "_address": address})
    for op in commercial_mgmt.catalog():
        phone = commercial_mgmt.dial_phone(op)
        if phone and op["name"] not in seen:
            seen.add(op["name"])
            ctx = op.get("phone_context") or ""
            m = re.search(r"([A-Z][a-z]+(?: [A-Z][a-z]+)*), FL (\d{5})", ctx)
            address = f"x, {m.group(1)}, FL {m.group(2)}" if m else None
            out.append({**row(email=None, phone=phone, name=None, address=address, company=op["name"], value=None,
                              website=op["domain"], source="commercial_operator"), "_address": address or "",
                        "_any_area": True})
    return out


def from_registry(min_units: int | None = None, conn=None, *, include_lodging: bool = True,
                  include_npi: bool = True, include_firms: bool = True) -> list[dict[str, Any]]:
    """Local registries of the service area: DBPR condo contacts, DBPR lodging operators (real phones),
    NPI clinics (official + direct phone) and the management firms / commercial operators."""
    from .dbpr import connect

    conn = conn or connect()
    rows = _condo_rows(min_units, conn)
    if include_lodging:
        rows += _lodging_rows(conn)
    if include_firms:
        rows += _firm_rows(conn)
    if include_npi:
        rows += _npi_rows()
    return rows


def write_csv(rows: list[dict[str, Any]], path: str | Path) -> dict[str, int]:
    """Filter to the service area, drop rows without identifiers, merge duplicates, write the CSV."""
    from .collectors.dbpr_seed import area_filter

    area = area_filter(service_area())
    seen: set[str] = set()
    kept, outside, empty, dup = [], 0, 0, 0
    for r in rows:
        if not r.get("_any_area") and not in_service_area(r.get("_address"), area):
            outside += 1
            continue
        if not has_identifier(r):
            empty += 1
            continue
        street = (r.get("_address") or "").split(",")[0].strip().upper()
        keys = {k for k in (r["email"] and f"e:{r['email']}", r["phone"] and f"p:{r['phone']}",
                            r["fn"] and r["ln"] and f"n:{r['fn']}|{r['ln']}|{r['zip']}",
                            # same company at the same street address (lodging licence + registry contact rows)
                            street and r["company"] and not (r["fn"] or r["email"]) and
                            f"a:{street}|{r['company'].upper()}") if k}
        if keys & seen:
            dup += 1
            continue
        seen |= keys
        kept.append({**{k: r[k] for k in COLUMNS}, "_source": r.get("_source", "")})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(kept)
    by_source: dict[str, int] = {}
    for r in kept:
        by_source[r["_source"] or "batch"] = by_source.get(r["_source"] or "batch", 0) + 1
    return {"rows": len(kept), "outside_area": outside, "no_identifier": empty, "duplicates": dup,
            "with_email": sum(1 for r in kept if r["email"]), "with_phone": sum(1 for r in kept if r["phone"]),
            "by_source": by_source}
