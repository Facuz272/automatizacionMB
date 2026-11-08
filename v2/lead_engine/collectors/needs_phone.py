"""Re-seed the buildings a run left in needs_phone.csv (`python -m lead_engine enrich-phones <csv>`).

Each row comes back as a lead with its original source and id (dbpr:<project>, pa:<folio>), so the
pipeline repeats the full phone search with whatever is configured now (Places by name + address,
external search + site crawl, lodging registry, firm central when allowed) and, when a phone
appears, the normal enrichment, PDF, outreach pack and CRM payload.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Iterator

from ..dbpr import match
from ..models import Lead


def read(path: str | Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(fh)]


def main_strategy(rows: list[dict[str, str]]) -> str:
    c = Counter(r.get("strategy") for r in rows if r.get("strategy") in ("onsite", "portfolio", "pain"))
    return c.most_common(1)[0][0] if c else "onsite"


def to_lead(row: dict[str, str], strategy: str) -> Lead | None:
    if not row.get("condo") or not row.get("address"):
        return None
    source = row.get("source") or "dbpr"
    sid = row.get("source_id") or (f"dbpr:{row['dbpr_project']}" if row.get("dbpr_project") else f"np:{row['address']}")
    kind = row.get("strategy") if row.get("strategy") in ("onsite", "portfolio", "pain") else strategy
    lead = Lead(source=source, source_id=sid, strategy_kind=kind,  # type: ignore[arg-type]
                company_name=row["condo"], address=row["address"],
                evidence={"seed": "needs_phone", "retry_of": row.get("consolidated_into") or None})
    if row.get("website"):
        lead.website_url = row["website"]
    if source == "dbpr":
        rec = match(row["address"], row["condo"])
        if rec:
            lead.public_records["dbpr"] = {k: rec.get(k) for k in (
                "project_number", "condo_name", "association", "units", "projects_at_address", "status", "route_raw",
                "management_company", "contact_name", "contact_title", "management_type", "registered_agent",
                "matched_by")}
    mgmt = row.get("management") or ""
    if mgmt and mgmt not in ("onsite", "self_managed", "unknown"):
        lead.management_company = mgmt
        lead.management_type = "third_party"
    elif mgmt == "onsite":
        lead.management_type = "onsite"
    return lead


def collect(path: str | Path, strategy: str, max_candidates: int) -> Iterator[Lead]:
    for row in read(path)[: max(1, max_candidates)]:
        lead = to_lead(row, strategy)
        if lead:
            yield lead
