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

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
DAILY_LIMIT = 40  # conservative limit for Gmail SMTP (~100/day max)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_pending_emails(limit: int = DAILY_LIMIT):
    """
    Return pending emails that:
      - haven't been sent yet (pending/NULL status)
      - belong to a lead that has NOT replied (replied = 0)
    Ordered so step-1 always goes before step-2.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT domain, email, subject, body, sequence_step
        FROM generated_emails
        WHERE (send_status = 'pending' OR send_status IS NULL)
          AND replied = 0
        ORDER BY sequence_step ASC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def mark_send_status(email_addr: str, sequence_step: int, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE generated_emails
        SET send_status = ?, sent_at = CURRENT_TIMESTAMP
        WHERE email = ? AND sequence_step = ?
    """, (status, email_addr, sequence_step))
    conn.commit()
    conn.close()


# ── SMTP ──────────────────────────────────────────────────────────────────────

def _send_email(to_addr: str, subject: str, body: str, sender_email: str, sender_password: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = to_addr
    footer = "\n\n---\nReply STOP to unsubscribe from future emails."
    msg.attach(MIMEText(body + footer, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_addr, msg.as_string())


# ── Public runner ─────────────────────────────────────────────────────────────

def run_sender():
    # Read credentials at call time — never at module import time
    sender_email = os.getenv("SENDER_EMAIL")
    sender_password = os.getenv("SENDER_APP_PASSWORD")

    if not sender_email or not sender_password:
        logger.error("Missing SENDER_EMAIL or SENDER_APP_PASSWORD in .env")
        return

    pending = get_pending_emails()

    if not pending:
        logger.info("No pending emails to send (all sent, failed, or leads have replied).")
        return

    step_counts: dict[int, int] = {}
    logger.info("Module 4 — sending {} emails (daily limit: {})", len(pending), DAILY_LIMIT)

    sent_count = 0
    for domain, email_addr, subject, body, sequence_step in pending:
        label = "initial" if sequence_step == 1 else f"follow-up #{sequence_step - 1}"
        try:
            _send_email(email_addr, subject, body, sender_email, sender_password)
            mark_send_status(email_addr, sequence_step, "sent")
            sent_count += 1
            step_counts[sequence_step] = step_counts.get(sequence_step, 0) + 1
            logger.success(
                "[{}/{}] {} sent to {} ({})",
                sent_count, len(pending), label, email_addr, domain,
            )
        except Exception as e:
            mark_send_status(email_addr, sequence_step, "failed")
            logger.error("Failed to send {} to {}: {}", label, email_addr, e)

        time.sleep(random.uniform(25, 45))

    logger.info(
        "Sender done — {} initial, {} follow-ups, {} failed",
        step_counts.get(1, 0),
        step_counts.get(2, 0),
        len(pending) - sent_count,
    )


if __name__ == "__main__":
    init_db()
    run_sender()
