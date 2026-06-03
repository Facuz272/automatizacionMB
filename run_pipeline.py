"""
Master pipeline scheduler.

Execution order (important):
  1. tracker       — mark replied=1 FIRST so writer/sender see fresh state
  2. scraper       — Monday only, discover new leads
  3. enricher      — find emails for unscraped domains
  4. writer        — draft step-1 for new leads
  5. followup_writer — draft step-2 only for non-replied leads >= 4 days
  6. sender        — send all pending, skipping replied leads

Schedule (TZ set via docker-compose environment):
  Monday   09:00 — full cycle (includes scraper)
  Tue–Fri  09:00 — daily cycle (no scraper)
"""

import os
import time

import schedule
from dotenv import load_dotenv
from loguru import logger

from modules.enricher import run_enricher
from modules.scraper import run_scraper
from modules.sender import run_sender
from modules.tracker import run_tracker
from modules.writer import run_followup_writer, run_writer
from utils.db import init_db

load_dotenv()


def full_pipeline():
    """Monday: check replies → scrape fresh leads → full cycle."""
    logger.info("=== FULL PIPELINE START (Mon) ===")
    try:
        run_tracker()
        run_scraper()
        run_enricher()
        run_writer()
        run_followup_writer()
        run_sender()
    except Exception as e:
        logger.error("Pipeline crashed: {}", e)
    logger.info("=== FULL PIPELINE COMPLETE ===")


def daily_cycle():
    """Tue–Fri: check replies → enrich → write → send."""
    logger.info("=== DAILY CYCLE START ===")
    try:
        run_tracker()
        run_enricher()
        run_writer()
        run_followup_writer()
        run_sender()
    except Exception as e:
        logger.error("Daily cycle crashed: {}", e)
    logger.info("=== DAILY CYCLE COMPLETE ===")


def main():
    # Single init — modules no longer call init_db() themselves
    init_db()

    # Times interpreted in container TZ (America/New_York via docker-compose)
    schedule.every().monday.at("09:00").do(full_pipeline)
    schedule.every().tuesday.at("09:00").do(daily_cycle)
    schedule.every().wednesday.at("09:00").do(daily_cycle)
    schedule.every().thursday.at("09:00").do(daily_cycle)
    schedule.every().friday.at("09:00").do(daily_cycle)

    logger.info(
        "Scheduler ready — pipeline runs Mon–Fri at 09:00 (TZ: {})",
        os.getenv("TZ", "system default"),
    )
    logger.info("Next scheduled run: {}", schedule.next_run())

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
