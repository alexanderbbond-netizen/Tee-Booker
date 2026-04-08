"""
request_booking.py
==================
A simple command-line tool to schedule (or cancel) a tee time booking
request — no need to hand-edit booking_request.json.

Usage examples:

  # Request next Saturday's booking with default preferences
  python request_booking.py on

  # Request a specific date
  python request_booking.py on --date 2026-04-19

  # Override preferences for this booking only
  python request_booking.py on --date 2026-04-19 --start 08:00 --end 09:30 --players 4

  # Cancel a previously set request
  python request_booking.py off

  # Check the current request status
  python request_booking.py status
"""

import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path(__file__).parent / "booking_request.json"
UK_TZ       = ZoneInfo("Europe/London")


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(config: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)
    print(f"✅ booking_request.json updated.")


def next_saturday() -> str:
    today      = datetime.now(UK_TZ).date()
    days_ahead = (5 - today.weekday()) % 7 or 7
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")


def cmd_on(args):
    config = load_config()

    date = args.date or next_saturday()
    # Validate date format
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        print(f"❌ Invalid date format: '{date}'. Please use YYYY-MM-DD.")
        return

    config["enabled"]     = True
    config["target_date"] = date

    if args.start:
        config["preferences"]["preferred_start"] = args.start
    if args.end:
        config["preferences"]["preferred_end"] = args.end
    if args.players:
        config["preferences"]["num_players"] = int(args.players)

    save_config(config)
    print(f"\n📅 Booking requested for:  {date}")
    print(f"   Time window:  {config['preferences']['preferred_start']} – {config['preferences']['preferred_end']}")
    print(f"   Players:      {config['preferences']['num_players']}")
    print(f"\nThe bot will run this Saturday at ~20:00 UK time.")
    print("To cancel before then, run:  python request_booking.py off")


def cmd_off(args):
    config = load_config()
    config["enabled"] = False
    save_config(config)
    print("🚫 Booking request cancelled. The bot will not run this Saturday.")


def cmd_status(args):
    config = load_config()
    if config.get("enabled"):
        print(f"\n✅ Booking IS requested")
        print(f"   Date:         {config.get('target_date') or '(not set)'}")
        prefs = config.get("preferences", {})
        print(f"   Time window:  {prefs.get('preferred_start')} – {prefs.get('preferred_end')}")
        print(f"   Players:      {prefs.get('num_players')}")
        print(f"\nThe bot will run this Saturday at ~20:00 UK time.")
    else:
        print("\n🚫 No booking requested. Bot will NOT run this Saturday.")
        if config.get("target_date"):
            print(f"   (Last target date was: {config['target_date']})")


def main():
    parser = argparse.ArgumentParser(
        description="Manage your weekly tee time booking request.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    sub = parser.add_subparsers(dest="command")

    # --- on ---
    on_parser = sub.add_parser("on", help="Request a booking for this or a specific Saturday")
    on_parser.add_argument("--date",    help="Target date (YYYY-MM-DD). Defaults to next Saturday.")
    on_parser.add_argument("--start",   help="Preferred start time (HH:MM). Default: 07:00")
    on_parser.add_argument("--end",     help="Preferred end time   (HH:MM). Default: 10:00")
    on_parser.add_argument("--players", help="Number of players (1–4). Default: 3")

    # --- off ---
    sub.add_parser("off",    help="Cancel the current booking request")

    # --- status ---
    sub.add_parser("status", help="Show the current booking request status")

    args = parser.parse_args()

    if args.command == "on":
        cmd_on(args)
    elif args.command == "off":
        cmd_off(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
