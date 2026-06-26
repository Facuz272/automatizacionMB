"""
Apollo.io decision-maker resolver.

Given a company domain, finds the highest-seniority decision-maker (Owner,
Founder, C-suite, VP, Director, Manager — in that order) and returns their
verified work email. This is the strategic fix for the "info@ black hole":
instead of scraping whatever generic address sits in a website footer, we go
straight to the person who can approve a $400 service.

Flow:
  1. People Search (mixed_people/search) — filter by domain + seniority,
     get back ranked candidates.
  2. People Enrichment (people/match) — Apollo masks emails in search results
     ("email_not_unlocked@domain.com"); match() spends one credit to reveal
     the real address.

Returns None on any failure (no key, network error, no decision-maker found,
masked email that won't unlock) so the enricher can fall back to scraping.
"""

import os
import time

import requests
from loguru import logger

APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/search"
APOLLO_MATCH_URL  = "https://api.apollo.io/api/v1/people/match"


class ApolloTransientError(Exception):
    """
    Raised when Apollo is temporarily unreachable (timeout, connection drop,
    HTTP 429 rate-limit, or 5xx) and retries were exhausted.

    The enricher catches this and SKIPS stamping scraped_at, so the domain is
    retried on the next cycle instead of being permanently downgraded to a
    scraped/blank email because of a passing network blip.

    A *permanent* failure (404 / other 4xx, or simply "no decision-maker
    listed") is NOT this — those return None so the domain is marked scraped.
    """

# Seniority levels Apollo recognises, ordered best → worst. We request all of
# them and then pick the most senior person available at the company.
SENIORITY_RANK: tuple[str, ...] = (
    "owner", "founder", "c_suite", "partner",
    "vp", "head", "director", "manager",
)
_RANK_INDEX = {level: i for i, level in enumerate(SENIORITY_RANK)}

# Apollo returns this sentinel when an email exists but the plan hasn't
# unlocked it. Never send to it.
_MASKED_EMAIL_MARKER = "email_not_unlocked"

REQUEST_TIMEOUT_S = 15
MAX_RETRIES       = 3     # attempts per call before giving up on a transient error
BACKOFF_BASE_S    = 2     # exponential backoff: 2s, 4s, 8s


def _api_key() -> str | None:
    return os.getenv("APOLLO_API_KEY")


def _headers(key: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": key,
    }


def _retry_after_seconds(resp: requests.Response) -> int | None:
    """Honour a numeric Retry-After header on 429 responses, if present."""
    raw = resp.headers.get("Retry-After", "")
    return int(raw) if raw.isdigit() else None


def _sleep_backoff(attempt: int, retry_after: int | None = None) -> None:
    delay = retry_after if retry_after is not None else BACKOFF_BASE_S * (2 ** (attempt - 1))
    time.sleep(delay)


def _post(url: str, payload: dict, key: str) -> dict | None:
    """
    POST to Apollo with retry/backoff.

    Returns parsed JSON on success.
    Returns None on a PERMANENT client error (404 / other non-429 4xx) — the
    caller treats this as "no data".
    Raises ApolloTransientError when timeouts / connection errors / 429 / 5xx
    persist past MAX_RETRIES.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url, json=payload, headers=_headers(key), timeout=REQUEST_TIMEOUT_S
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            logger.debug("Apollo network error (attempt {}/{}): {}", attempt, MAX_RETRIES, exc)
            _sleep_backoff(attempt)
            continue

        # Transient HTTP: rate-limit or server-side. Back off and retry.
        if resp.status_code == 429 or resp.status_code >= 500:
            logger.debug(
                "Apollo transient HTTP {} (attempt {}/{})",
                resp.status_code, attempt, MAX_RETRIES,
            )
            _sleep_backoff(attempt, _retry_after_seconds(resp))
            continue

        # Permanent client error (404, 422, …): not retryable, not transient.
        if resp.status_code >= 400:
            logger.warning("Apollo permanent HTTP {} — treating as no data", resp.status_code)
            return None

        return resp.json()

    raise ApolloTransientError(f"Apollo unreachable after {MAX_RETRIES} attempts: {url}")


def _seniority_sort_key(person: dict) -> int:
    """Lower is more senior. Unknown seniority sorts last."""
    return _RANK_INDEX.get((person.get("seniority") or "").lower(), len(SENIORITY_RANK))


def _search_candidates(domain: str, key: str) -> list[dict]:
    """
    Return decision-maker candidates at `domain`, most senior first.
    May raise ApolloTransientError (propagated from _post).
    """
    payload = {
        "q_organization_domains_list": [domain],
        "person_seniorities": list(SENIORITY_RANK),
        "page": 1,
        "per_page": 10,
    }
    data = _post(APOLLO_SEARCH_URL, payload, key)
    if not data:
        return []
    people = data.get("people", []) or []
    return sorted(people, key=_seniority_sort_key)


def _reveal_email(person_id: str, key: str) -> str | None:
    """
    Spend one enrichment credit to unlock a candidate's real email.
    May raise ApolloTransientError (propagated from _post).
    """
    data = _post(APOLLO_MATCH_URL, {"id": person_id, "reveal_personal_emails": False}, key)
    if not data:
        return None
    email = (data.get("person") or {}).get("email")
    if not email or _MASKED_EMAIL_MARKER in email.lower():
        return None
    return email


def find_decision_maker(domain: str) -> tuple[str, str, str] | None:
    """
    Resolve the best decision-maker at `domain`.

    Returns (email, full_name, title) or None.
    """
    key = _api_key()
    if not key:
        return None

    # NOTE: ApolloTransientError is intentionally NOT caught here — it must
    # propagate to the enricher so the domain is retried next cycle instead of
    # being stamped scraped_at on a passing network/rate-limit blip.
    candidates = _search_candidates(domain, key)

    if not candidates:
        logger.debug("Apollo: no decision-maker listed for {}", domain)
        return None

    for person in candidates:
        full_name = person.get("name") or " ".join(
            filter(None, [person.get("first_name"), person.get("last_name")])
        )
        title = person.get("title") or ""

        # Use the email already in the search result if it's real.
        email = person.get("email") or ""
        if email and _MASKED_EMAIL_MARKER not in email.lower():
            return email, full_name, title

        person_id = person.get("id")
        if not person_id:
            continue
        revealed = _reveal_email(person_id, key)
        if revealed:
            return revealed, full_name, title

    logger.debug("Apollo: candidates found for {} but none had an unlockable email", domain)
    return None
