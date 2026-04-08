"""
Intelligent Golf Tee Time Booking Bot
======================================
Automatically books a tee time on intelligentgolf.co.uk.

Called by scheduler.py with the date and preferences read from
booking_request.json. Can also be run directly for testing.

Stealth features:
  - Masks navigator.webdriver flag
  - Realistic browser fingerprint (viewport, user-agent, locale, timezone)
  - Human-like random delays between every action
  - Natural multi-step mouse movement before every click
  - Randomised typing speed on login form
  - Decoy hover interactions before selecting the target slot
  - Visits homepage before /login (not a direct jump)
"""

import os
import re
import time
import random
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Config from environment variables ─────────────────────────────────────────
IG_CLUB_URL  = os.environ["IG_CLUB_URL"]
IG_USERNAME  = os.environ["IG_USERNAME"]
IG_PASSWORD  = os.environ["IG_PASSWORD"]

NOTIFY_EMAIL  = os.environ.get("NOTIFY_EMAIL", "")
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

UK_TZ = ZoneInfo("Europe/London")

# ── Realistic browser fingerprints ────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
]

STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
    window.chrome = { runtime: {} };
    const _origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery(parameters);
}
"""


# ── Human-behaviour helpers ────────────────────────────────────────────────────

def human_pause(min_s=0.6, max_s=2.2):
    time.sleep(random.uniform(min_s, max_s))

def micro_pause(min_s=0.08, max_s=0.4):
    time.sleep(random.uniform(min_s, max_s))

def human_type(page, selector, text):
    page.click(selector)
    micro_pause(0.15, 0.5)
    for char in text:
        page.keyboard.type(char)
        delay = random.uniform(0.08, 0.22)
        if random.random() < 0.05:
            delay += random.uniform(0.3, 0.7)
        time.sleep(delay)

def _move_and_click(page, element):
    box = element.bounding_box()
    if not box:
        element.click()
        return
    tx = box["x"] + random.uniform(box["width"]  * 0.2, box["width"]  * 0.8)
    ty = box["y"] + random.uniform(box["height"] * 0.2, box["height"] * 0.8)
    for _ in range(random.randint(5, 12)):
        page.mouse.move(tx + random.uniform(-3, 3), ty + random.uniform(-3, 3))
        time.sleep(random.uniform(0.01, 0.04))
    page.mouse.move(tx, ty)
    micro_pause(0.05, 0.15)
    page.mouse.click(tx, ty)

def human_move_and_click(page, selector):
    el = page.query_selector(selector)
    if not el:
        page.click(selector)
        return
    _move_and_click(page, el)

def human_move_and_click_element(page, element):
    _move_and_click(page, element)

def random_scroll(page):
    page.mouse.wheel(0, random.randint(80, 300))
    micro_pause(0.2, 0.6)


# ── Stealth browser factory ────────────────────────────────────────────────────

def make_stealth_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    viewport = random.choice(VIEWPORTS)
    context  = browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=viewport,
        locale="en-GB",
        timezone_id="Europe/London",
        screen={"width": viewport["width"], "height": viewport["height"]},
        color_scheme="light",
        extra_http_headers={
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "DNT":             "1",
        },
    )
    context.add_init_script(STEALTH_JS)
    return browser, context


# ── Booking helpers ────────────────────────────────────────────────────────────

def time_in_window(t, start, end):
    return start <= t <= end

def nearest_slot(slots, start, end):
    in_window = [s for s in slots if time_in_window(s["time"], start, end)]
    if in_window:
        return in_window[0]
    def distance(s):
        t  = datetime.strptime(s["time"], "%H:%M")
        lo = datetime.strptime(start,     "%H:%M")
        hi = datetime.strptime(end,       "%H:%M")
        return min(abs((t - lo).total_seconds()), abs((t - hi).total_seconds()))
    return min(slots, key=distance) if slots else None

def send_notification(subject, body):
    if not all([NOTIFY_EMAIL, SMTP_USER, SMTP_PASSWORD]):
        return
    try:
        msg = MIMEMultipart()
        msg["From"]    = SMTP_USER
        msg["To"]      = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo(); server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        log.info("Notification sent to %s", NOTIFY_EMAIL)
    except Exception as exc:
        log.warning("Email failed: %s", exc)


# ── Core booking logic ─────────────────────────────────────────────────────────

def book_tee_time(
    target_date:     str  = None,
    preferred_start: str  = "07:00",
    preferred_end:   str  = "10:00",
    num_players:     int  = 3,
):
    """
    Log in to Intelligent Golf and book the best available tee time.

    Parameters
    ----------
    target_date     : Date to book, YYYY-MM-DD. Defaults to next Saturday.
    preferred_start : Earliest acceptable tee time, HH:MM.
    preferred_end   : Latest acceptable tee time, HH:MM.
    num_players     : Number of players (1–4).
    """
    if not target_date:
        now = datetime.now(UK_TZ)
        days_ahead  = (5 - now.weekday()) % 7 or 7
        target_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    log.info("Targeting: %s  |  window: %s–%s  |  players: %d",
             target_date, preferred_start, preferred_end, num_players)

    with sync_playwright() as p:
        browser, context = make_stealth_context(p)
        page = context.new_page()

        try:
            # 1. Homepage first
            log.info("Opening club homepage…")
            page.goto(IG_CLUB_URL, wait_until="networkidle")
            human_pause(1.2, 2.8)
            random_scroll(page)
            human_pause(0.6, 1.4)

            # 2. Login page
            log.info("Going to login page…")
            page.goto(f"{IG_CLUB_URL}/login", wait_until="networkidle")
            human_pause(0.8, 1.8)

            # 3. Type credentials
            log.info("Entering credentials…")
            human_type(page, 'input[name="username"], input[type="email"]', IG_USERNAME)
            human_pause(0.5, 1.2)
            human_type(page, 'input[name="password"], input[type="password"]', IG_PASSWORD)
            human_pause(0.6, 1.4)
            human_move_and_click(page, 'button[type="submit"], input[type="submit"]')
            page.wait_for_load_state("networkidle")

            if "login" in page.url.lower():
                raise RuntimeError("Login failed – check IG_USERNAME / IG_PASSWORD.")
            log.info("Logged in.")
            human_pause(1.0, 2.2)

            # 4. Tee booking page
            random_scroll(page)
            human_pause(0.5, 1.0)
            page.goto(f"{IG_CLUB_URL}/tee-booking", wait_until="networkidle")
            human_pause(1.0, 2.2)

            # 5. Select date
            date_selector = f'[data-date="{target_date}"], td[data-date="{target_date}"]'
            try:
                page.wait_for_selector(date_selector, timeout=10_000)
                human_pause(0.5, 1.0)
                human_move_and_click(page, date_selector)
                page.wait_for_load_state("networkidle")
                log.info("Selected date: %s", target_date)
            except PlaywrightTimeout:
                log.warning("Date cell not found – trying calendar navigation.")
                human_move_and_click(page, 'a.next, button.fc-next-button, [aria-label="next"]')
                page.wait_for_load_state("networkidle")
                human_pause(0.5, 1.2)
                human_move_and_click(page, date_selector)
                page.wait_for_load_state("networkidle")

            human_pause(0.8, 1.8)
            random_scroll(page)
            human_pause(0.5, 1.2)

            # 6. Scrape available slots
            page.wait_for_selector(
                ".tee-time, .teetime, [class*='tee'], .booking-slot", timeout=15_000
            )
            slot_elements = page.query_selector_all(
                ".tee-time, .teetime, [class*='tee-time'], .booking-slot"
            )
            slots = []
            for el in slot_elements:
                match = re.search(r"\b(\d{1,2}:\d{2})\b", el.inner_text().strip())
                if match:
                    t       = match.group(1).zfill(5)
                    classes = el.get_attribute("class") or ""
                    if any(x in classes.lower() for x in ["full", "booked", "closed", "disabled"]):
                        continue
                    slots.append({"time": t, "element": el})

            if not slots:
                raise RuntimeError("No available slots found on the page.")
            log.info("Found %d available slots.", len(slots))

            # 7. Pick best slot
            chosen = nearest_slot(slots, preferred_start, preferred_end)
            if not chosen:
                raise RuntimeError("Could not determine a slot to book.")

            in_win = time_in_window(chosen["time"], preferred_start, preferred_end)
            log.info("Chosen: %s  (in window: %s)", chosen["time"], in_win)

            # Hover over 1–2 decoy slots first
            for d in random.sample([s for s in slots if s is not chosen],
                                   k=min(2, len(slots) - 1)):
                box = d["element"].bounding_box()
                if box:
                    page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                    micro_pause(0.15, 0.45)
            human_pause(0.4, 1.0)

            # 8. Click slot
            human_move_and_click_element(page, chosen["element"])
            page.wait_for_load_state("networkidle")
            human_pause(0.8, 1.6)

            # 9. Set players
            try:
                page.select_option(
                    'select[name="players"], select[name="numberOfPlayers"], select[id*="player"]',
                    str(num_players)
                )
                human_pause(0.5, 1.0)
            except Exception:
                try:
                    human_move_and_click(
                        page,
                        f'[data-players="{num_players}"], '
                        f'input[value="{num_players}"][name*="player"]'
                    )
                    human_pause(0.5, 1.0)
                except Exception:
                    log.warning("Could not set player count – using site default.")

            # 10. Review pause
            human_pause(1.2, 2.8)

            # 11. Confirm
            human_move_and_click(
                page,
                'button[type="submit"], input[type="submit"], '
                'a.confirm-booking, button:has-text("Confirm"), button:has-text("Book")'
            )
            page.wait_for_load_state("networkidle")
            human_pause(0.5, 1.2)

            # 12. Verify
            content = page.content().lower()
            if any(w in content for w in ["confirmed", "booked", "booking reference", "thank you"]):
                msg = (f"✅ Tee time BOOKED!\n"
                       f"Date:    {target_date}\n"
                       f"Time:    {chosen['time']}\n"
                       f"Players: {num_players}\n"
                       f"In preferred window: {in_win}")
                log.info(msg)
                send_notification(f"⛳ Tee time booked – {chosen['time']} on {target_date}", msg)
                return True
            else:
                page.screenshot(path="/tmp/booking_failed.png")
                raise RuntimeError(
                    "Confirmation text not found. Screenshot: /tmp/booking_failed.png"
                )

        except Exception as exc:
            log.error("Booking failed: %s", exc)
            send_notification(
                "❌ Tee time booking FAILED",
                f"Error:\n\n{exc}\n\nPlease book manually."
            )
            raise
        finally:
            browser.close()


# ── Entry point (for manual testing) ──────────────────────────────────────────

if __name__ == "__main__":
    book_tee_time()
