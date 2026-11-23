"""Recycle dead phones (☎️): find another number for CRM leads whose line a setter marked wrong / disconnected /
no tone, and send it as `new_phone` to POST /api/public/leads-enrich (CRM migration 0078).

`python -m lead_engine backfill-crm --dead-phones [--commit] [--limit N] [--max-cost USD] [--no-places]`

1. GET /api/public/leads-unenriched?include_attempted=true → open leads with `phone_dead: true` / `missing: phone`.
2. Candidates for each, free sources first: the lead's website (crawl), the DBPR lodging licence of the building,
   the management firm's published line (verified catalog / its own site); only when none of those gives a new
   number, one Places lookup (PLACES_MODE=details: IDs Only + Place Details) within the run's cost ceiling.
3. Never offered: the dead number itself, numbers the engine already knows are bad for that building, and
   numbers the CRM already has on another lead (one dry-run precheck for every candidate). The CRM re-checks
   it all and answers per row (`phone_not_dead`, `phone_invalid`, `phone_same`, `phone_duplicate`).
4. Every row goes first as a dry run; --commit sends the same rows for real. The dead number is stored in
   state.db (`bad_phones`) for the building, so no future run offers it again.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from . import backfill, costs
from .config import settings
from .models import Lead
from .normalize import phone_key, to_e164
from .state import Ledger, building_keys
from .web_extract import LABEL_PRIORITY

DEAD_REASON = "crm_dead_line"
SOURCE_RANK = {"website": 0, "dbpr_lodging": 1, "firm_line": 2, "google_places": 3}


def dead_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Open CRM leads whose main line is marked dead (☎️)."""
    return [r for r in rows if r.get("phone_dead") or "phone" in (r.get("missing") or [])]


def _us(number: str | None) -> str | None:
    """+1XXXXXXXXXX or None (the CRM accepts only 10-digit US numbers as new_phone)."""
    e = to_e164(number)
    return e if e and e.startswith("+1") and len(e) == 12 else None


def free_candidates(row: dict[str, Any], lead: Lead) -> list[dict[str, Any]]:
    """Numbers from sources that cost nothing: the site, the building's lodging licence, the firm's line."""
    from . import lodging, phone_resolver, pipeline

    out: list[dict[str, Any]] = []
    if lead.website_url:
        try:
            facts = pipeline.crawl(lead.website_url, max_pages=backfill.CRAWL_PAGES)
            out += [{"number": p["number"], "label": p.get("label") or "unlabeled", "source": "website"}
                    for p in facts.phones if p.get("label") != "fax"]
        except Exception as exc:                     # noqa: BLE001 — a dead site is just no candidate
            logger.warning("sitio de {} falló: {}", lead.company_name, exc)
    if lead.address and lead.evidence.get("lead_kind") == "building":
        hit = lodging.building_phone(lead.address, [lead.company_name])
        if hit:
            out.append({"number": hit["phone"], "label": hit.get("label") or "front_desk", "source": "dbpr_lodging"})
    firm = row.get("management_company") or (lead.company_name if lead.evidence.get("lead_kind") == "management_firm"
                                             else None)
    fp = phone_resolver.firm_phone(firm) if firm else None
    if fp:
        out.append({"number": fp["number"], "label": "firm_central", "source": "firm_line"})
    return out


def places_candidates(row: dict[str, Any], lead: Lead) -> list[dict[str, Any]]:
    if not settings.google_places_api_key or costs.tracker.over_budget():
        return []
    found = backfill.identify(row, lead.strategy_kind)
    return [{"number": p["number"], "label": p.get("label") or "google_main", "source": "google_places"}
            for p in (found.phones if found else [])]


def rank(cands: list[dict[str, Any]], excluded: set[str]) -> list[dict[str, Any]]:
    """Valid US numbers not excluded, best first: management / HOA office lines, then by source, no repeats."""
    seen: set[str] = set()
    out = []
    for c in sorted(cands, key=lambda c: (LABEL_PRIORITY.get(c["label"], 8), SOURCE_RANK.get(c["source"], 9))):
        n = _us(c["number"])
        k = phone_key(n or "")
        if not n or k in excluded or k in seen:
            continue
        seen.add(k)
        out.append({**c, "number": n})
    return out


