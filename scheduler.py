"""
scheduler.py
============
Reads booking_request.json to find the target_date, then waits until
exactly 7 days before that date at 20:00 UK time before booking.

This means whatever day you want to play, the bot fires at 8pm on the
same day of the week, one week earlier.

Examples:
  target_date = 2026-05-09 (Saturday)  → fires 2026-05-02 at ~20:00
  target_date = 2026-05-13 (Wednesday) → fires 2026-05-06 at ~20:00

Usage:
  python scheduler.py            # normal mode — waits until release time
  python scheduler.py --test     # test mode   — skips timer, runs immediately
  python scheduler.py --dry-run  # dry run     — skips timer, stops before confirming
"""

import sys
import json
import time
import random
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

RELEASE_HOUR   = 20   # 8 PM
RELEASE_MINUTE = 0
JITTER_MIN     = 2    # seconds after 20:00:00 to fire (min)
JITTER_MAX     = 8    # seconds after 20:00:00 to fire (max)


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def reset_config():
    """Disable the booking request after a successful booking."""
    config = load_config()
    config["enabled"] = False
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)
    log.info("booking_request.json reset — bot disabled until next request.")


def release_datetime(target_date_str: str) -> datetime:
    """
    Return the UK datetime when bookings open for target_date:
    exactly 7 days before, at 20:00 UK time.
    """
    target = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    release_date = target - timedelta(days=7)
    release_dt = datetime(
        release_date.year, release_date.month, release_date.day,
        RELEASE_HOUR, RELEASE_MINUTE, 0,
        tzinfo=UK_TZ
    )
    return release_dt


def seconds_until(dt: datetime) -> float:
    now   = datetime.now(UK_TZ)
    delta = (dt - now).total_seconds()
    return delta if delta > 0 else 0


def main():
    test_mode    = "--test"    in sys.argv
    dry_run_mode = "--dry-run" in sys.argv

    if test_mode:
        log.info("🧪 TEST MODE — skipping timer, booking immediately.")
    if dry_run_mode:
        log.info("🔍 DRY RUN MODE — will stop before confirming the booking.")

    # ── 1. Read config ────────────────────────────────────────────────────────
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
    log.info("Target date: %s  |  window: %s–%s  |  players: %s",
             target_date,
             prefs.get("preferred_start", "07:00"),
             prefs.get("preferred_end",   "10:00"),
             prefs.get("num_players",     3))

    # ── 2. Work out release time ──────────────────────────────────────────────
    try:
        release_dt = release_datetime(target_date)
    except ValueError:
        log.error("Invalid target_date format: '%s'. Use YYYY-MM-DD.", target_date)
        return

    log.info("Bookings open: %s", release_dt.strftime("%A %d %b %Y at %H:%M %Z"))

    # ── 3. Wait (unless test/dry-run) ─────────────────────────────────────────
    if not test_mode and not dry_run_mode:
        wait = seconds_until(release_dt)
        if wait > 86400:
            log.info("Release time is %.1f hours away. Sleeping…", wait / 3600)
        if wait > 0:
            while wait > 10:
                time.sleep(10)
                wait = seconds_until(release_dt)
                if wait > 3600:
                    pass  # only log once per hour in long waits
                else:
                    log.info("  %.0f seconds until release…", wait)
            time.sleep(wait)

        jitter = random.uniform(JITTER_MIN, JITTER_MAX)
        log.info("Release time reached. Jitter: %.1fs", jitter)
        time.sleep(jitter)

    log.info("🏌️  Firing booking bot!")

    # ── 4. Attempt booking ────────────────────────────────────────────────────
    max_attempts = 1 if (test_mode or dry_run_mode) else 5
    for attempt in range(1, max_attempts + 1):
        try:
            log.info("Attempt %d/%d…", attempt, max_attempts)
            success = book_tee_time(
                target_date=target_date,
                preferred_start=prefs.get("preferred_start", "07:00"),
                preferred_end=prefs.get("preferred_end",     "10:00"),
                num_players=int(prefs.get("num_players",     3)),
                dry_run=dry_run_mode,
            )
            if success:
                log.info("✅ Done.")
                if not dry_run_mode:
                    reset_config()
                return
        except Exception as exc:
            log.warning("Attempt %d failed: %s", attempt, exc)
            if attempt < max_attempts:
                retry_wait = random.uniform(4, 8)
                log.info("Retrying in %.1fs…", retry_wait)
                time.sleep(retry_wait)
            else:
                log.error("All attempts exhausted. Please book manually.")


if __name__ == "__main__":
    main()
