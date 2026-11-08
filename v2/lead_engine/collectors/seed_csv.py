"""CSV seed collector for public lists and directories.

Use it for exports you already have: Florida DBPR condo association list,
CAI / association directories, a property manager's "communities we manage"
page copied to a sheet, an Apollo export, etc. Columns (header names are
case-insensitive; only `name` is required):

    name, phone, address, website, contact_name, contact_title, email,
    management_company, properties_count

Rows without phone get it from the website in the extraction step; rows with
neither phone nor website are dropped by the gate.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterator

from ..models import Lead
from ..normalize import clean_address, clean_email, extract_domain, is_company_site, to_e164


def collect(path: str | Path, strategy: str, max_candidates: int) -> Iterator[Lead]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        emitted = 0
        for i, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            name = row.get("name") or row.get("company_name")
            if not name:
                continue
            website = row.get("website") or row.get("website_url")
            count = row.get("properties_count", "")
            yield Lead(
                source="seed_csv",
                source_id=f"{Path(path).name}:{i}",
                strategy_kind=strategy,  # type: ignore[arg-type]
                company_name=name,
                phone=to_e164(row.get("phone")),
                phones=[{"number": to_e164(row.get("phone")), "label": "unlabeled", "source": "seed", "context": ""}]
                if to_e164(row.get("phone")) else [],
                address=clean_address(row.get("address")),
                website_url=website if is_company_site(website) else None,
                domain=extract_domain(website) if is_company_site(website) else "",
                contact_name=row.get("contact_name") or None,
                contact_title=row.get("contact_title") or None,
                email=clean_email(row.get("email")),
                email_source="seed" if clean_email(row.get("email")) else None,
                management_company=row.get("management_company") or None,
                properties_count=int(count) if count.isdigit() else None,
            )
            emitted += 1
            if emitted >= max_candidates:
                return
