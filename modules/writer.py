import json
import os
import re
import time

from anthropic import Anthropic, APIError, RateLimitError
from loguru import logger

from utils.db import get_connection, init_db


# ── System prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an elite, top-performing B2B Sales Representative for 'MB Softwash Miami'.
Your objective is to write a highly personalized, concise cold email to a Property Management company to book a quick 10-minute discovery call.

=== BUSINESS CONTEXT (OUR VALUE PROPOSITION) ===
- What we do: We take exterior maintenance completely off the property manager's plate.
- The Pain We Solve: We prevent long-term damage (mold, algae, dirt) and minimize tenant complaints, reducing maintenance costs and preserving property value.
- Our Differentiator: We are a proactive partner, not just a vendor. We provide before-and-after photo documentation, communicate clearly, use safe soft-washing methods, and make it stress-free to manage multiple properties.
- The Hook/Offer: A FREE Property Exterior Inspection & Maintenance Assessment (includes a photo report, recommendations, and a no-obligation estimate with a 100% satisfaction guarantee).

=== PROSPECT DATA ===
Company Name: {company_name}
Website Information: {website_text}

=== STRICT WRITING RULES ===
1. THE OPENING: You MUST write a highly specific, personalized first sentence based ONLY on the Website Information in PROSPECT DATA above. Prove you actually read their website.
2. THE PITCH: Connect their specific business focus to curb appeal, tenant satisfaction, or avoiding costly maintenance.
3. THE OFFER: Briefly introduce MB Softwash Miami and offer our Free Exterior Inspection & photo report.
4. THE CTA: End with a single, low-friction question.
5. TONE & LENGTH: Maximum 100-120 words. Confident, conversational, direct, strictly professional.
6. FORBIDDEN PHRASES: Never use "I hope this email finds you well", "We are a leading company", or corporate buzzwords.
7. SIGNATURE: Sign exactly as: Best, / Tomas / MB Softwash Miami
"""

FOLLOWUP_SYSTEM_PROMPT = """\
You are an expert B2B sales copywriter for 'MB Softwash Miami', an exterior maintenance company serving property management companies in Miami.

You are writing a brief follow-up to a cold email sent 4 days ago. The original email offered a FREE Property Exterior Inspection & Maintenance Assessment.

Company being contacted: {company_name}

Write an ultra-short follow-up (2-3 sentences MAX). Be polite, not pushy — a single gentle nudge to check if they saw the previous message.
- Do not re-pitch the full offer.
- End with a simple, low-friction question (e.g., "Would this week work for a quick chat?").
- Sign exactly as: Best, / Tomas / MB Softwash Miami
"""

_JSON_INSTRUCTION = (
    'Return ONLY a valid JSON object with exactly two keys: "subject" and "body". '
    "No markdown, no extra text, no explanation."
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_initial_candidates():
    """
    Enriched leads that don't have a step-1 draft yet.
    Returns (domain, email, company_name, website_text).
    company_name falls back to domain if leads.name is absent.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT el.domain,
               el.email,
               COALESCE(l.name, el.domain)         AS company_name,
               COALESCE(l.website_text, '')         AS website_text
        FROM enriched_leads el
        LEFT JOIN leads l ON l.domain = el.domain
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
    Returns (domain, email, company_name).
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ge.domain,
               ge.email,
               COALESCE(l.name, ge.domain) AS company_name
        FROM generated_emails ge
        LEFT JOIN leads l ON l.domain = ge.domain
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

def _draft_email(
    client: Anthropic,
    system: str,
    user_message: str,
    domain: str,
    step: int,
) -> dict | None:
    """Call Claude with up to 3 retries. Returns dict with 'subject'/'body' or None."""
    for attempt in range(1, 4):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=600,
                system=system,
                messages=[{"role": "user", "content": user_message}],
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

    for domain, email, company_name, website_text in leads:
        system = SYSTEM_PROMPT.format(
            company_name=company_name,
            website_text=website_text or f"(no website text captured — domain: {domain})",
        )

        result = _draft_email(client, system, _JSON_INSTRUCTION, domain, step=1)
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

    for domain, email, company_name in candidates:
        system = FOLLOWUP_SYSTEM_PROMPT.format(company_name=company_name)

        result = _draft_email(client, system, _JSON_INSTRUCTION, domain, step=2)
        if result:
            save_generated_email(domain, email, result["subject"], result["body"], sequence_step=2)
        time.sleep(1)


if __name__ == "__main__":
    init_db()
    run_writer()
    run_followup_writer()
