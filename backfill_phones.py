"""
One-shot backfill: populate leads.phone for rows scraped before phone capture
was added to the scraper.

The scraper now stores each business's phone (Google Places
formatted_phone_number). Rows created before that column existed have phone
NULL, so this script re-fetches Place Details for every lead missing a phone
and fills it in. New scrapes already include the phone, so this is only needed
once (and again only if you ever import leads from another source).

Costs one Google Places Details call per lead with a missing phone
(a few cents total for the current database).

Usage:
    python backfill_phones.py           # dry run — count only, no API calls, no writes
    python backfill_phones.py --apply   # fetch phones from Google and write them
"""

import sys
import time

from dotenv import load_dotenv
from loguru import logger

# Load .env before importing modules that read API keys at import time.
load_dotenv()

from modules.scraper import build_client, fetch_place_details  # noqa: E402
from utils.db import get_connection, init_db                    # noqa: E402


def _rows_missing_phone(conn):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT place_id, name FROM leads WHERE phone IS NULL OR phone = ''"
    )
    return cursor.fetchall()


def backfill(apply: bool) -> None:
    init_db()
    conn = get_connection()
    rows = _rows_missing_phone(conn)

    if not rows:
        logger.success("Every lead already has a phone — nothing to backfill.")
        conn.close()
        return

    logger.info("{} lead(s) missing a phone.", len(rows))

    if not apply:
        logger.info("")
        logger.info("DRY RUN — no API calls, nothing changed. Re-run with --apply to fetch phones.")
        conn.close()
        return

    client = build_client()
    cursor = conn.cursor()
    filled = no_phone = failed = 0

    for place_id, name in rows:
        try:
            details = fetch_place_details(client, place_id)
        except Exception as exc:
            logger.warning("Could not fetch {} ({}): {}", name, place_id, exc)
            failed += 1
            continue

        phone = (
            details.get("formatted_phone_number")
            or details.get("international_phone_number")
        )
        if phone:
            cursor.execute(
                "UPDATE leads SET phone = ? WHERE place_id = ?", (phone, place_id)
            )
            filled += 1
            logger.success("{} → {}", name, phone)
        else:
            no_phone += 1
            logger.info("{} → no phone listed on Google", name)

        time.sleep(0.05)  # stay under Google QPS limit

    conn.commit()
    conn.close()

    logger.success("Backfill complete:")
    logger.success("  {} phone(s) filled in", filled)
    logger.success("  {} business(es) with no phone listed on Google", no_phone)
    if failed:
        logger.warning("  {} lookup(s) failed (network / API) — re-run to retry", failed)


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv)
