import os
import random
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv
from loguru import logger

from utils.db import get_connection, init_db

# Explicit load — no implicit dependency on import order from other modules
load_dotenv()

SMTP_HOST   = "smtp.gmail.com"
SMTP_PORT   = 587
DAILY_LIMIT = 5    # warm-up phase: strict ceiling while building domain reputation
MAX_RETRIES = 3    # a 'failed' email is retried up to this many times total

# Pause between sends — simulates human behaviour, protects domain reputation.
# Range: 2–5 minutes (120–300 s).  Raise both bounds gradually as warm-up progresses.
STAGGER_MIN_S = 120   # 2 minutes
STAGGER_MAX_S = 300   # 5 minutes


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_pending_emails(limit: int = DAILY_LIMIT):
    """
    Return emails that are actionable today:
      - 'pending'  → never attempted
      - 'failed'   with failure_count < MAX_RETRIES → transient failure, retry

    Hard exclusions (AND guards):
      - replied = 0      — lead replied or unsubscribed; tracker set this flag
      - snooze_until     — lead returned an OOO / auto-responder; tracker paused
                           the sequence for 7 days. Once the timestamp passes the
                           row becomes sendable again automatically.
      - suppression_list — permanent opt-out or confirmed bounce; belt-and-
                           suspenders guard in case tracker ran before the row
                           was suppressed (e.g. manual suppression).

    Ordered: step-1 before step-2; fewer failures first within each step
    (fresh emails go out before retries).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT domain, email, subject, body, sequence_step
        FROM generated_emails
        WHERE (
                send_status = 'pending'
                OR (send_status = 'failed' AND COALESCE(failure_count, 0) < ?)
              )
          AND replied = 0
          AND (snooze_until IS NULL OR snooze_until <= CURRENT_TIMESTAMP)
          AND NOT EXISTS (
              SELECT 1 FROM suppression_list sl
              WHERE sl.email = generated_emails.email
          )
        ORDER BY sequence_step ASC, COALESCE(failure_count, 0) ASC
        LIMIT ?
    """, (MAX_RETRIES, limit))
    rows = cursor.fetchall()
    conn.close()
    return rows


def mark_send_status(email_addr: str, sequence_step: int, status: str):
    """
    Update send state.
    On 'failed': increment failure_count so retries are tracked.
    On 'sent':   standard timestamp update, failure_count unchanged.
    """
    conn = get_connection()
    cursor = conn.cursor()
    if status == "failed":
        cursor.execute("""
            UPDATE generated_emails
            SET send_status   = ?,
                sent_at       = CURRENT_TIMESTAMP,
                failure_count = COALESCE(failure_count, 0) + 1
            WHERE email = ? AND sequence_step = ?
        """, (status, email_addr, sequence_step))
    else:
        cursor.execute("""
            UPDATE generated_emails
            SET send_status = ?,
                sent_at     = CURRENT_TIMESTAMP
            WHERE email = ? AND sequence_step = ?
        """, (status, email_addr, sequence_step))
    conn.commit()
    conn.close()


# ── SMTP ──────────────────────────────────────────────────────────────────────

def _send_email(to_addr: str, subject: str, body: str, sender_email: str, sender_password: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender_email
    msg["To"]      = to_addr
    footer = "\n\n---\nReply STOP to unsubscribe from future emails."
    msg.attach(MIMEText(body + footer, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_addr, msg.as_string())


# ── Public runner ─────────────────────────────────────────────────────────────

def run_sender():
    sender_email    = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_APP_PASSWORD")

    if not sender_email or not sender_password:
        logger.error("Missing SENDER_EMAIL or SENDER_APP_PASSWORD in .env")
        return

    pending = get_pending_emails()

    if not pending:
        logger.info("No actionable emails today (all sent, exhausted retries, or leads replied).")
        return

    step_counts: dict[int, int] = {}
    logger.info("Module 4 — sending {} emails (daily limit: {}, max retries: {})",
                len(pending), DAILY_LIMIT, MAX_RETRIES)

    sent_count = 0
    for domain, email_addr, subject, body, sequence_step in pending:
        label = "initial" if sequence_step == 1 else f"follow-up #{sequence_step - 1}"
        try:
            _send_email(email_addr, subject, body, sender_email, sender_password)
            mark_send_status(email_addr, sequence_step, "sent")
            sent_count += 1
            step_counts[sequence_step] = step_counts.get(sequence_step, 0) + 1
            logger.success("[{}/{}] {} sent → {} ({})",
                           sent_count, len(pending), label, email_addr, domain)
        except Exception as e:
            mark_send_status(email_addr, sequence_step, "failed")
            logger.error("Failed to send {} → {}: {}", label, email_addr, e)

        # Human staggering — skip the final sleep (nothing left to space out)
        if sent_count < len(pending):
            wait = random.uniform(STAGGER_MIN_S, STAGGER_MAX_S)
            logger.info("Waiting {:.0f}s before next send…", wait)
            time.sleep(wait)

    logger.info("Sender done — {} initial, {} follow-ups, {} failed",
                step_counts.get(1, 0),
                step_counts.get(2, 0),
                len(pending) - sent_count)


if __name__ == "__main__":
    init_db()
    run_sender()
