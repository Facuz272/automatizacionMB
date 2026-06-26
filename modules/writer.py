import json
import os
import re
import time

from anthropic import Anthropic, APIError, RateLimitError
from loguru import logger

from utils.db import get_connection, init_db

# ── Limits ────────────────────────────────────────────────────────────────────

# Draft at most this many step-1 emails per pipeline run.
# Keeps the pending queue in sync with DAILY_LIMIT in sender.py (40) while
# leaving headroom for follow-up steps.  Raise once sending infrastructure
# (domain warm-up, multiple accounts) supports higher volume.
WRITER_DAILY_LIMIT = 80


# ── System prompts ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior sales rep at MB Softwash Miami who has written thousands of cold emails.
You do not sound like an AI. You do not follow templates. Every email you write feels like
it came from a specific human who spent five minutes on the prospect's website — not from
a mail merge that spent five milliseconds.

=== MB SOFTWASH CONTEXT ===
We handle exterior maintenance for property managers: soft washing, mold/algae removal,
before-and-after photo documentation. Core offer: FREE Property Exterior Inspection —
full photo report, zero-obligation estimate, 100% satisfaction guarantee.

=== PROSPECT DATA ===
Company Name: {company_name}
Decision-maker: {recipient_name}
Website Information: {website_text}
Google Reviews Signal: {rating_signal}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 0 — GREETING (first line of the body, mandatory)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The body MUST begin with exactly this line, on its own, followed by a blank line:

  {recipient_greeting}

Do not invent or guess a name, do not add a surname or title, do not write
"Dear". Use the greeting above verbatim, then start your opening line.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 1 — BANNED PHRASES (any of these = rejected output)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Subject bans : "quick question", "quick question about", "regarding",
               "exterior maintenance strategy", "checking in"

Opening bans : "I noticed", "I saw that", "I was looking at your website",
               "I came across", "I recently came across", "I wanted to reach out",
               "I hope this finds you", "I wanted to touch base",
               "As a property manager", "I came across your company"

Body bans    : "Hope this email finds you well", "We are a leading",
               "state-of-the-art", "synergy", "leverage", "circle back",
               "touch base", "value-add", "pain point", "at the end of the day"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 2 — SUBJECT LINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 2 to 5 words only
- All lowercase — it should look like an internal forward, not a campaign
- No question marks in the subject line
- Must feel like a colleague sent it, not a marketing tool

Tone reference — do NOT copy these, create your own based on the prospect:
  mold season in doral
  exterior check, free
  building walkthrough offer
  before the rainy season
  algae on the facade
  property upkeep offer

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 3 — OPENING LINE (choose exactly ONE framework)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Read the prospect data, then pick the framework that fits. Do not blend them.

  FRAMEWORK A — DECLARATIVE (best when website_text has specific scale data):
  State a fact about their operation using third-person framing — no "I" at all.
  Pattern : "[Specific operational detail] means [concrete consequence]."
  Example : "Managing 800 units across Doral and Hialeah means a single exterior
             issue can generate fifty maintenance tickets before it hits a report."

  FRAMEWORK B — PAIN QUESTION (best when Reviews Signal is BELOW AVERAGE or BORDERLINE):
  Open with one question that surfaces a problem they already feel but haven't named.
  Pattern : "[Specific operational question tied to their scale or review signal]?"
  Example : "How many of your maintenance complaints last quarter started as something
             as simple as algae on a walkway or mold on a building facade?"

  FRAMEWORK C — ASSET PIVOT (best when website_text is thin or no reviews data):
  Name something specific from their positioning, then pivot to the implication.
  Pattern : "[What they offer or claim] — [the exterior maintenance consequence]."
  Example : "A portfolio marketed on curb appeal is only as strong as the last time
             the building exterior was actually cleaned."

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 4 — BODY STRUCTURE (do NOT use the same pattern twice)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Vary the flow. Do not default to Observation → Problem → Solution → Pitch → CTA.
Choose whichever feels most natural for this specific prospect:

  Flow 1 : Opening → pain consequence → MB Softwash offer → CTA
  Flow 2 : Opening → direct offer statement → proof of low friction → CTA
  Flow 3 : Opening (Framework B) → empathy bridge → MB Softwash as the fix → CTA

