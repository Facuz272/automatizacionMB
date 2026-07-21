import time
from urllib.parse import urlparse

import googlemaps
from loguru import logger

from config import GOOGLE_PLACES_API_KEY, TARGET_CITIES, TARGET_VERTICALS
from utils.db import get_connection, init_db


def validate_config():
    if not GOOGLE_PLACES_API_KEY:
        logger.error("GOOGLE_PLACES_API_KEY is missing in config.py or .env")
        raise ValueError("Missing Google Places API key")


def build_client():
    validate_config()
    return googlemaps.Client(key=GOOGLE_PLACES_API_KEY)


def extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        return hostname.lower().lstrip("www.")
    except Exception as exc:
        logger.warning("Unable to extract domain from URL {}: {}", url, exc)
        return ""


def get_existing_place_ids():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT place_id FROM leads")
    ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    return ids


def _fetch_page(client, search_args: dict, has_page_token: bool) -> dict | None:
    """
    Fetch one page from the Places Text Search API.

    When a next_page_token is present Google can take up to ~5 seconds to
    activate it after issuing it.  Sending a request too early returns
    INVALID_REQUEST — this is documented behaviour, not an error on our end.

    Strategy:
      - page_token requests: retry INVALID_REQUEST up to 3 times with
        increasing delays (3 s → 5 s → 10 s).
      - All other errors: single attempt, return None on failure so the
        caller can break pagination gracefully instead of crashing.
    """
    delays = [3, 5, 10]
    max_attempts = len(delays) if has_page_token else 1

    for attempt in range(1, max_attempts + 1):
        try:
            return client.places(**search_args)
        except Exception as exc:
            is_token_activation = has_page_token and "INVALID_REQUEST" in str(exc)

            if is_token_activation and attempt < max_attempts:
                wait = delays[attempt - 1]
                logger.warning(
                    "next_page_token not ready yet (attempt {}/{}) — retrying in {}s",
                    attempt, max_attempts, wait,
                )
                time.sleep(wait)
            else:
                logger.warning(
                    "Places API request failed after {} attempt(s): {}", attempt, exc
                )
                return None

    return None  # all retries exhausted


def search_place_ids(client):
    """
    Collect place_ids across all configured cities and verticals.
    Pagination failures on page 2+ do NOT discard page 1 results —
    the function breaks the pagination loop and returns what it has.
    """
    place_id_map: dict[str, str] = {}

    for city in TARGET_CITIES:
        for vertical in TARGET_VERTICALS:
            query = f"{vertical} {city}"
            logger.info("Searching for '{}'", query)

            page_token: str | None = None
            page_index = 0

            while True:
                page_index += 1
                search_args: dict = {"query": query}
                if page_token:
                    search_args["page_token"] = page_token

                response = _fetch_page(client, search_args, has_page_token=bool(page_token))

                if response is None:
                    # Page fetch failed after retries.
                    # Break pagination loop — results from previous pages are safe.
                    logger.warning(
                        "Stopping pagination for '{}' at page {} — "
                        "{} place(s) collected so far will be saved.",
                        query, page_index, len(place_id_map),
                    )
                    break

                results = response.get("results", [])
                logger.info("Page {}: {} results for '{}'", page_index, len(results), query)

                for place in results:
                    place_id = place.get("place_id")
                    if place_id and place_id not in place_id_map:
                        place_id_map[place_id] = city

                page_token = response.get("next_page_token")
                if not page_token:
                    break  # no more pages for this query

                logger.info("Waiting 3s for next_page_token to activate...")
                time.sleep(3)

    logger.info("Collected {} unique place(s) across all queries", len(place_id_map))
    return place_id_map


def fetch_place_details(client, place_id: str, max_retries: int = 3) -> dict:
    logger.debug("Fetching details for place {}", place_id)
    for attempt in range(1, max_retries + 1):
        try:
            response = client.place(
                place_id=place_id,
                fields=[
                    "name", "formatted_address", "website",
                    "permanently_closed",
                    "rating",             # Google review score (0.0–5.0)
                    "user_ratings_total", # number of reviews
                    "formatted_phone_number",     # local dialing format, e.g. (305) 834-2218
                    "international_phone_number",  # +country fallback if local missing
                ],
            )
            return response.get("result", {})
        except Exception as exc:
            wait = 2 ** attempt
            logger.warning(
                "Attempt {}/{} failed for {}: {} — retrying in {}s",
                attempt, max_retries, place_id, exc, wait,
            )
            if attempt == max_retries:
                raise
            time.sleep(wait)


def build_lead_record(place_id: str, details: dict, city: str) -> dict | None:
    if details.get("permanently_closed"):
        logger.debug("Skipping permanently closed business {}", place_id)
        return None

    website = details.get("website")
    if not website:
        logger.debug("Skipping {} — no website", place_id)
        return None

    domain = extract_domain(website)
    if not domain:
        logger.debug("Skipping {} — domain extraction failed", place_id)
        return None

    return {
        "place_id":           place_id,
        "name":               details.get("name", "")[:255],
        "address":            details.get("formatted_address", "")[:512],
        "city":               city,
        "website":            website,
        "domain":             domain,
        "rating":             details.get("rating"),            # float or None
        "user_ratings_total": details.get("user_ratings_total"),# int  or None
        "phone":              details.get("formatted_phone_number")
                              or details.get("international_phone_number"),  # str or None
    }


def save_leads(leads: list[dict]) -> int:
    """
    Persist new leads.

    Two-layer deduplication:
      1. place_id PRIMARY KEY  — same API result arriving twice (idempotent re-runs).
      2. WHERE NOT EXISTS domain — same company discovered via a different city query
         or vertical search.  We keep the first record encountered and silently drop
         the duplicate, avoiding phantom duplicates in the enricher / writer queue.
    """
    conn = get_connection()
    try:
        cursor = conn.cursor()
        before = conn.total_changes
        for lead in leads:
            cursor.execute(
                """INSERT INTO leads
                       (place_id, name, address, city, website, domain,
                        rating, user_ratings_total, phone)
                   SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM leads
                       WHERE place_id = ? OR domain = ?
                   )""",
                (
                    lead["place_id"], lead["name"],    lead["address"],
                    lead["city"],     lead["website"], lead["domain"],
                    lead.get("rating"), lead.get("user_ratings_total"),
                    lead.get("phone"),
                    # WHERE NOT EXISTS params
                    lead["place_id"], lead["domain"],
                ),
            )
        conn.commit()
        inserted = conn.total_changes - before
        logger.info("Saved {} new lead(s) to DB", inserted)
        return inserted
    finally:
        conn.close()


def run_scraper():
    logger.info("Scraper starting")
    init_db()
    client        = build_client()
    existing_ids  = get_existing_place_ids()
    place_id_map  = search_place_ids(client)

    new_ids = {pid: city for pid, city in place_id_map.items() if pid not in existing_ids}
    logger.info(
        "{} already in DB, {} new to fetch details for",
        len(place_id_map) - len(new_ids), len(new_ids),
    )

    leads = []
    for place_id, city in new_ids.items():
        try:
            details = fetch_place_details(client, place_id)
            lead    = build_lead_record(place_id, details, city)
            if lead:
                leads.append(lead)
        except Exception as exc:
            logger.warning("Could not fetch details for {}: {}", place_id, exc)
        time.sleep(0.05)  # stay under Google QPS limit

    logger.info("Processed {} detail records", len(leads))
    save_leads(leads)
    logger.info("Scraper complete")


if __name__ == "__main__":
    run_scraper()
