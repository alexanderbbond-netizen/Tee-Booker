"""
scheduler.py
============
Reads booking_request.json, then calls book_tee_time() which:
  - Logs in and pre-positions on the booking page BEFORE 8pm
  - Waits internally until 8pm, then refreshes and grabs a slot
  - Adds guests and completes the booking

The scheduler should be triggered at ~19:30 (not 19:55) to give
plenty of time for login and pre-positioning before 8pm.

Cron / GitHub Actions schedule:
  '30 18 * * 6'  →  19:30 BST (UTC+1 in summer) every Saturday
  '30 19 * * 6'  →  19:30 GMT (UTC+0 in winter) every Saturday
"""

import json
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

from book_tee import book_tee_time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

UK_TZ       = ZoneInfo("Europe/London")
CONFIG_PATH = Path(__file__).parent / "booking_request.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)

def reset_config():
    config = load_config()
    config["enabled"] = False
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)
    log.info("booking_request.json reset — disabled until next request.")


def main():
    test_mode    = "--test"    in sys.argv
    dry_run_mode = "--dry-run" in sys.argv

    if test_mode:
        log.info("TEST MODE — running immediately.")
    if dry_run_mode:
        log.info("DRY RUN MODE — will stop before confirming.")

    # Load config
    try:
        config = load_config()
    except Exception as exc:
        log.error("Could not read booking_request.json: %s", exc)
        return

    if not config.get("enabled") and not test_mode and not dry_run_mode:
        log.info("No booking requested (enabled=false). Exiting.")
        return

    target_date = config.get("target_date", "").strip()
    if not target_date:
        log.error("No target_date set. Run: python request_booking.py on --date YYYY-MM-DD")
        return

    prefs = config.get("preferences", {})
    log.info("Target date: %s | window: %s–%s | players: %s",
             target_date,
             prefs.get("preferred_start", "07:00"),
             prefs.get("preferred_end",   "10:00"),
             prefs.get("num_players",     3))

    # book_tee_time handles all waiting internally — just call it
    try:
        success = book_tee_time(
            target_date=target_date,
            preferred_start=prefs.get("preferred_start", "07:00"),
            preferred_end=prefs.get("preferred_end",     "10:00"),
            num_players=int(prefs.get("num_players",     3)),
            dry_run=dry_run_mode,
        )
        if success:
            log.info("✅ Booking complete.")
            if not dry_run_mode:
                reset_config()
    except Exception as exc:
        log.error("Booking failed after all attempts: %s", exc)
        log.error("Please book manually at %s/memberbooking/", 
                  config.get("IG_CLUB_URL", "your club site"))


if __name__ == "__main__":
    main()
