import json
import os
import re
import time

from anthropic import Anthropic, APIError, RateLimitError
from loguru import logger

from utils.db import get_connection, init_db


def get_enriched_leads():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT el.domain, el.email
        FROM enriched_leads el
        LEFT JOIN generated_emails ge ON el.email = ge.email
        WHERE ge.email IS NULL
    """)
    leads = cursor.fetchall()
    conn.close()
    return leads


def save_generated_email(domain, email, subject, body):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO generated_emails (domain, email, subject, body) VALUES (?, ?, ?, ?)",
            (domain, email, subject, body),
        )
        conn.commit()
    except Exception as e:
        logger.error("Error saving generated email for {}: {}", domain, e)
    finally:
        conn.close()


def _extract_json(text: str) -> dict:
    """Parse JSON from model output, tolerating markdown wrappers and extra text."""
    # 1. Direct parse (ideal case — model obeyed instructions)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Extract from ```json ... ``` or ``` ... ``` block
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return json.loads(match.group(1))

    # 3. Find first {...} object anywhere in the text
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"No JSON object found in model output: {text!r}")


def run_writer():
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("Missing ANTHROPIC_API_KEY in .env")
        return

    init_db()
    client = Anthropic(api_key=api_key)
    leads = get_enriched_leads()

    if not leads:
        logger.warning("No new leads to process — all emails already drafted.")
        return

    logger.info("Starting Module 3: drafting emails for {} prospects", len(leads))

    for domain, email in leads:
        logger.info("Drafting email for: {} ({})", domain, email)

        prompt = f"""
        Act as an expert B2B copywriter. Write a short, highly converting cold email (in English) targeting a Property Management company at the domain: {domain}.

        Our Pitch: We offer custom AI and automation software (like lead scraping systems and CRM workflows) to help them save hours of manual work and acquire more property owners.
        Goal: Get them to reply to schedule a 10-minute discovery call.
        Tone: Professional, direct, not overly salesy.

        You MUST return ONLY a valid JSON object with the keys "subject" and "body". Do not include any other text or markdown outside the JSON.
        """

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

                save_generated_email(domain, email, email_data["subject"], email_data["body"])
                logger.success("Draft saved for {}", domain)
                time.sleep(1)
                break

            except RateLimitError:
                wait = 30 * attempt
                logger.warning("Rate limit hit — waiting {}s before retry {}/3", wait, attempt)
                time.sleep(wait)

            except (APIError, ValueError, json.JSONDecodeError) as e:
                logger.warning("Attempt {}/3 failed for {}: {}", attempt, domain, e)
                if attempt == 3:
                    logger.error("All retries exhausted for {}", domain)
                time.sleep(2 * attempt)


if __name__ == "__main__":
    run_writer()
