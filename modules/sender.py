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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT domain, email, subject, body
        FROM generated_emails
        WHERE send_status = 'pending' OR send_status IS NULL
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def mark_send_status(email_addr: str, status: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE generated_emails
        SET send_status = ?, sent_at = CURRENT_TIMESTAMP
        WHERE email = ?
    """, (status, email_addr))
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
        logger.warning("No pending emails to send. Run writer.py first.")
        return

    logger.info("Starting Module 4: sending {} emails (daily limit: {})", len(pending), DAILY_LIMIT)

    sent_count = 0
    for domain, email, subject, body in pending:
        try:
            send_email(email, subject, body)
            mark_send_status(email, "sent")
            sent_count += 1
            logger.success("[{}/{}] Sent to {} ({})", sent_count, len(pending), email, domain)
        except Exception as e:
            mark_send_status(email, "failed")
            logger.error("Failed to send to {}: {}", email, e)

        # Randomized delay to avoid spam filters and stay well under rate limits
        time.sleep(random.uniform(25, 45))

    logger.info("Sender run completed: {} sent, {} failed", sent_count, len(pending) - sent_count)


if __name__ == "__main__":
    run_sender()