Weave the Google Reviews signal into the pain or the pitch — not as a standalone
paragraph. If the signal is BELOW AVERAGE, the urgency belongs in the first half of
the email. If STRONG, mention it briefly as something worth protecting, not celebrating.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 5 — CALL TO ACTION (one question, never two)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
End with exactly ONE question. Nothing after it except the signature.

  ✓ CORRECT : "Would a free exterior walkthrough of one of your Doral
               properties make sense this week?"
  ✗ WRONG   : "Would a call make sense? Does Thursday work for you?"
  ✗ WRONG   : "Can we set something up? What does your schedule look like?"

The question must make 'yes' feel easy and 'no' feel like extra effort to type.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 6 — LENGTH AND TONE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 90 to 120 words in the body, not counting the signature block
- Direct, confident, unhurried — write like someone who does not need this deal
- Never sound eager. Never sound like you are selling. Sound like you are offering.
- Read the final draft aloud. If it sounds like a robot, rewrite it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 7 — SIGNATURE (mandatory, exact, zero variations allowed)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The body MUST close with this exact block — no additions, no reordering:

Best,
Mateo Morantes
MB Softwash Miami
+1 (305) 834-2218
"""

FOLLOWUP_SYSTEM_PROMPT = """\
You are an expert B2B sales copywriter for 'MB Softwash Miami', an exterior maintenance company serving property management companies in Miami.

You are writing a brief follow-up to a cold email sent 4 days ago. The original email offered a FREE Property Exterior Inspection & Maintenance Assessment.

Company being contacted: {company_name}
Decision-maker: {recipient_name}

Write an ultra-short follow-up (2-3 sentences MAX). Be polite, not pushy — a single gentle nudge to check if they saw the previous message.
- The body MUST begin with exactly this greeting line, on its own, then a blank line: {recipient_greeting}
  Do not invent a name or add a title — use it verbatim.
- Do not re-pitch the full offer.
- End with a simple, low-friction question (e.g., "Would this week work for a quick chat?").
- The email MUST end with this exact signature block — no variations, no omissions:
  Best,
  Mateo Morantes
  MB Softwash Miami
  +1 (305) 834-2218
