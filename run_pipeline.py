"""
Master pipeline scheduler.

Schedule (timezone set via TZ env var in docker-compose):
  Monday    09:00 — full cycle: scrape + enrich + write + follow-ups + send
  Tue–Fri   09:00 — daily cycle: enrich + write + follow-ups + send

The scraper only runs on Mondays to discover fresh leads for the week.
All other days focus on enriching, drafting and sending.
"""

import time

import schedule
from loguru import logger

from modules.enricher import run_enricher
from modules.scraper import run_scraper
from modules.sender import run_sender
from modules.writer import run_followup_writer, run_writer
from utils.db import init_db


def full_pipeline():
    """Monday: scrape fresh leads + full send cycle."""
    logger.info("=== FULL PIPELINE START (Mon) ===")
    try:
        run_scraper()
        run_enricher()
        run_writer()
        run_followup_writer()
        run_sender()
    except Exception as e:
        logger.error("Pipeline crashed: {}", e)
    logger.info("=== FULL PIPELINE COMPLETE ===")


def daily_cycle():
    """Tue–Fri: enrich + write + follow-ups + send (no scrape)."""
    logger.info("=== DAILY CYCLE START ===")
    try:
        run_enricher()
        run_writer()
        run_followup_writer()
        run_sender()
    except Exception as e:
        logger.error("Daily cycle crashed: {}", e)
    logger.info("=== DAILY CYCLE COMPLETE ===")


def main():
    init_db()

    # Times are relative to the container's TZ (set to America/New_York in docker-compose)
    schedule.every().monday.at("09:00").do(full_pipeline)
    schedule.every().tuesday.at("09:00").do(daily_cycle)
    schedule.every().wednesday.at("09:00").do(daily_cycle)
    schedule.every().thursday.at("09:00").do(daily_cycle)
    schedule.every().friday.at("09:00").do(daily_cycle)

    logger.info("Scheduler ready — pipeline runs Mon–Fri at 09:00 ({})", __import__("os").getenv("TZ", "system TZ"))
    logger.info("Next run: {}", schedule.next_run())

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
