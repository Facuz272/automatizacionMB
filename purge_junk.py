"""
One-shot cleanup for role/generic addresses that entered the database before
the enricher filter was hardened (the `donotreply@`, `info+canned.response@`,
`sales@` rows that kept receiving sends and follow-ups).

The enricher only filters at scrape time — it never purges what's already
stored. This script applies the *current* is_valid_outreach_email rules to
existing rows and:

  1. Adds every junk address to suppression_list (reason='manual') so it can
     never be targeted again, even if re-scraped.
  2. Deletes its un-sent rows (pending / failed) from generated_emails so the
     sender stops attempting it immediately.
  3. Deletes it from enriched_leads so the writer never drafts for it again.

Already-sent rows are LEFT in place for history; the suppression entry stops
any further sends or follow-ups to them.

Usage:
    python purge_junk.py            # dry run — report only, no writes
    python purge_junk.py --apply    # perform the purge
"""

import sys

from loguru import logger

from modules.enricher import is_valid_outreach_email
from utils.db import get_connection, init_db


def _collect_junk(conn) -> set[tuple[str, str]]:
    """Return {(email, domain)} across both tables that fail the current filter."""
    cursor = conn.cursor()
    junk: set[tuple[str, str]] = set()

    cursor.execute("SELECT DISTINCT email, domain FROM enriched_leads")
    for email, domain in cursor.fetchall():
        if email and not is_valid_outreach_email(email):
            junk.add((email.lower(), domain or ""))

    cursor.execute("SELECT DISTINCT email, domain FROM generated_emails")
    for email, domain in cursor.fetchall():
        if email and not is_valid_outreach_email(email):
            junk.add((email.lower(), domain or ""))

    return junk


def purge(apply: bool) -> None:
    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    junk = _collect_junk(conn)
    if not junk:
        logger.success("No junk addresses found — database is clean.")
        conn.close()
        return

    logger.warning("Found {} junk address(es):", len(junk))
    for email, domain in sorted(junk):
        logger.warning("  {}  (domain: {})", email, domain or "?")

    if not apply:
        logger.info("")
        logger.info("DRY RUN — nothing changed. Re-run with --apply to purge.")
        conn.close()
        return

    suppressed = unsent_deleted = enriched_deleted = 0
    for email, domain in junk:
        cursor.execute(
            """INSERT OR IGNORE INTO suppression_list (email, domain, reason)
               VALUES (?, ?, 'manual')""",
            (email, domain),
        )
        suppressed += cursor.rowcount

        cursor.execute(
            """DELETE FROM generated_emails
               WHERE email = ?
                 AND send_status IN ('pending', 'failed')""",
            (email,),
        )
        unsent_deleted += cursor.rowcount

        cursor.execute("DELETE FROM enriched_leads WHERE email = ?", (email,))
        enriched_deleted += cursor.rowcount

    conn.commit()
    conn.close()

    logger.success("Purge complete:")
    logger.success("  {} address(es) added to suppression_list", suppressed)
    logger.success("  {} un-sent generated_emails row(s) deleted", unsent_deleted)
    logger.success("  {} enriched_leads row(s) deleted", enriched_deleted)


if __name__ == "__main__":
    purge(apply="--apply" in sys.argv)
