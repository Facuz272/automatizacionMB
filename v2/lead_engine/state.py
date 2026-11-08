"""Local SQLite ledger so repeated runs don't pay twice for the same place.

`seen`: every candidate processed (source_id -> outcome); `imported`: phone_keys
committed to the CRM with their batch. The CRM remains the source of truth for
duplicates; this only saves Places/Apollo/Claude spend.
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
"""


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
