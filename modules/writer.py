import json
import os
import re
import time

from anthropic import Anthropic, APIError, RateLimitError
from loguru import logger

from utils.db import get_connection, init_db


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_initial_candidates():
    """Enriched leads that don't have a step-1 draft yet."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT el.domain, el.email
        FROM enriched_leads el
        WHERE NOT EXISTS (
            SELECT 1 FROM generated_emails ge
            WHERE ge.email = el.email AND ge.sequence_step = 1
        )
    """)
    leads = cursor.fetchall()
    conn.close()
    return leads


def get_followup_candidates():
    """
    Leads whose step-1 email was sent 4+ days ago, haven't replied,
    and don't have a step-2 draft yet.
    The replied = 0 guard is the critical block: if they replied to step-1,
    we never generate (or send) a follow-up.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ge.domain, ge.email
        FROM generated_emails ge
        WHERE ge.sequence_step = 1
          AND ge.send_status   = 'sent'
          AND ge.replied       = 0
          AND ge.sent_at      <= datetime('now', '-4 days')
          AND NOT EXISTS (
              SELECT 1 FROM generated_emails ge2
              WHERE ge2.email = ge.email AND ge2.sequence_step = 2
          )
    """)
    leads = cursor.fetchall()
    conn.close()
    return leads


def save_generated_email(domain, email, subject, body, sequence_step=1):
    """
    Insert draft into generated_emails.
    Uses INSERT OR IGNORE — checks cursor.rowcount to distinguish
    a real save from a silently ignored duplicate.
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT OR IGNORE INTO generated_emails
               (domain, email, subject, body, sequence_step)
               VALUES (?, ?, ?, ?, ?)""",
            (domain, email, subject, body, sequence_step),
        )
        conn.commit()

        if cursor.rowcount == 0:
            logger.warning(
                "Duplicate silently ignored — {} already has a step-{} draft (shared inbox?). No data was written.",
                email, sequence_step,
            )
        else:
            logger.success("Step-{} draft saved for {}", sequence_step, domain)

    except Exception as e:
        logger.error("Error saving draft for {} step {}: {}", domain, sequence_step, e)
    finally:
        conn.close()


# ── Parsing ───────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Parse JSON from model output, tolerating markdown wrappers and extra text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))

    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"No JSON object found in model output: {text!r}")


# ── Core Claude call ──────────────────────────────────────────────────────────

def _draft_email(client: Anthropic, prompt: str, domain: str, step: int) -> dict | None:
    """Call Claude with up to 3 retries. Returns dict with 'subject'/'body' or None."""
    for attempt in range(1, 4):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            email_data = _extract_json(response.content[0].text)

            if "subject" not in email_data or "body" not in email_data:
                raise ValueError(f"Missing keys in JSON: {list(email_data.keys())}")

            return email_data

        except RateLimitError:
            wait = 30 * attempt
            logger.warning("Rate limit — waiting {}s (attempt {}/3)", wait, attempt)
            time.sleep(wait)

        except (APIError, ValueError, json.JSONDecodeError) as e:
            logger.warning("Attempt {}/3 failed for {} step {}: {}", attempt, domain, step, e)
            if attempt == 3:
                logger.error("All retries exhausted for {} step {}", domain, step)
            time.sleep(2 * attempt)

    return None


# ── Public runners ────────────────────────────────────────────────────────────

def run_writer():
    """Generate step-1 (initial) cold emails for newly enriched leads."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("Missing ANTHROPIC_API_KEY in .env")
        return

    client = Anthropic(api_key=api_key)
    leads = get_initial_candidates()

    if not leads:
        logger.info("No new leads need an initial email draft.")
        return

    logger.info("Module 3 — drafting {} initial emails", len(leads))

    for domain, email in leads:
        prompt = f"""
        Act as an expert B2B copywriter. Write a short, highly converting cold email (in English)
        targeting a Property Management company at the domain: {domain}.

        Our Pitch: We offer custom AI and automation software (lead scraping systems, CRM workflows)
        to help them save hours of manual work and acquire more property owners.
        Goal: Get them to reply to schedule a 10-minute discovery call.
        Tone: Professional, direct, not overly salesy.

        Return ONLY a valid JSON object with keys "subject" and "body". No markdown, no extra text.
        """

        result = _draft_email(client, prompt, domain, step=1)
        if result:
            save_generated_email(domain, email, result["subject"], result["body"], sequence_step=1)
        time.sleep(1)


def run_followup_writer():
    """
    Generate step-2 follow-up emails for leads sent step-1 >= 4 days ago
    that have NOT replied (replied = 0).
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("Missing ANTHROPIC_API_KEY in .env")
        return

    client = Anthropic(api_key=api_key)
    candidates = get_followup_candidates()

    if not candidates:
        logger.info("No leads eligible for a follow-up (none, or all have replied).")
        return

    logger.info("Module 3 — drafting {} follow-up emails", len(candidates))

    for domain, email in candidates:
        prompt = f"""
        Act as an expert B2B copywriter. Write an ultra-short follow-up email (2-3 sentences MAX)
        for a Property Management company at domain: {domain}.

        Context: We sent them a cold email 4 days ago offering custom AI automation software
        (lead scraping, CRM workflows) to save manual work and acquire more property owners.
        They haven't replied yet. This is a single gentle nudge — not pushy, no re-pitch.
        Goal: Invite them to reply and pick a 10-minute call time.

        Return ONLY a valid JSON object with keys "subject" and "body". No markdown, no extra text.
        """

        result = _draft_email(client, prompt, domain, step=2)
        if result:
            save_generated_email(domain, email, result["subject"], result["body"], sequence_step=2)
        time.sleep(1)


if __name__ == "__main__":
    init_db()
    run_writer()
    run_followup_writer()
