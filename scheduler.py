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

    prefs          = config.get("preferences", {})
    preferred_start = prefs.get("preferred_start", "07:00")
    preferred_end   = prefs.get("preferred_end",   "10:00")
    num_players     = int(prefs.get("num_players", 3))

    log.info("Target date: %s | window: %s–%s | players: %s",
             target_date, preferred_start, preferred_end, num_players)

    # ── Check release date ────────────────────────────────────────────────────
    # Tee times open exactly 7 days before the target date at 20:00 UK time.
    # If today is not that day, exit — the cron will run again next Saturday.
    today = datetime.now(UK_TZ).date()
    target_dt    = datetime.strptime(target_date, "%Y-%m-%d").date()
    days_until   = (target_dt - today).days

    if not test_mode and not dry_run_mode:
        if days_until > 7:
            log.info(
                "Target date is %d days away — release day is not today. "
                "Exiting. Will try again next Saturday.", days_until
            )
            return
        elif days_until < 7:
            log.warning(
                "Target date is only %d days away — release window may have passed. "
                "Attempting anyway.", days_until
            )
        else:
            log.info("Release day confirmed — target is exactly 7 days away. Running.")

    # ── Randomise target time within window ───────────────────────────────────
    # Everyone rushes for 07:00. Picking a random slot in the window gives a
    # better chance of success — less competition for 08:30 than 07:00.
    import random as _random
    from datetime import datetime as _dt, timedelta as _td
    start_m = int(preferred_start[:2]) * 60 + int(preferred_start[3:])
    end_m   = int(preferred_end[:2])   * 60 + int(preferred_end[3:])
    # Round to nearest 10-minute tee time interval
    intervals = list(range(start_m, end_m + 1, 10))
    target_m  = _random.choice(intervals)
    target_time = f"{target_m // 60:02d}:{target_m % 60:02d}"
    log.info(
        "Randomised target time: %s (chosen from %s–%s window). "
        "Will book nearest available slot to this time.",
        target_time, preferred_start, preferred_end
    )

    # book_tee_time handles all waiting internally — just call it
    try:
        success = book_tee_time(
            target_date=target_date,
            preferred_start=target_time,
            preferred_end=preferred_end,
            num_players=num_players,
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
