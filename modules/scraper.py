import sqlite3
import time
from urllib.parse import urlparse

import googlemaps
from loguru import logger

from config import GOOGLE_PLACES_API_KEY, TARGET_CITIES, TARGET_VERTICALS, DB_PATH
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


def search_place_ids(client):
    place_id_map = {}
    total_found = 0

    for city in TARGET_CITIES:
        for vertical in TARGET_VERTICALS:
            query = f"{vertical} {city}"
            logger.info("Searching for {}", query)

            page_token = None
            page_index = 0

            while True:
                page_index += 1
                search_args = {"query": query}
                if page_token:
                    search_args["page_token"] = page_token

                response = client.places(**search_args)
                results = response.get("results", [])
                logger.info("Found {} results on page {} for query {}", len(results), page_index, query)

                for place in results:
                    place_id = place.get("place_id")
                    if place_id and place_id not in place_id_map:
                        place_id_map[place_id] = city
                        total_found += 1

                page_token = response.get("next_page_token")
                if not page_token:
                    break

                logger.info("Waiting 2.5 seconds before fetching next page token")
                time.sleep(2.5)

    logger.info("Found {} unique raw places", total_found)
    return place_id_map


def fetch_place_details(client, place_id, max_retries=3):
    logger.debug("Extracting details for place {}", place_id)
    for attempt in range(1, max_retries + 1):
        try:
            response = client.place(
                place_id=place_id,
                fields=["name", "formatted_address", "website", "permanently_closed"],
            )
            return response.get("result", {})
        except Exception as exc:
            wait = 2 ** attempt
            logger.warning("Attempt {}/{} failed for {}: {} — retrying in {}s", attempt, max_retries, place_id, exc, wait)
            if attempt == max_retries:
                raise
            time.sleep(wait)


def build_lead_record(place_id, details, city):
    if details.get("permanently_closed"):
        logger.debug("Skipping permanently closed business {}", place_id)
        return None

    website = details.get("website")
    if not website:
        logger.debug("Skipping place {} because no website is available", place_id)
        return None

    domain = extract_domain(website)
    if not domain:
        logger.debug("Skipping place {} because domain extraction failed", place_id)
        return None

    return {
        "place_id": place_id,
        "name": details.get("name", "")[:255],
        "address": details.get("formatted_address", "")[:512],
        "city": city,
        "website": website,
        "domain": domain,
    }


def save_leads(leads):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        before_changes = conn.total_changes
        for lead in leads:
            cursor.execute(
                "INSERT OR IGNORE INTO leads (place_id, name, address, city, website, domain) VALUES (?, ?, ?, ?, ?, ?)",
                (lead["place_id"], lead["name"], lead["address"], lead["city"], lead["website"], lead["domain"]),
            )
        conn.commit()
        inserted = conn.total_changes - before_changes
        logger.info("Saved {} new leads", inserted)
        return inserted
    finally:
        conn.close()


def run_scraper():
    logger.info("Starting scraper module")
    init_db()
    client = build_client()
    existing_ids = get_existing_place_ids()
    place_id_map = search_place_ids(client)

    new_place_ids = {pid: city for pid, city in place_id_map.items() if pid not in existing_ids}
    logger.info("Skipping {} already-saved places, fetching details for {} new ones", len(place_id_map) - len(new_place_ids), len(new_place_ids))

    leads = []
    for place_id, city in new_place_ids.items():
        try:
            details = fetch_place_details(client, place_id)
            lead = build_lead_record(place_id, details, city)
            if lead:
                leads.append(lead)
        except Exception as exc:
            logger.warning("Failed to fetch details for {}: {}", place_id, exc)
        time.sleep(0.05)  # stay under Google's QPS limit

    logger.info("Processed {} detail records, saving valid leads", len(leads))
    save_leads(leads)
    logger.info("Scraper run completed")


if __name__ == "__main__":
    run_scraper()
