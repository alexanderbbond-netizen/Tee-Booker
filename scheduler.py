"""
scheduler.py
============
Runs every Saturday at ~19:55 (via cron). Checks booking_request.json:
  - If enabled = true:  waits until ~20:00, books the tee time, then resets the file.
  - If enabled = false: exits immediately without doing anything.

Cron entry (PythonAnywhere / VPS):
  55 19 * * 6  cd /home/user/tee_booker && python scheduler.py
"""

import json
import time
import random
import logging
from datetime import datetime
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

RELEASE_HOUR   = 20
RELEASE_MINUTE = 0
RELEASE_SECOND = 0
JITTER_MIN     = 2    # seconds after 20:00:00 to fire (min)
JITTER_MAX     = 8    # seconds after 20:00:00 to fire (max)


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def reset_config():
    """Disable the booking request after a successful booking."""
    config = load_config()
    config["enabled"] = False
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)
    log.info("booking_request.json reset — bot will not run next week unless re-enabled.")


# ── Timing helpers ─────────────────────────────────────────────────────────────

def seconds_until_release() -> float:
    now    = datetime.now(UK_TZ)
    target = now.replace(
        hour=RELEASE_HOUR,
        minute=RELEASE_MINUTE,
        second=RELEASE_SECOND,
        microsecond=0,
    )
    delta = (target - now).total_seconds()
    return delta if delta > 0 else 0


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # ── 1. Check whether a booking has been requested ─────────────────────────
    try:
        config = load_config()
    except Exception as exc:
        log.error("Could not read booking_request.json: %s", exc)
        return

    if not config.get("enabled"):
        log.info("No booking requested this week (enabled=false). Exiting.")
        return

    target_date = config.get("target_date", "").strip()
    if not target_date:
        log.error("enabled=true but no target_date set. Please run: python request_booking.py on --date YYYY-MM-DD")
        return

    prefs = config.get("preferences", {})
    log.info("Booking requested for %s  |  window: %s–%s  |  players: %s",
             target_date,
             prefs.get("preferred_start", "07:00"),
             prefs.get("preferred_end",   "10:00"),
             prefs.get("num_players",     3))

    # ── 2. Wait until 20:00:00 ────────────────────────────────────────────────
    wait = seconds_until_release()
    if wait > 0:
        log.info("Waiting %.1f seconds until 20:00:00 UK time…", wait)
        while wait > 10:
            time.sleep(10)
            wait = seconds_until_release()
            log.info("  %.0f seconds remaining…", wait)
        time.sleep(wait)

    # ── 3. Randomised jitter (so we never fire at exactly 20:00:00) ───────────
    jitter = random.uniform(JITTER_MIN, JITTER_MAX)
    log.info("20:00 reached. Jitter: %.1fs", jitter)
    time.sleep(jitter)

    log.info("🏌️  Firing booking bot now!")

    # ── 4. Attempt booking (with retries) ─────────────────────────────────────
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            log.info("Attempt %d/%d…", attempt, max_attempts)
            success = book_tee_time(
                target_date=target_date,
                preferred_start=prefs.get("preferred_start", "07:00"),
                preferred_end=prefs.get("preferred_end",     "10:00"),
                num_players=int(prefs.get("num_players",     3)),
            )
            if success:
                log.info("✅ Booking successful.")
                reset_config()   # auto-disable so it won't fire next week
                return
        except Exception as exc:
            log.warning("Attempt %d failed: %s", attempt, exc)
            if attempt < max_attempts:
                retry_wait = random.uniform(4, 8)
                log.info("Retrying in %.1fs…", retry_wait)
                time.sleep(retry_wait)
            else:
                log.error("All %d attempts exhausted. Please book manually.", max_attempts)


if __name__ == "__main__":
    main()