def find_new_phone(row: dict[str, Any], ledger: Ledger, *, use_places: bool = True) -> dict[str, Any]:
    """{lead, dead, candidates}: ranked candidates for one dead lead (free sources, then Places if none)."""
    lead = backfill.to_lead(row, backfill.is_firm(row.get("name")))
    dead = _us(row.get("phone"))
    excluded = ({phone_key(dead)} if dead else set()) | ledger.bad_phones_for(lead)
    cands = rank(free_candidates(row, lead), excluded)
    if not cands and use_places:
        cands = rank(places_candidates(row, lead), excluded)
    return {"lead": lead, "dead": dead, "candidates": cands}


def choose(found: list[dict[str, Any]], in_crm: set[str]) -> list[dict[str, Any]]:
    """First candidate per lead that no other CRM lead already has."""
    out = []
    for f in found:
        pick = next((c for c in f["candidates"] if phone_key(c["number"]) not in in_crm), None)
        out.append({**f, "pick": pick})
    return out


def payload_row(row: dict[str, Any], pick: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "phone": _us(row.get("phone")) or row.get("phone"), "name": row.get("name"),
            "new_phone": pick["number"]}


def summarize(response: dict[str, Any] | None) -> dict[str, Any]:
    results = (response or {}).get("results") or []
    written = sum(1 for r in results if "new_phone" in (r.get("fields") or []) or "phone" in (r.get("fields") or []))
    refused = Counter(r["new_phone"] for r in results if isinstance(r.get("new_phone"), str))
    return {"written": written, "refused": dict(refused)}


def run(*, limit: int | None = None, commit: bool = False, max_cost: float = 2.0, use_places: bool = True,
        out_dir: Path | None = None, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from . import crm_importer, render

    ledger = Ledger()
    costs.tracker.reset()
    costs.tracker.budget_usd = max_cost
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    batch_id = f"deadphone-{stamp}"
    out = Path(out_dir or settings.out_dir) / batch_id
    out.mkdir(parents=True, exist_ok=True)
    rows = backfill.select(dead_rows(rows if rows is not None else backfill.fetch_unenriched(True)), None, limit)
    found = []
    try:
        for row in rows:
            try:
                f = find_new_phone(row, ledger, use_places=use_places)
            except Exception as exc:                 # noqa: BLE001 — one lead never stops the batch
                logger.exception("teléfono alternativo de {}: {}", row.get("name"), exc)
                continue
            found.append({"row": row, **f})
    finally:
        render.close()
        costs.tracker.budget_usd = None
    # One CRM precheck (dry run) for every candidate: numbers another lead already has are never sent.
    probe = [{"company_name": f["row"].get("name") or "x", "phone": c["number"]} for f in found for c in f["candidates"]]
    in_crm = crm_importer.existing_phone_keys(probe) if probe else set()
    chosen = choose(found, in_crm)
    payloads = [payload_row(c["row"], c["pick"]) for c in chosen if c["pick"]]
    for c in chosen:                                  # the dead number is never offered again for this building
        if c["dead"]:
            for key in building_keys(c["lead"]):
                ledger.add_bad_phone(key, phone_key(c["dead"]), DEAD_REASON, c["row"]["id"])
    dry = backfill.post_enrich(payloads, batch_id, dry_run=True) if payloads else None
    real = backfill.post_enrich(payloads, batch_id, dry_run=False) if commit and payloads else None
    report = {"batch_id": batch_id, "out": str(out), "committed": bool(real), "dead_leads": len(rows),
              "with_candidate": sum(1 for c in chosen if c["pick"]),
              "only_numbers_already_in_crm": sum(1 for c in chosen if c["candidates"] and not c["pick"]),
              "no_candidate": sum(1 for c in chosen if not c["candidates"]),
              "by_source": dict(Counter(c["pick"]["source"] for c in chosen if c["pick"])),
              "crm": summarize(real or dry), "costs": costs.tracker.report()}
    (out / "dead_phone_payload.json").write_text(json.dumps({"batch_id": batch_id, "leads": payloads},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "dead_phone_report.json").write_text(json.dumps(
        {**report, "rows": [{"id": c["row"]["id"], "name": c["row"].get("name"), "dead": c["dead"],
                             "pick": c["pick"], "candidates": c["candidates"]} for c in chosen]},
        ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for name, body in (("crm_dry_run.json", dry), ("crm_commit.json", real)):
        if body is not None:
            (out / name).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
