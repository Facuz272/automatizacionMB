import sqlite3
import os
from loguru import logger

from config import DB_PATH


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


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

    logger.info("Migration: generated_emails → multi-step schema (sequence_step)...")
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
            UNIQUE(email, sequence_step)
        )
    """)
    cursor.execute("""
        INSERT INTO generated_emails
            (domain, email, subject, body, sequence_step, send_status, sent_at, replied)
        SELECT domain, email, subject, body, 1, send_status, sent_at, 0
        FROM _generated_emails_v1
    """)
    cursor.execute("DROP TABLE _generated_emails_v1")
    conn.commit()
    logger.info("Migration complete — existing emails preserved as sequence_step=1, replied=0")


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
    # V3 columns — safe to add even if already present
    _add_column_if_missing(conn, "leads", "scraped_at", "TIMESTAMP DEFAULT NULL")
    _add_column_if_missing(conn, "generated_emails", "replied", "INTEGER NOT NULL DEFAULT 0")


# ── Public API ────────────────────────────────────────────────────────────────

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            place_id   TEXT PRIMARY KEY,
            name       TEXT,
            address    TEXT,
            city       TEXT,
            website    TEXT,
            domain     TEXT,
            scraped_at TIMESTAMP DEFAULT NULL
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
            UNIQUE(email, sequence_step)
        )
    """)

    conn.commit()
    _run_all_migrations(conn)
    conn.close()
    logger.info("Database ready at {}", DB_PATH)


if __name__ == "__main__":
    init_db()
