"""
Module 5 — Reply Tracker

Connects to Gmail via IMAP, scans the inbox for recent emails, and:

  1. HOT REPLY    — sender is in our active outbound set → sets replied=1,
                    blocking all future follow-ups and sends for that lead.

  2. UNSUBSCRIBE  — subject contains opt-out keywords (CAN-SPAM compliance)
                    → sets replied=1 AND adds to suppression_list so the
                    address is permanently excluded from future campaigns.

  3. BOUNCE       — message is from mailer-daemon / Delivery System
                    → tries to identify the original recipient via subject
                    matching, marks send_status='bounced', adds to
                    suppression_list so we never attempt that address again.

Run order in pipeline: FIRST — before writer and sender, so all three signals
are reflected before the day's drafting and sending begins.
"""

import email
import email.utils
import imaplib
import os
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv
from loguru import logger

from utils.db import get_connection, init_db

load_dotenv()

IMAP_HOST    = "imap.gmail.com"
IMAP_PORT    = 993
LOOKBACK_DAYS = 2   # see original comment about UTC/ET boundary


# ── Detection vocabularies ────────────────────────────────────────────────────

# Subject keywords that constitute an unsubscribe request (CAN-SPAM §7(a)(3))
UNSUBSCRIBE_KEYWORDS: frozenset[str] = frozenset({
    "unsubscribe", "stop", "opt out", "opt-out", "optout",
    "remove me", "remove my email", "take me off", "take me off your list",
    "no more emails", "no more contact", "do not email", "don't email",
    "dont email", "please remove", "please unsubscribe",
})

# Sender local-parts / display-name patterns → bounce notification
BOUNCE_FROM_PATTERNS: frozenset[str] = frozenset({
    "mailer-daemon", "postmaster", "mail delivery subsystem",
    "delivery system", "email delivery", "mail system",
    "delivery subsystem",
})

# Subject keywords that flag a bounce notification
BOUNCE_SUBJECT_KEYWORDS: frozenset[str] = frozenset({
    "undeliverable", "delivery status notification",
    "delivery failure", "failed to deliver",
    "delivery has failed", "returned mail",
    "mail delivery failure", "non-delivery report",
    "message delivery failed",
})

# Ordered longest-first so the most specific prefix is stripped preferentially
_BOUNCE_PREFIXES: tuple[str, ...] = (
    "delivery status notification (failure): ",
    "delivery status notification (success): ",
    "delivery status notification: ",
    "mail delivery subsystem: ",
    "returned mail: see transcript for details: ",
    "auto: delivery failure: ",
    "failure notice: ",
    "undeliverable: ",
    "returned mail: ",
)


# ── Classification helpers ────────────────────────────────────────────────────

def _is_unsubscribe_request(subject: str) -> bool:
    """True if the subject line contains any known opt-out phrase."""
    s = subject.lower()
    return any(kw in s for kw in UNSUBSCRIBE_KEYWORDS)


def _is_bounce_notification(from_addr: str, subject: str) -> bool:
    """True if the message looks like a Mail Delivery System bounce report."""
    from_lower    = from_addr.lower()
    subject_lower = subject.lower()
    return (
        any(p in from_lower    for p in BOUNCE_FROM_PATTERNS)
        or any(kw in subject_lower for kw in BOUNCE_SUBJECT_KEYWORDS)
    )


def _extract_original_subject(bounce_subject: str) -> str | None:
    """
    Strip known bounce-notification prefixes to recover the original subject.
    Returns None if no prefix matched or the remainder is too short to be
    a real subject.
    """
    s = bounce_subject.strip()
    for prefix in _BOUNCE_PREFIXES:
        if s.lower().startswith(prefix):
            original = s[len(prefix):].strip()
            return original if len(original) > 5 else None
    return None


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
    AND replied=0 clause makes repeated calls idempotent.
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


def add_to_suppression_list(email_addr: str, domain: str, reason: str) -> int:
    """
    Permanently suppress an address.
    reason: 'unsubscribe' | 'bounce' | 'manual'
    INSERT OR IGNORE makes repeated calls idempotent.
    Returns 1 if newly inserted, 0 if already present.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT OR IGNORE INTO suppression_list (email, domain, reason)
           VALUES (?, ?, ?)""",
        (email_addr.lower().strip(), domain, reason),
    )
    inserted = cursor.rowcount
    conn.commit()
    conn.close()
    return inserted


