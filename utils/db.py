import sqlite3
import os
from loguru import logger

from config import DB_PATH


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            place_id TEXT PRIMARY KEY,
            name TEXT,
            address TEXT,
            city TEXT,
            website TEXT,
            domain TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS enriched_leads (
            domain TEXT,
            email TEXT,
            UNIQUE(domain, email)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS generated_emails (
            domain TEXT,
            email TEXT,
            subject TEXT,
            body TEXT,
            sent_at TIMESTAMP DEFAULT NULL,
            send_status TEXT DEFAULT 'pending',
            UNIQUE(email)
        )
    """)

    # Safe migration: add columns to existing DBs that predate sender.py
    for col, definition in [
        ("sent_at", "TIMESTAMP DEFAULT NULL"),
        ("send_status", "TEXT DEFAULT 'pending'"),
    ]:
        try:
            cursor.execute(f"ALTER TABLE generated_emails ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    conn.close()
    logger.info("Database initialized at {}", DB_PATH)


if __name__ == "__main__":
    init_db()
