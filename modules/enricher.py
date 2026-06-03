import re
import time

import requests
from bs4 import BeautifulSoup
from loguru import logger

from utils.db import get_connection, init_db


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_pending_domains():
    """
    Return domains where scraped_at IS NULL — meaning we've never attempted
    to find emails there, regardless of whether we found any.
    This prevents infinite re-scraping of domains with no public email.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT domain, website
        FROM leads
        WHERE website IS NOT NULL
          AND scraped_at IS NULL
    """)
    domains = cursor.fetchall()
    conn.close()
    return domains


def save_email(domain, email):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO enriched_leads (domain, email) VALUES (?, ?)",
            (domain, email),
        )
        conn.commit()
    except Exception as e:
        logger.error("Error saving email {}: {}", email, e)
    finally:
        conn.close()


def mark_domain_scraped(domain):
    """
    Stamp scraped_at on the lead row whether or not an email was found.
    This is what prevents the infinite-retry loop.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE leads SET scraped_at = CURRENT_TIMESTAMP WHERE domain = ?",
        (domain,),
    )
    conn.commit()
    conn.close()


# ── Scraping ──────────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?!png|jpg|jpeg|gif|webp)[a-zA-Z]{2,}"
)


def find_emails_on_page(url):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(separator=" ")
        return set(EMAIL_RE.findall(text))
    except Exception as e:
        logger.debug("Could not access {}: {}", url, e)
        return set()


# ── Public runner ─────────────────────────────────────────────────────────────

def run_enricher():
    logger.info("Module 2: Email Finder starting")
    targets = get_pending_domains()

    if not targets:
        logger.info("No unscraped domains — enricher has nothing to do.")
        return

    logger.info("Enriching {} domains", len(targets))

    for domain, website in targets:
        logger.info("Scanning: {}", domain)

        if not website.startswith("http"):
            website = "https://" + website

        urls_to_check = [website, f"{website}/contact", f"{website}/about"]
        found_emails = set()

        for url in urls_to_check:
            found_emails.update(find_emails_on_page(url))
            time.sleep(1)

        if found_emails:
            for em in found_emails:
                logger.success("Email found for {}: {}", domain, em)
                save_email(domain, em.lower())
        else:
            logger.info("No emails found for {} — marking as scraped to skip next time", domain)

        # Always stamp scraped_at, even on zero-result domains
        mark_domain_scraped(domain)

    logger.info("Enricher complete")


if __name__ == "__main__":
    init_db()
    run_enricher()
