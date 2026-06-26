import re
import time

import requests
from bs4 import BeautifulSoup
from loguru import logger

from modules.apollo import ApolloTransientError, find_decision_maker
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


def save_email(domain, email, full_name=None, title=None, source="scrape"):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT OR IGNORE INTO enriched_leads
                   (domain, email, full_name, title, source)
               VALUES (?, ?, ?, ?, ?)""",
            (domain, email, full_name, title, source),
        )
        conn.commit()
    except Exception as e:
        logger.error("Error saving email {}: {}", email, e)
    finally:
        conn.close()


def mark_domain_scraped(domain: str, website_text: str = ""):
    """
    Stamp scraped_at and persist homepage text on the lead row.
    Called whether or not an email was found — prevents infinite-retry loop.
    website_text is capped at 1 500 chars so the DB column stays lean.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE leads
           SET scraped_at   = CURRENT_TIMESTAMP,
               website_text = ?
           WHERE domain = ?""",
        (website_text[:1500] if website_text else None, domain),
    )
    conn.commit()
    conn.close()


# ── Scraping ──────────────────────────────────────────────────────────────────

EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.(?!png|jpg|jpeg|gif|webp)[a-zA-Z]{2,}"
)

# Local-parts that indicate a role/generic inbox or spam-trap address.
# Sending to these tanks deliverability and never reaches a decision-maker.
_GENERIC_LOCAL_PARTS = frozenset({
    "info", "contact", "hello", "hola", "hi",
    "admin", "administrator", "webmaster", "hostmaster", "abuse",
    "postmaster", "mailer-daemon", "noreply", "no-reply", "donotreply",
    "support", "help", "helpdesk", "service", "customerservice",
    "sales", "marketing", "billing", "invoices", "accounts",
    "team", "office", "general", "reception", "enquiries", "enquiry",
    "mail", "email", "privacy", "legal", "compliance",
    "news", "newsletter", "updates", "notifications",
    "careers", "jobs", "hr", "hiring",
    "media", "press", "pr", "partners",
})


def is_valid_outreach_email(email: str) -> bool:
    """
    Return True only if the email address looks like a real person's inbox.

    Rejects:
      - Role / generic local-parts          (info@, admin@, noreply@, …)
      - Plus-addressed role inboxes          (info+canned.response@, sales+x@)
      - Compound role local-parts            (info.miami@, sales.team@, support-us@)
      - Addresses longer than 254 chars (RFC 5321 hard limit)
      - Addresses with no '@'

    The exact-match check this replaced let plus-addressing and dotted role
    inboxes through — that is exactly how `info+canned.response@…` slipped past
    the filter and received a follow-up. We now normalise the local-part
    (drop the +tag) and inspect every token split on '.', '_' and '-'.

    Does NOT validate domain MX records — that's a pre-send step (ZeroBounce).
    """
    if "@" not in email or len(email) > 254:
        return False
    local = email.split("@")[0].lower()
    base = local.split("+", 1)[0]              # strip plus-tag: info+x → info
    if base in _GENERIC_LOCAL_PARTS:
        return False
    # Token check catches compound role inboxes (info.miami, sales-team).
    tokens = re.split(r"[._-]", base)
    return not any(tok in _GENERIC_LOCAL_PARTS for tok in tokens)


def scrape_page(url: str) -> tuple[set[str], str]:
    """
    Fetch a page and return (emails_found, clean_page_text).
    clean_page_text has whitespace collapsed — ready for DB storage.
    Returns (set(), "") on any fetch / parse error.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        text = soup.get_text(separator=" ")
        clean_text = " ".join(text.split())          # collapse all whitespace
        return set(EMAIL_RE.findall(text)), clean_text
    except Exception as e:
        logger.debug("Could not access {}: {}", url, e)
        return set(), ""


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

        # Always scrape the homepage — even when Apollo supplies the email, the
        # writer needs website_text for personalization.
        urls_to_check = [website, f"{website}/contact", f"{website}/about"]
        found_emails: set[str] = set()
        homepage_text = ""

        for i, url in enumerate(urls_to_check):
            emails, page_text = scrape_page(url)
            found_emails.update(emails)
            if i == 0 and page_text:
                homepage_text = page_text
            time.sleep(1)

        # ── Priority 1: Apollo decision-maker ─────────────────────────────────
        # Go straight to the owner/director instead of a footer's info@.
        # A transient Apollo failure must NOT burn the domain: skip it WITHOUT
        # stamping scraped_at so the next cycle retries it cleanly.
        try:
            decision_maker = find_decision_maker(domain)
        except ApolloTransientError as exc:
            logger.warning(
                "Apollo transient failure for {} — skipping, will retry next cycle: {}",
                domain, exc,
            )
            continue  # do NOT mark_domain_scraped — domain stays pending

        if decision_maker and is_valid_outreach_email(decision_maker[0]):
            dm_email, dm_name, dm_title = decision_maker
            save_email(
                domain, dm_email.lower(),
                full_name=dm_name or None,
                title=dm_title or None,
                source="apollo",
            )
            logger.success(
                "Decision-maker for {}: {} <{}> ({})",
                domain, dm_name or "?", dm_email, dm_title or "no title",
            )
            mark_domain_scraped(domain, homepage_text)
            continue

        # ── Priority 2: scraped personal emails (fallback) ────────────────────
        personal_emails = {em for em in found_emails if is_valid_outreach_email(em)}
        filtered_out    = len(found_emails) - len(personal_emails)

        if filtered_out:
            logger.debug(
                "{} generic/trap address(es) discarded for {}",
                filtered_out, domain,
            )

        if personal_emails:
            for em in personal_emails:
                logger.success("Email found for {}: {}", domain, em)
                save_email(domain, em.lower(), source="scrape")
        else:
            logger.info("No usable email for {} — marking as scraped to skip next time", domain)

        # Always stamp scraped_at + save homepage text, even on zero-result domains
        mark_domain_scraped(domain, homepage_text)

    logger.info("Enricher complete")


if __name__ == "__main__":
    init_db()
    run_enricher()