def _mark_as_bounced(email_addr: str) -> int:
    """
    Flip send_status → 'bounced' for all sent rows of this address.
    Leaves reply state unchanged (bounce ≠ reply).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE generated_emails
           SET send_status = 'bounced'
           WHERE email       = ?
             AND send_status = 'sent'""",
        (email_addr,),
    )
    affected = cursor.rowcount
    conn.commit()
    conn.close()
    return affected


def _resolve_bounced_email(original_subject: str) -> tuple[str, str] | None:
    """
    Cross-reference the original subject against generated_emails to find
    which outbound address the bounce corresponds to.

    Uses a prefix match on the first 80 chars of the subject — precise
    enough to avoid false positives while tolerating minor Gmail re-wrapping.

    Returns (email_addr, domain) or None.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT email, domain
           FROM generated_emails
           WHERE send_status = 'sent'
             AND subject LIKE ?
           LIMIT 1""",
        (f"{original_subject[:80]}%",),
    )
    row = cursor.fetchone()
    conn.close()
    return row  # (email, domain) or None


# ── IMAP ──────────────────────────────────────────────────────────────────────

def _parse_from_address(raw_from: str) -> str:
    """Extract the bare address from 'Display Name <addr@domain.com>'."""
    _, addr = email.utils.parseaddr(raw_from)
    return addr.lower().strip()


def _fetch_recent_senders(
    mail: imaplib.IMAP4_SSL,
    lookback_days: int,
) -> list[tuple[str, str]]:
    """
    Return (from_address, subject) pairs for inbox messages received in
    the last `lookback_days` days.

    Fetches ONLY From + Subject headers (~200 bytes/message).
    RAM usage stays flat regardless of inbox size.
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
            _, msg_data = mail.fetch(msg_id, "(BODY[HEADER.FIELDS (FROM SUBJECT)])")
            msg       = email.message_from_bytes(msg_data[0][1])
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

    new_replies      = 0
    new_unsubscribes = 0
    new_bounces      = 0

    for from_addr, subject in recent_senders:

        # ── 1. Bounce detection — must check BEFORE reply / unsubscribe ─────────
        # Bounces originate from mailer-daemon, never from a real lead address,
        # so they won't be in `tracked`.  We identify them by sender + subject,
        # then try to resolve which of our emails bounced via subject matching.
        if _is_bounce_notification(from_addr, subject):
            original_subject = _extract_original_subject(subject)
            if original_subject:
                resolved = _resolve_bounced_email(original_subject)
                if resolved:
                    bounced_email, bounced_domain = resolved
                    _mark_as_bounced(bounced_email)
                    newly_suppressed = add_to_suppression_list(
                        bounced_email, bounced_domain, "bounce"
                    )
                    if newly_suppressed:
                        new_bounces += 1
                        logger.warning(
                            "Bounce detected — {} suppressed (domain: {})",
                            bounced_email, bounced_domain,
                        )
                else:
                    logger.debug(
                        "Bounce received but could not resolve original recipient "
                        "from subject: {!r}", subject,
                    )
            else:
                logger.debug("Bounce notification with unrecognised subject format: {!r}", subject)
            continue  # bounce notifications are never hot replies

        # ── 2. Unsubscribe detection ─────────────────────────────────────────────
        # Checked for ALL senders, not just tracked ones — a lead may have replied
        # to an earlier step already (replied=1) but still sends a STOP.
        # mark_as_replied is idempotent; add_to_suppression_list is idempotent.
        if _is_unsubscribe_request(subject):
            domain = from_addr.split("@")[-1] if "@" in from_addr else ""
            mark_as_replied(from_addr)   # blocks further follow-ups immediately
            newly_suppressed = add_to_suppression_list(from_addr, domain, "unsubscribe")
            if newly_suppressed:
                new_unsubscribes += 1
                logger.warning(
                    "Unsubscribe request from {} — permanently suppressed", from_addr
                )
            continue

        # ── 3. Hot reply from a tracked lead ────────────────────────────────────
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

    # ── Summary ──────────────────────────────────────────────────────────────
    if new_replies == 0 and new_unsubscribes == 0 and new_bounces == 0:
        logger.info("Tracker: nothing actionable in the last {} day(s).", LOOKBACK_DAYS)
    else:
        if new_replies:
            logger.success("Tracker: {} hot reply(ies) locked.", new_replies)
        if new_unsubscribes:
            logger.warning("Tracker: {} unsubscribe(s) honoured and suppressed.", new_unsubscribes)
        if new_bounces:
            logger.warning("Tracker: {} bounce(s) detected and suppressed.", new_bounces)


if __name__ == "__main__":
    init_db()
    run_tracker()