"""

_JSON_INSTRUCTION = (
    'Return ONLY a valid JSON object with exactly two keys: "subject" and "body". '
    "No markdown, no extra text, no explanation."
)


# ── Recipient greeting ────────────────────────────────────────────────────────

def _greeting(full_name: str | None) -> tuple[str, str]:
    """
    Build the salutation from the Apollo decision-maker name.

    Returns (greeting_line, recipient_label):
      - With a name : ("Hi John,", "John")          — first name only, sane casing
      - Without     : ("Hi there,", "the property manager (name unknown)")

    The fallback is deliberately warm and human — "Hi there," reads like a real
    person, never like a broken mail-merge ("Hi ,").
    """
    if full_name and full_name.strip():
        first = full_name.strip().split()[0]
        if first.isupper() or first.islower():   # JOHN / john → John, leave McKay alone
            first = first.capitalize()
        return f"Hi {first},", first
    return "Hi there,", "the property manager (name unknown)"


# ── Rating signal ─────────────────────────────────────────────────────────────

def _build_rating_signal(rating: float | None, review_count: int | None) -> str:
    """
    Translate raw Google review numbers into a plain-English instruction
    that the system prompt can act on without needing Claude to do the maths.

    Thresholds (property-management specific):
      < 4.0  → genuinely poor — lead with tenant-satisfaction pain
      4.0–4.2 → borderline — subtle maintenance angle
      > 4.2  → strong — reinforce their reputation
    """
    if rating is None or review_count is None:
        return "(no Google review data available — use a general curb-appeal angle)"

    stars = f"{rating:.1f}/5 ({review_count} reviews)"

    if rating < 4.0:
        return (
            f"{stars} — BELOW AVERAGE rating. "
            "This prospect likely has active tenant complaints about property upkeep. "
            "Lead with how MB Softwash prevents the maintenance failures that drive "
            "negative reviews (mold, algae, dirty exteriors)."
        )
    if rating < 4.2:
        return (
            f"{stars} — BORDERLINE rating. "
            "Subtly reference that proactive exterior maintenance protects the "
            "reputation they've built and prevents the slide into negative territory."
        )
    return (
        f"{stars} — STRONG rating. "
        "They care about their reputation. Position MB Softwash as the partner "
        "that keeps their properties looking as good as their reviews."
    )


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_initial_candidates(limit: int = WRITER_DAILY_LIMIT):
    """
    Enriched leads that don't have a step-1 draft yet.
    Returns (domain, email, company_name, website_text, rating, review_count, full_name).

    Excludes:
      - Addresses already in suppression_list (unsubscribed / bounced)
    Capped at `limit` rows so writer output stays in sync with sender capacity.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT el.domain,
               el.email,
               COALESCE(l.name, el.domain)        AS company_name,
               COALESCE(l.website_text, '')        AS website_text,
               l.rating                            AS rating,
               l.user_ratings_total                AS review_count,
               el.full_name                        AS full_name
        FROM enriched_leads el
        LEFT JOIN leads l ON l.domain = el.domain
        WHERE NOT EXISTS (
            SELECT 1 FROM generated_emails ge
            WHERE ge.email = el.email AND ge.sequence_step = 1
        )
          AND NOT EXISTS (
            SELECT 1 FROM suppression_list sl
            WHERE sl.email = el.email
        )
        LIMIT ?
    """, (limit,))
    leads = cursor.fetchall()
    conn.close()
    return leads


def get_followup_candidates():
    """
    Leads whose step-1 email was sent 4+ days ago, haven't replied,
    and don't have a step-2 draft yet.
    The replied = 0 guard is the critical block: if they replied to step-1,
    we never generate (or send) a follow-up.
    Returns (domain, email, company_name, full_name).

    Also excludes:
      - Suppressed addresses — catch-all for late unsubscribes after step-1.
      - Snoozed leads (OOO / auto-reply) — don't draft a follow-up while the
        sequence is paused; it would just sit until the snooze expires.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT ge.domain,
               ge.email,
               COALESCE(l.name, ge.domain) AS company_name,
               el.full_name                AS full_name
        FROM generated_emails ge
        LEFT JOIN leads l ON l.domain = ge.domain
        LEFT JOIN enriched_leads el ON el.email = ge.email
        WHERE ge.sequence_step = 1
          AND ge.send_status   = 'sent'
          AND ge.replied       = 0
          AND ge.sent_at      <= datetime('now', '-4 days')
          AND (ge.snooze_until IS NULL OR ge.snooze_until <= CURRENT_TIMESTAMP)
          AND NOT EXISTS (
              SELECT 1 FROM generated_emails ge2
              WHERE ge2.email = ge.email AND ge2.sequence_step = 2
          )
          AND NOT EXISTS (
              SELECT 1 FROM suppression_list sl
              WHERE sl.email = ge.email
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

    logger.info(
        "Module 3 — drafting up to {} initial emails (WRITER_DAILY_LIMIT={})",
        len(leads), WRITER_DAILY_LIMIT,
    )

    for domain, email, company_name, website_text, rating, review_count, full_name in leads:
        greeting_line, recipient_label = _greeting(full_name)
        system = SYSTEM_PROMPT.format(
            company_name=company_name,
            recipient_name=recipient_label,
            recipient_greeting=greeting_line,
            website_text=website_text or f"(no website text captured — domain: {domain})",
            rating_signal=_build_rating_signal(rating, review_count),
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

    for domain, email, company_name, full_name in candidates:
        greeting_line, recipient_label = _greeting(full_name)
        system = FOLLOWUP_SYSTEM_PROMPT.format(
            company_name=company_name,
            recipient_name=recipient_label,
            recipient_greeting=greeting_line,
        )

        result = _draft_email(client, system, _JSON_INSTRUCTION, domain, step=2)
        if result:
            save_generated_email(domain, email, result["subject"], result["body"], sequence_step=2)
        time.sleep(1)


if __name__ == "__main__":
    init_db()
    run_writer()
    run_followup_writer()
