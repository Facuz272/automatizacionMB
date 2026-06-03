import os
import random
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger

from utils.db import get_connection, init_db

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_APP_PASSWORD = os.getenv("SENDER_APP_PASSWORD")
DAILY_LIMIT = 40  # conservative limit for Gmail SMTP (~100/day max)


def get_pending_emails(limit: int = DAILY_LIMIT):
    """Return pending emails ordered so step-1 always goes before step-2."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT domain, email, subject, body, sequence_step
        FROM generated_emails
        WHERE send_status = 'pending' OR send_status IS NULL
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


def send_email(to_addr: str, subject: str, body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = to_addr
    footer = "\n\n---\nReply STOP to unsubscribe from future emails."
    msg.attach(MIMEText(body + footer, "plain"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, to_addr, msg.as_string())


def run_sender():
    if not SENDER_EMAIL or not SENDER_APP_PASSWORD:
        logger.error("Missing SENDER_EMAIL or SENDER_APP_PASSWORD in .env")
        return

    init_db()
    pending = get_pending_emails()

    if not pending:
        logger.info("No pending emails to send.")
        return

    step_counts = {1: 0, 2: 0}
    logger.info("Module 4 — sending {} emails (daily limit: {})", len(pending), DAILY_LIMIT)

    sent_count = 0
    for domain, email, subject, body, sequence_step in pending:
        label = "initial" if sequence_step == 1 else f"follow-up #{sequence_step - 1}"
        try:
            send_email(email, subject, body)
            mark_send_status(email, sequence_step, "sent")
            sent_count += 1
            step_counts[sequence_step] = step_counts.get(sequence_step, 0) + 1
            logger.success("[{}/{}] {} sent to {} ({})", sent_count, len(pending), label, email, domain)
        except Exception as e:
            mark_send_status(email, sequence_step, "failed")
            logger.error("Failed to send {} to {}: {}", label, email, e)

        time.sleep(random.uniform(25, 45))

    logger.info(
        "Sender done — {} initial, {} follow-ups, {} failed",
        step_counts.get(1, 0),
        step_counts.get(2, 0),
        len(pending) - sent_count,
    )


if __name__ == "__main__":
    run_sender()
