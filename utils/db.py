import sqlite3
import os
from loguru import logger

from config import DB_PATH


def get_connection():
    """
    Every connection gets WAL mode + performance PRAGMAs.
    WAL is a DB-level persistent setting (first call enables it, subsequent calls are no-ops).
    timeout=30 prevents immediate SQLITE_BUSY errors under transient lock contention.
    """
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")       # concurrent reads, serialized writes
    conn.execute("PRAGMA synchronous=NORMAL")      # safe + 3-5x faster than FULL
    conn.execute("PRAGMA cache_size=-64000")       # 64 MB page cache
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Migrations ────────────────────────────────────────────────────────────────

def _migrate_generated_emails_sequence_step(conn):
    """
    V1 → V2: recreate generated_emails with sequence_step + UNIQUE(email, sequence_step).
    Skipped if already on new schema.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(generated_emails)")
    columns = {row[1] for row in cursor.fetchall()}

    if "sequence_step" in columns:
        return

    logger.info("Migration V2: generated_emails → multi-step schema...")
    cursor.execute("ALTER TABLE generated_emails RENAME TO _generated_emails_v1")
    cursor.execute("""
        CREATE TABLE generated_emails (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            domain         TEXT,
            email          TEXT,
            subject        TEXT,
            body           TEXT,
            sequence_step  INTEGER NOT NULL DEFAULT 1,
            send_status    TEXT    DEFAULT 'pending',
            sent_at        TIMESTAMP DEFAULT NULL,
            replied        INTEGER NOT NULL DEFAULT 0,
            failure_count  INTEGER NOT NULL DEFAULT 0,
            UNIQUE(email, sequence_step)
        )
    """)
    cursor.execute("""
        INSERT INTO generated_emails
            (domain, email, subject, body, sequence_step, send_status, sent_at, replied, failure_count)
        SELECT domain, email, subject, body, 1, send_status, sent_at, 0, 0
        FROM _generated_emails_v1
    """)
    cursor.execute("DROP TABLE _generated_emails_v1")
    conn.commit()
    logger.info("Migration V2 complete")


def _add_column_if_missing(conn, table: str, column: str, definition: str):
    """ALTER TABLE ADD COLUMN — safe no-op if column already exists."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    if column not in existing:
        logger.info("Migration: adding column {}.{}", table, column)
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        conn.commit()


def _run_all_migrations(conn):
    _migrate_generated_emails_sequence_step(conn)
    _add_column_if_missing(conn, "leads",            "scraped_at",           "TIMESTAMP DEFAULT NULL")
    _add_column_if_missing(conn, "leads",            "website_text",         "TEXT DEFAULT NULL")
    _add_column_if_missing(conn, "leads",            "rating",               "REAL DEFAULT NULL")
    _add_column_if_missing(conn, "leads",            "user_ratings_total",   "INTEGER DEFAULT NULL")
    _add_column_if_missing(conn, "generated_emails", "replied",              "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "generated_emails", "failure_count",        "INTEGER NOT NULL DEFAULT 0")


# ── Public API ────────────────────────────────────────────────────────────────

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            place_id            TEXT PRIMARY KEY,
            name                TEXT,
            address             TEXT,
            city                TEXT,
            website             TEXT,
            domain              TEXT,
            scraped_at          TIMESTAMP DEFAULT NULL,
            website_text        TEXT      DEFAULT NULL,
            rating              REAL      DEFAULT NULL,
            user_ratings_total  INTEGER   DEFAULT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS enriched_leads (
            domain TEXT,
            email  TEXT,
            UNIQUE(domain, email)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suppression_list (
            id         INTEGER   PRIMARY KEY AUTOINCREMENT,
            email      TEXT      NOT NULL,
            domain     TEXT,
            reason     TEXT      NOT NULL
                       CHECK(reason IN ('unsubscribe', 'bounce', 'manual')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(email)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS generated_emails (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            domain         TEXT,
            email          TEXT,
            subject        TEXT,
            body           TEXT,
            sequence_step  INTEGER NOT NULL DEFAULT 1,
            send_status    TEXT    DEFAULT 'pending',
            sent_at        TIMESTAMP DEFAULT NULL,
            replied        INTEGER NOT NULL DEFAULT 0,
            failure_count  INTEGER NOT NULL DEFAULT 0,
            UNIQUE(email, sequence_step)
        )
    """)

    conn.commit()
    _run_all_migrations(conn)

    # ── Indexes ───────────────────────────────────────────────────────────────
    # Create after migrations so they cover any newly added columns.

    # enricher: find unscraped domains
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_leads_scraped_at
        ON leads(scraped_at)
    """)
    # scraper: domain lookups / enricher mark_domain_scraped
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_leads_domain
        ON leads(domain)
    """)
    # writer + tracker: lookup by email + step (also covers the UNIQUE constraint scans)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ge_email_step
        ON generated_emails(email, sequence_step)
    """)
    # sender: pending/failed filter with replied guard
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ge_status_replied
        ON generated_emails(send_status, replied)
    """)
    # writer: get_followup_candidates — partial index over sent rows only
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_ge_sent_at
        ON generated_emails(sent_at, replied)
        WHERE send_status = 'sent'
    """)
    # tracker / sender: fast suppression lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_suppression_email
        ON suppression_list(email)
    """)

    conn.commit()
    conn.close()
    logger.info("Database ready at {}", DB_PATH)


if __name__ == "__main__":
    init_db()
