"""Local SQLite ledger: memory between runs.

`seen_leads`: every building a run worked on, keyed by its normalised street address (so the
same tower arriving from DBPR, the cadastre or Places is one building) and by its source id
(place_id, dbpr project, folio, NPI). Only definitive outcomes are written, and only by committed
runs (never by a dry run): imported (the CRM confirmed the insert), in_crm, needs_phone, rejected.
A lead is never consumed before the CRM confirms it, so a run that dies half-way or a CRM outage
loses nothing; "processed" rows written by older versions no longer block a building. A new run
skips the rest, so
`--seed dbpr --limit 20` brings the next 20 of the ranking; `--ignore-seen` forces them back and
`--retry-needs-phone` retries only the ones that lacked a phone.
`pending_batches`: committed batches whose CRM send failed after the retries; `daily` re-sends them
before building a new batch.
`crm_outcomes` / `bad_phones` / `building_flags` / `feedback_state`: what the CRM says happened to each
lead (feedback.py): phones proven wrong or dead for a building, buildings run by an external firm, and
the conversion samples behind the daily mix weights. A building whose phone failed is set back to
"rephone" (not consumed) so the next run looks for another line.
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
CREATE TABLE IF NOT EXISTS pending_batches (
  batch_id TEXT PRIMARY KEY, out_dir TEXT, created_at TEXT, attempts INTEGER, last_error TEXT
);
-- CRM feedback (feedback.py): one row per CRM lead, last known outcome.
CREATE TABLE IF NOT EXISTS crm_outcomes (
  crm_id TEXT PRIMARY KEY, batch_id TEXT, source TEXT, seed TEXT, segment TEXT, county TEXT, zip TEXT,
  phone_key TEXT, building_key TEXT, status TEXT, call_attempts INTEGER, no_answer_streak INTEGER,
  discard_reason TEXT, objection TEXT, inspection_scheduled INTEGER, closed_won INTEGER, updated_at TEXT,
  synced_at TEXT, visit_completed INTEGER, quote_amount_usd REAL, quote_status TEXT, quote_loss_reason TEXT
);
CREATE INDEX IF NOT EXISTS crm_outcomes_phone ON crm_outcomes(phone_key);
-- Phones the CRM proved useless for a building (wrong number / never answers); never offered again there.
CREATE TABLE IF NOT EXISTS bad_phones (
  building_key TEXT, phone_key TEXT, reason TEXT, crm_id TEXT, created_at TEXT,
  PRIMARY KEY (building_key, phone_key)
);
-- Per-building hints from the CRM (prefer_firm: the building is run by an external management firm).
CREATE TABLE IF NOT EXISTS building_flags (
  building_key TEXT, flag TEXT, value TEXT, crm_id TEXT, created_at TEXT, PRIMARY KEY (building_key, flag)
);
CREATE TABLE IF NOT EXISTS feedback_state (key TEXT PRIMARY KEY, value TEXT);
"""

# Statuses that mean "this building is done": a new run skips it. "processed" (older versions wrote it
# before the CRM confirmed) is deliberately not here.
CONSUMED = {"imported", "in_crm", "needs_phone", "rejected"}
# Written by the CRM feedback when a building must be worked again (bad phone / external firm): not consumed,
# and mark_seen may overwrite it (rank -1).
REOPENED = "rephone"

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


def building_keys(lead) -> list[str]:
    """Keys a building's CRM feedback (bad phones, flags) is stored under: the same identity keys."""
    return seed_keys(lead)


class Ledger:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or settings.state_db)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.executescript(SCHEMA)
        self._migrate()

    # Columns added after a table was first created (state.db files from earlier versions keep their rows).
    ADDED_COLUMNS = {"crm_outcomes": {"visit_completed": "INTEGER", "quote_amount_usd": "REAL",
                                      "quote_status": "TEXT", "quote_loss_reason": "TEXT"}}

    def _migrate(self) -> None:
        for table, cols in self.ADDED_COLUMNS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, kind in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        self.conn.commit()

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
        if st not in CONSUMED:
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

    # ── committed batches waiting for the CRM ─────────────────────────────
    def add_pending(self, batch_id: str, out_dir: Path, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO pending_batches VALUES (?,?,?,1,?) ON CONFLICT(batch_id) DO UPDATE SET "
            "attempts = pending_batches.attempts + 1, last_error = excluded.last_error",
            (batch_id, str(out_dir), now, error[:500]))
        self.conn.commit()

    def pending(self) -> list[dict]:
        rows = self.conn.execute("SELECT batch_id, out_dir, created_at, attempts, last_error FROM pending_batches "
                                 "ORDER BY created_at").fetchall()
        return [dict(zip(("batch_id", "out_dir", "created_at", "attempts", "last_error"), r)) for r in rows]

    def clear_pending(self, batch_id: str) -> None:
        self.conn.execute("DELETE FROM pending_batches WHERE batch_id = ?", (batch_id,))
        self.conn.commit()

    def seen_counts(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT status, COUNT(*) FROM seen_leads WHERE seed_key LIKE 'src:%' "
                                      "GROUP BY status").fetchall())

    # ── CRM feedback ──────────────────────────────────────────────────────
    def buildings_for_phone(self, pkey: str) -> list[dict]:
        """seen_leads rows (one per building) whose phone was this one."""
        rows = self.conn.execute(
            "SELECT DISTINCT address_key, source_id, source, company_name, batch_id FROM seen_leads "
            "WHERE phone_key = ?", (pkey,)).fetchall()
        return [dict(zip(("address_key", "source_id", "source", "company_name", "batch_id"), r)) for r in rows]

    def reopen_building(self, address_key: str | None, source_id: str | None) -> int:
        """Make a consumed building eligible again (next run looks for another phone / the firm's line)."""
        cur = self.conn.execute(
            "UPDATE seen_leads SET status = ?, last_seen = ? WHERE (address_key IS NOT NULL AND address_key = ?) "
            "OR (source_id IS NOT NULL AND source_id = ?)",
            (REOPENED, datetime.now(timezone.utc).isoformat(), address_key, source_id))
        self.conn.commit()
        return cur.rowcount

    def add_bad_phone(self, building_key: str, pkey: str, reason: str, crm_id: str | None = None) -> bool:
        cur = self.conn.execute("INSERT OR IGNORE INTO bad_phones VALUES (?,?,?,?,?)",
                                (building_key, pkey, reason, crm_id, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()
        return cur.rowcount > 0

    def bad_phones_for(self, lead) -> set[str]:
        keys = building_keys(lead)
        if not keys:
            return set()
        rows = self.conn.execute(f"SELECT phone_key FROM bad_phones WHERE building_key IN ({','.join('?' * len(keys))})",
                                 keys).fetchall()
        return {r[0] for r in rows}

    def set_flag(self, building_key: str, flag: str, value: str = "1", crm_id: str | None = None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO building_flags VALUES (?,?,?,?,?)",
                          (building_key, flag, value, crm_id, datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def flags_for(self, lead) -> dict[str, str]:
        keys = building_keys(lead)
        if not keys:
            return {}
        rows = self.conn.execute(f"SELECT flag, value FROM building_flags WHERE building_key IN "
                                 f"({','.join('?' * len(keys))})", keys).fetchall()
        return dict(rows)

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM feedback_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO feedback_state VALUES (?,?)", (key, value))
        self.conn.commit()
