"""Local SQLite ledger: memory between runs.

`seen_leads`: every building a run worked on, keyed by its normalised street address (so the
same tower arriving from DBPR, the cadastre or Places is one building) and by its source id
(place_id, dbpr project, folio, NPI). Status: processed (accepted in a dry run), imported
(committed to the CRM), needs_phone, rejected, in_crm. A new run skips them, so
`--seed dbpr --limit 20` brings the next 20 of the ranking; `--ignore-seen` forces them back and
`--retry-needs-phone` retries only the ones that lacked a phone.
`seen` / `imported`: older per-source and per-phone tables, kept for compatibility.
The CRM remains the source of truth for duplicates.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .normalize import phone_key

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
  source_id TEXT PRIMARY KEY, source TEXT, phone_key TEXT, outcome TEXT, reason TEXT,
  batch_id TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS imported (
  phone_key TEXT PRIMARY KEY, company_name TEXT, batch_id TEXT, imported_at TEXT
);
CREATE TABLE IF NOT EXISTS seen_leads (
  seed_key TEXT PRIMARY KEY, source TEXT, source_id TEXT, address_key TEXT, phone_key TEXT, company_name TEXT,
  status TEXT, reason TEXT, batch_id TEXT, first_seen TEXT, last_seen TEXT
);
CREATE INDEX IF NOT EXISTS seen_leads_addr ON seen_leads(address_key);
CREATE INDEX IF NOT EXISTS seen_leads_src ON seen_leads(source_id);
"""

STATUS_RANK = {"rejected": 0, "needs_phone": 1, "processed": 2, "in_crm": 3, "imported": 4}


def seed_keys(lead) -> list[str]:
    """Identity keys of a lead: normalised street address (cross-source) and source id."""
    from .dbpr import address_key

    keys = []
    ak = address_key(lead.address)
    if ak:
        keys.append("addr:" + ak)
    if lead.source_id:
        keys.append("src:" + lead.source_id)
    return keys


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or settings.state_db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)

    def outcome(self, source_id: str) -> str | None:
        row = self.conn.execute("SELECT outcome FROM seen WHERE source_id = ?", (source_id,)).fetchone()
        return row[0] if row else None

    def is_imported(self, phone: str | None) -> bool:
        if not phone:
            return False
        return self.conn.execute("SELECT 1 FROM imported WHERE phone_key = ?", (phone_key(phone),)).fetchone() is not None

    def mark(self, source_id: str, source: str, phone: str | None, outcome: str, reason: str | None, batch_id: str) -> None:
        self.conn.execute(
            "INSERT INTO seen VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET "
            "outcome=excluded.outcome, reason=excluded.reason, batch_id=excluded.batch_id, updated_at=excluded.updated_at",
            (source_id, source, phone_key(phone) if phone else None, outcome, reason, batch_id,
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def mark_imported(self, payloads: list[dict], report) -> None:
        dup_or_rej = {d["index"] for d in report.duplicates} | {r["index"] for r in report.rejected} | {
            s["index"] for s in report.skipped_local}
        now = datetime.now(timezone.utc).isoformat()
        for i, p in enumerate(payloads):
            if i in dup_or_rej:
                continue
            self.conn.execute(
                "INSERT OR IGNORE INTO imported VALUES (?,?,?,?)",
                (phone_key(p["phone"]), p.get("company_name"), report.batch_id, now),
            )
        self.conn.commit()


    # ── memory between runs ───────────────────────────────────────────────
    def seen_status(self, lead) -> str | None:
        """Strongest status any of the lead's keys has (None when never seen)."""
        from .dbpr import address_key

        ak = address_key(lead.address)
        rows = self.conn.execute(
            "SELECT status FROM seen_leads WHERE seed_key IN ({}) OR (address_key IS NOT NULL AND address_key = ?) "
            "OR source_id = ?".format(",".join("?" * len(seed_keys(lead))) or "''"),
            (*seed_keys(lead), ak, lead.source_id)).fetchall()
        statuses = [r[0] for r in rows]
        return max(statuses, key=lambda s: STATUS_RANK.get(s, -1)) if statuses else None

    def should_skip(self, lead, *, ignore_seen: bool = False, retry_needs_phone: bool = False) -> bool:
        if ignore_seen:
            return False
        st = self.seen_status(lead)
        if st is None:
            return False
        return not (retry_needs_phone and st == "needs_phone")

    def mark_seen(self, lead, status: str, batch_id: str, reason: str | None = None) -> None:
        from .dbpr import address_key

        now = datetime.now(timezone.utc).isoformat()
        ak = address_key(lead.address)
        for key in seed_keys(lead):
            row = self.conn.execute("SELECT status FROM seen_leads WHERE seed_key = ?", (key,)).fetchone()
            if row and STATUS_RANK.get(row[0], -1) > STATUS_RANK.get(status, -1):
                continue                    # never downgrade (an imported lead stays imported)
            self.conn.execute(
                "INSERT INTO seen_leads VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(seed_key) DO UPDATE SET "
                "status=excluded.status, reason=excluded.reason, batch_id=excluded.batch_id, "
                "phone_key=COALESCE(excluded.phone_key, seen_leads.phone_key), last_seen=excluded.last_seen",
                (key, lead.source, lead.source_id, ak, phone_key(lead.phone) if lead.phone else None, lead.company_name,
                 status, reason, batch_id, now, now))
        self.conn.commit()

    def seen_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status, COUNT(*) FROM seen_leads WHERE seed_key LIKE 'src:%' "
                                      "GROUP BY status").fetchall())
