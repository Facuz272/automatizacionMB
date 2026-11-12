"""Client for the CRM bulk import: POST {CRM_BASE_URL}/api/public/leads-import.

Contract (docs/IMPORTAR_LEADS.md in the CRM repo):
  headers  x-webhook-secret: <integration_settings.webhook_secret>
  body     {"dry_run": bool, "source"?: str, "import_batch_id"?: str, "leads": [...≤200]}
           each lead: company_name, phone, segment, strategy_kind, contact_name, contact_title,
           management_company, email, address, website_url, maps_url, google_rating,
           total_reviews, sales_strategy (hook only), properties_count, source, import_batch_id
  200      {ok, dry_run, inserted, would_insert, positions{from,to}, duplicates[{index,
            existing_position, existing_company}], rejected[{index, reason}]}
  400      invalid_json | invalid_payload   401 unauthorized   500 import_failed

The server dedupes by phone_key (last 10 digits) against the whole CRM and inside
the batch, in one transaction under an advisory lock. Retrying a batch after a
timeout is therefore safe: rows already inserted come back as duplicates.

CLI:  python -m lead_engine.crm_importer out/<batch>/crm_payload.json            (dry run)
      python -m lead_engine.crm_importer out/<batch>/crm_payload.json --commit   (writes)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from loguru import logger

from .config import settings
from .normalize import phone_key

IMPORT_MAX = 200
REQUIRED = ("company_name", "phone", "segment")


class CrmError(RuntimeError):
    pass


class CrmAuthError(CrmError):
    pass


@dataclass
class ImportReport:
    batch_id: str
    dry_run: bool
    sent: int = 0
    inserted: int = 0
    would_insert: int = 0
    positions: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)   # index is global (position in the input list)
    rejected: list[dict] = field(default_factory=list)
    skipped_local: list[dict] = field(default_factory=list)  # never sent (missing fields / repeated phone)
    responses: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class CrmClient:
    def __init__(self, base_url: str | None = None, secret: str | None = None, timeout: float = 60):
        self.url = (base_url or settings.crm_base_url).rstrip("/") + "/api/public/leads-import"
        self.secret = secret or settings.crm_webhook_secret
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.secret)

    def post(self, leads: list[dict], dry_run: bool, source: str | None = None, batch_id: str | None = None) -> dict:
        if not self.secret:
            raise CrmAuthError("Falta CRM_WEBHOOK_SECRET")
        if not 1 <= len(leads) <= IMPORT_MAX:
            raise ValueError(f"un lote debe tener entre 1 y {IMPORT_MAX} leads")
        body: dict[str, Any] = {"dry_run": dry_run, "leads": leads}
        # Batch-level traceability; rows that carry their own values win on the server.
        if source:
            body["source"] = source
        if batch_id and batch_id != "precheck":
            body["import_batch_id"] = batch_id
        headers = {"x-webhook-secret": self.secret, "content-type": "application/json"}
        last: str = ""
        for attempt in range(1, 5):
            try:
                r = self.session.post(self.url, json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last = str(exc)
                logger.warning("CRM red (intento {}/4): {}", attempt, exc)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 401:
                raise CrmAuthError("401: el x-webhook-secret no coincide con ⚡ Integraciones")
            if r.status_code == 400:
                raise CrmError(f"400: {r.text[:500]}")
            if r.status_code >= 500 or r.status_code == 429:
                last = f"HTTP {r.status_code}: {r.text[:200]}"
                logger.warning("CRM {} (intento {}/4)", last, attempt)
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise CrmError(f"CRM no respondió: {last}")


def prepare(leads: list[dict]) -> tuple[list[tuple[int, dict]], list[dict]]:
    """Drop rows the server would reject anyway and repeated phones (first wins)."""
    ok: list[tuple[int, dict]] = []
    skipped: list[dict] = []
    seen: dict[str, int] = {}
    for i, lead in enumerate(leads):
        missing = [k for k in REQUIRED if not lead.get(k)]
        if missing:
            skipped.append({"index": i, "reason": "faltan " + ", ".join(missing)})
            continue
        key = phone_key(lead["phone"])
        if key in seen:
            skipped.append({"index": i, "reason": f"teléfono repetido (fila {seen[key]})"})
            continue
        seen[key] = i
        ok.append((i, lead))
    return ok, skipped


def import_leads(leads: list[dict], batch_id: str, dry_run: bool = True, client: CrmClient | None = None,
                 batch_size: int = IMPORT_MAX, source: str | None = "lead-engine-v2") -> ImportReport:
    client = client or CrmClient()
    report = ImportReport(batch_id=batch_id, dry_run=dry_run)
    rows, report.skipped_local = prepare(leads)
    batch_size = max(1, min(batch_size, IMPORT_MAX))
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        res = client.post([lead for _, lead in chunk], dry_run=dry_run, source=source, batch_id=batch_id)
        report.sent += len(chunk)
        report.inserted += int(res.get("inserted") or 0)
        report.would_insert += int(res.get("would_insert") or 0)
        if res.get("positions"):
            report.positions.append(res["positions"])
        for kind in ("duplicates", "rejected"):
            for item in res.get(kind) or []:
                local = item.get("index")
                if isinstance(local, int) and 0 <= local < len(chunk):
                    g = chunk[local][0]
                    getattr(report, kind).append({**item, "index": g, "company_name": leads[g].get("company_name")})
        report.responses.append(res)
        logger.info("CRM lote {} [{}..{}] dry_run={} → {}", batch_id, start, start + len(chunk) - 1, dry_run,
                    {k: res.get(k) for k in ("inserted", "would_insert", "positions")})
    return report


def existing_phone_keys(leads: list[dict], client: CrmClient | None = None) -> set[str]:
    """Dry-run precheck: phone_keys already in the CRM, so we don't pay enrichment for them."""
    client = client or CrmClient()
    if not client.configured:
        return set()
    probe = [{"company_name": l["company_name"] or "x", "phone": l["phone"], "segment": "Gran Comunidad"} for l in leads]
    report = import_leads(probe, batch_id="precheck", dry_run=True, client=client, source=None)
    return {phone_key(probe[d["index"]]["phone"]) for d in report.duplicates}


