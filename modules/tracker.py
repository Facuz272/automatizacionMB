"""
Module 5 — Reply Tracker

Connects to Gmail via IMAP, scans the inbox for recent emails,
and matches the sender address against generated_emails.
On match: sets replied = 1, blocking all future follow-ups and sends
for that lead (enforced in writer.py and sender.py via AND replied = 0).

Run order in pipeline: FIRST — before writer and sender, so follow-up
logic already has fresh reply state when it runs.
"""

import email
import email.utils
import imaplib
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv
from loguru import logger

from utils.db import get_connection, init_db

load_dotenv()

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# 2-day lookback covers the UTC/ET boundary gap:
# a reply at 10 PM ET Sunday = 3 AM UTC Monday; with 1-day lookback the Monday
# pipeline (9 AM ET = 2 PM UTC) using "SINCE Monday UTC" would miss it.
# 2-day lookback is safe because mark_as_replied() is idempotent (AND replied=0).
LOOKBACK_DAYS = 2


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_tracked_addresses() -> set[str]:
    """Return addresses we've sent to that haven't replied yet."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT email
        FROM generated_emails
        WHERE send_status = 'sent'
          AND replied = 0
    """)
    addresses = {row[0].lower().strip() for row in cursor.fetchall()}
    conn.close()
    return addresses


def mark_as_replied(email_addr: str) -> int:
    """
    Set replied=1 for every sequence step of this address.
    The AND replied=0 clause makes repeated calls idempotent:
    rowcount > 0 only on the first detection.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE generated_emails
        SET replied = 1
        WHERE email   = ?
          AND replied = 0
    """, (email_addr,))
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected


# ── IMAP ──────────────────────────────────────────────────────────────────────

def _parse_from_address(raw_from: str) -> str:
    """Extract the bare address from 'Display Name <addr@domain.com>'."""
    _, addr = email.utils.parseaddr(raw_from)
    return addr.lower().strip()


def _fetch_recent_senders(mail: imaplib.IMAP4_SSL, lookback_days: int) -> list[tuple[str, str]]:
    """
    Return (from_address, subject) pairs for inbox messages received in
    the last `lookback_days` days.

    Fetches ONLY the From + Subject headers — never the full body or
    attachments. This keeps RAM usage flat regardless of inbox size.
    Previously using RFC822 could pull 50-100 MB on a busy inbox.
    """
    since_date = (datetime.now() - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    _, message_ids = mail.search(None, f"SINCE {since_date}")

    if not message_ids[0]:
        return []

    ids = message_ids[0].split()
    logger.info("Scanning {} inbox messages since {} (headers only)", len(ids), since_date)

    senders = []
    for msg_id in ids:
        try:
            # BODY[HEADER.FIELDS ...] downloads ~200 bytes per message
            # instead of the full RFC822 payload (headers + body + attachments)
            _, msg_data = mail.fetch(msg_id, "(BODY[HEADER.FIELDS (FROM SUBJECT)])")
            msg = email.message_from_bytes(msg_data[0][1])
            from_addr = _parse_from_address(msg.get("From", ""))
            subject   = msg.get("Subject", "(no subject)")
            if from_addr:
                senders.append((from_addr, subject))
        except Exception as exc:
            logger.debug("Could not parse message {}: {}", msg_id, exc)

    return senders


# ── Public runner ─────────────────────────────────────────────────────────────

def run_tracker():
    sender_email    = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_APP_PASSWORD")

    if not sender_email or not sender_password:
        logger.error("Missing SENDER_EMAIL or SENDER_APP_PASSWORD in .env")
        return

    tracked = get_tracked_addresses()
    if not tracked:
        logger.info("Tracker: no active outbound leads to watch for replies.")
        return

    logger.info("Tracker: checking inbox for replies from {} active leads...", len(tracked))

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(sender_email, sender_password)
        mail.select("INBOX")
        recent_senders = _fetch_recent_senders(mail, LOOKBACK_DAYS)
        mail.logout()
    except imaplib.IMAP4.error as exc:
        logger.error("IMAP authentication failed: {}", exc)
        return
    except Exception as exc:
        logger.error("Tracker IMAP error: {}", exc)
        return

    if not recent_senders:
        logger.info("Tracker: no new inbox messages in the last {} day(s).", LOOKBACK_DAYS)
        return

    new_replies = 0
    for from_addr, subject in recent_senders:
        if from_addr not in tracked:
            continue

        affected = mark_as_replied(from_addr)
        if affected > 0:
            new_replies += 1
            logger.info("")
            logger.info("━" * 58)
            logger.success("  ★  REPLY DETECTED — LEAD IS HOT  ★")
            logger.success("  From    : {}", from_addr)
            logger.success("  Subject : {}", subject)
            logger.success("  Action  : replied=1 — all follow-ups BLOCKED")
            logger.info("━" * 58)
            logger.info("")

    if new_replies == 0:
        logger.info("Tracker: no new replies from tracked leads.")
    else:
        logger.success("Tracker done — {} new reply(ies) detected and locked.", new_replies)


if __name__ == "__main__":
    init_db()
    run_tracker()
