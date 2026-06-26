"""
Master pipeline scheduler.

Execution order (important):
  1. tracker         — mark replied=1 FIRST so writer/sender see fresh state
  2. scraper         — Monday only, discover new leads
  3. enricher        — find emails for unscraped domains
  4. writer          — draft step-1 for new leads
  5. followup_writer — draft step-2 only for non-replied leads >= 4 days
  6. sender          — send all pending/retryable, skipping replied leads

Schedule (TZ must be provided by the environment — e.g. systemd
Environment="TZ=America/New_York"; there is no longer a docker-compose to set it):
  Monday   09:00 — full cycle (includes scraper)
  Tue–Fri  09:00 — daily cycle (no scraper)
"""

import os
import time
from typing import Callable

import requests
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


# ── Observability ─────────────────────────────────────────────────────────────

LOG_DIR = os.getenv("LOG_DIR", "logs")

# Dead-man's-switch: a Healthchecks.io (or compatible) ping URL. The pipeline
# pings <url>/start at the top of a run and <url> on success / <url>/fail on a
# failed run. If pings stop arriving, Healthchecks alerts you — so a silent
# crash can't go unnoticed for days the way the first production month did.
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_PING_URL")


def _configure_logging() -> None:
    """Persist logs to a rotating, compressed file in addition to stderr."""
    logger.add(
        os.path.join(LOG_DIR, "pipeline.log"),
        rotation="10 MB",        # roll over once the file hits 10 MB
        retention="30 days",     # keep a month of history, then delete
        compression="zip",       # gzip old logs to save disk
        level="INFO",
        enqueue=True,            # process-safe writes
        backtrace=True,
        diagnose=False,          # never expand variables — avoids leaking secrets
    )


def _ping_healthcheck(suffix: str = "") -> None:
    """
    Ping the dead-man's-switch. Never raises — a monitoring failure must never
    take down the pipeline. suffix: '/start', '' (success), or '/fail'.
    """
    if not HEALTHCHECK_URL:
        return
    try:
        requests.get(HEALTHCHECK_URL + suffix, timeout=10)
    except requests.RequestException as exc:
        logger.warning("Healthcheck ping '{}' failed: {}", suffix or "success", exc)


# ── Step runner ───────────────────────────────────────────────────────────────

def _run_step(name: str, func: Callable) -> bool:
    """
    Execute one pipeline step in isolation.
    A crash in step N does NOT abort steps N+1 … N+k.
    Returns True on success, False if the step raised an exception.
    """
    logger.info("── step: {} ──", name)
    try:
        func()
        return True
    except Exception as exc:
        logger.error("Step '{}' crashed and was skipped: {}", name, exc)
        return False


# ── Pipelines ─────────────────────────────────────────────────────────────────

def full_pipeline():
    """Monday: check replies → scrape fresh leads → full send cycle."""
    logger.info("=== FULL PIPELINE START (Mon) ===")
    _ping_healthcheck("/start")

    steps = [
        ("tracker",         run_tracker),
        ("scraper",         run_scraper),
        ("enricher",        run_enricher),
        ("writer",          run_writer),
        ("followup_writer", run_followup_writer),
        ("sender",          run_sender),
    ]

    results = {name: _run_step(name, func) for name, func in steps}

    failed_steps = [name for name, ok in results.items() if not ok]
    if failed_steps:
        logger.warning("Full pipeline finished with failed steps: {}", failed_steps)
        _ping_healthcheck("/fail")
    else:
        logger.success("Full pipeline finished — all 6 steps OK")
        _ping_healthcheck()

    logger.info("=== FULL PIPELINE COMPLETE ===")


def daily_cycle():
    """Tue–Fri: check replies → enrich → write → send."""
    logger.info("=== DAILY CYCLE START ===")
    _ping_healthcheck("/start")

    steps = [
        ("tracker",         run_tracker),
        ("enricher",        run_enricher),
        ("writer",          run_writer),
        ("followup_writer", run_followup_writer),
        ("sender",          run_sender),
    ]

    results = {name: _run_step(name, func) for name, func in steps}

    failed_steps = [name for name, ok in results.items() if not ok]
    if failed_steps:
        logger.warning("Daily cycle finished with failed steps: {}", failed_steps)
        _ping_healthcheck("/fail")
    else:
        logger.success("Daily cycle finished — all 5 steps OK")
        _ping_healthcheck()

    logger.info("=== DAILY CYCLE COMPLETE ===")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def main():
    _configure_logging()  # rotating file sink before anything logs
    init_db()  # single init — modules no longer call init_db() themselves

    # Times interpreted in the process TZ — set via systemd Environment="TZ=..."
    schedule.every().monday.at("09:00").do(full_pipeline)
    schedule.every().tuesday.at("09:00").do(daily_cycle)
    schedule.every().wednesday.at("09:00").do(daily_cycle)
    schedule.every().thursday.at("09:00").do(daily_cycle)
    schedule.every().friday.at("09:00").do(daily_cycle)

    logger.info("Scheduler ready — Mon–Fri 09:00 (TZ: {})",
                os.getenv("TZ", "system default"))
    logger.info("Next run: {}", schedule.next_run())

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
