import re
import time

import requests
from bs4 import BeautifulSoup
from loguru import logger

from utils.db import get_connection, init_db


def get_pending_domains():
    """Return domains that have not been enriched yet."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT l.domain, l.website
        FROM leads l
        LEFT JOIN enriched_leads el ON l.domain = el.domain
        WHERE l.website IS NOT NULL AND el.domain IS NULL
    """)
    domains = cursor.fetchall()
    conn.close()
    return domains


def save_email(domain, email):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO enriched_leads (domain, email) VALUES (?, ?)", (domain, email))
        conn.commit()
    except Exception as e:
        logger.error("Error saving email {}: {}", email, e)
    finally:
        conn.close()


EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?!png|jpg|jpeg|gif|webp)[a-zA-Z]{2,}"
)


def find_emails_on_page(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        # Parse visible text only — avoids false positives from JS/CSS/comments
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(separator=" ")
        return set(EMAIL_RE.findall(text))
    except Exception as e:
        logger.debug("Could not access {}: {}", url, e)
        return set()


def run_enricher():
    logger.info("Starting Module 2: Email Finder")
    init_db()
    targets = get_pending_domains()

    if not targets:
        logger.warning("No pending domains to enrich. Run the scraper first, or all domains are already enriched.")
        return

    logger.info("Enriching {} domains", len(targets))

    for domain, website in targets:
        logger.info("Scanning domain: {}", domain)

        if not website.startswith("http"):
            website = "https://" + website

        urls_to_check = [website, f"{website}/contact", f"{website}/about"]
        found_emails = set()

        for url in urls_to_check:
            found_emails.update(find_emails_on_page(url))
            time.sleep(1)

        if found_emails:
            for email in found_emails:
                logger.success("Email found for {}: {}", domain, email)
                save_email(domain, email.lower())
        else:
            logger.info("No emails found for {}", domain)


if __name__ == "__main__":
    run_enricher()
