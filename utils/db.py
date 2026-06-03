import sqlite3
import os
from loguru import logger

from config import DB_PATH


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def _migrate_generated_emails(conn):
    """
    Recreate generated_emails with sequence_step support.
    Safe to run on an existing DB — detects old schema and migrates data.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(generated_emails)")
    columns = {row[1] for row in cursor.fetchall()}

    if "sequence_step" in columns:
        return  # already on new schema

    logger.info("Migrating generated_emails → multi-step campaign schema...")
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
            UNIQUE(email, sequence_step)
        )
    """)
    cursor.execute("""
        INSERT INTO generated_emails (domain, email, subject, body, sequence_step, send_status, sent_at)
        SELECT domain, email, subject, body, 1, send_status, sent_at
        FROM _generated_emails_v1
    """)
    cursor.execute("DROP TABLE _generated_emails_v1")
    conn.commit()
    logger.info("Migration complete — existing emails preserved as sequence_step=1")


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            place_id TEXT PRIMARY KEY,
            name     TEXT,
            address  TEXT,
            city     TEXT,
            website  TEXT,
            domain   TEXT
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
            UNIQUE(email, sequence_step)
        )
    """)

    conn.commit()
    _migrate_generated_emails(conn)
    conn.close()
    logger.info("Database ready at {}", DB_PATH)


if __name__ == "__main__":
    init_db()