def _load(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(l) for l in text.splitlines() if l.strip()]
    data = json.loads(text)
    return data["leads"] if isinstance(data, dict) else data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Envía leads al CRM (/api/public/leads-import)")
    ap.add_argument("file", type=Path, help="crm_payload.json (o .jsonl con payloads)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="no escribe (por defecto)")
    mode.add_argument("--commit", action="store_true", help="inserta de verdad en el CRM")
    ap.add_argument("--batch-id", default=None)
    ap.add_argument("--batch-size", type=int, default=IMPORT_MAX)
    args = ap.parse_args(argv)

    data = json.loads(args.file.read_text(encoding="utf-8")) if args.file.suffix == ".json" else {}
    batch_id = args.batch_id or (data.get("import_batch_id") if isinstance(data, dict) else None) or args.file.parent.name
    report = import_leads(_load(args.file), batch_id=batch_id, dry_run=not args.commit, batch_size=args.batch_size)
    out = args.file.with_name(f"crm_response_{'commit' if args.commit else 'dry'}.json")
    out.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.as_dict().items() if k != "responses"}, ensure_ascii=False, indent=2))
    print(f"\nRespuesta completa: {out}")
    if args.commit:
        # A manual commit of a batch folder counts like daily's: its leads are consumed and it leaves the queue.
        leads_file = args.file.with_name("leads.jsonl")
        if leads_file.exists() and batch_id:
            from .models import Lead
            from .pipeline import record_commit
            from .state import Ledger

            accepted = [Lead.from_record(json.loads(l)) for l in leads_file.read_text(encoding="utf-8").splitlines()
                        if l.strip()]
            record_commit(Ledger(), batch_id, accepted, _load(args.file), report)
        elif report.inserted:
            from .state import Ledger

            Ledger().mark_imported(_load(args.file), report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
