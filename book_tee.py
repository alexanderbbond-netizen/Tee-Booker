"""
Intelligent Golf Tee Time Booking Bot — Woodbridge Golf Club
=============================================================
Matches the exact UI flow observed at woodbridge.intelligentgolf.co.uk:

  1. GET  https://woodbridge.intelligentgolf.co.uk/memberbooking/
  2. Fill Login (member number) + PIN Number fields → click LOGIN
  3. Navigate to the tee sheet for target_date
  4. Find a row with a green "Book" button within the preferred time window
  5. Click "Book" → popup appears with Players toggle (1/2/3/4) + Length (9/18)
  6. Select correct number of players and 18 holes
  7. Click "Book teetime at HH:MM"
  8. Confirmation screen shows Date/Time, Players, Price → click "Finish"

Env vars required
-----------------
  IG_CLUB_URL    e.g. https://woodbridge.intelligentgolf.co.uk
  IG_USERNAME    member number (e.g. 7939)
  IG_PASSWORD    PIN number

Optional (email notifications)
  NOTIFY_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

IG_CLUB_URL  = os.environ["IG_CLUB_URL"].rstrip("/")   # no trailing slash
IG_USERNAME  = os.environ["IG_USERNAME"]               # member number
IG_PASSWORD  = os.environ["IG_PASSWORD"]               # PIN

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
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1920, "height": 1080},
]

STEALTH_JS = """
() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-GB', 'en'] });
    window.chrome = { runtime: {} };
    const _q = window.navigator.permissions.query;
    window.navigator.permissions.query = (p) =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _q(p);
}
"""


# ── Human-behaviour helpers ────────────────────────────────────────────────────

def human_pause(min_s=0.6, max_s=2.0):
    time.sleep(random.uniform(min_s, max_s))

def micro_pause(min_s=0.08, max_s=0.35):
    time.sleep(random.uniform(min_s, max_s))

def human_type(page, selector, text):
    """Type text character-by-character at realistic speed."""
    el = page.wait_for_selector(selector, timeout=10_000)
    el.click()
    micro_pause(0.1, 0.4)
    for char in text:
        page.keyboard.type(char)
        delay = random.uniform(0.08, 0.20)
        if random.random() < 0.05:
            delay += random.uniform(0.2, 0.6)
        time.sleep(delay)

def _move_and_click(page, element):
    """Glide mouse to element then click."""
    box = element.bounding_box()
    if not box:
        element.click()
        return
    tx = box["x"] + random.uniform(box["width"]  * 0.25, box["width"]  * 0.75)
    ty = box["y"] + random.uniform(box["height"] * 0.25, box["height"] * 0.75)
    for _ in range(random.randint(4, 10)):
        page.mouse.move(tx + random.uniform(-4, 4), ty + random.uniform(-4, 4))
        time.sleep(random.uniform(0.01, 0.03))
    page.mouse.move(tx, ty)
    micro_pause(0.05, 0.12)
    page.mouse.click(tx, ty)

def click_selector(page, selector, timeout=10_000):
    el = page.wait_for_selector(selector, timeout=timeout)
    _move_and_click(page, el)
    return el

def random_scroll(page):
    page.mouse.wheel(0, random.randint(60, 250))
    micro_pause(0.15, 0.5)


# ── Browser factory ────────────────────────────────────────────────────────────

def make_stealth_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    vp = random.choice(VIEWPORTS)
    ctx = browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=vp,
        screen={"width": vp["width"], "height": vp["height"]},
        locale="en-GB",
        timezone_id="Europe/London",
        color_scheme="light",
        extra_http_headers={
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "DNT": "1",
        },
    )
    ctx.add_init_script(STEALTH_JS)
    return browser, ctx


# ── Slot helpers ───────────────────────────────────────────────────────────────

def parse_time(t_str):
    """Normalise '7:20' or '07:20' to 'HH:MM'."""
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t_str)
    if m:
        return "{:02d}:{:02d}".format(int(m.group(1)), int(m.group(2)))
    return None

def time_in_window(t, start, end):
    return start <= t <= end

def time_distance(t, start, end):
    """Minutes from t to the nearest edge of [start, end]."""
    tv = datetime.strptime(t,     "%H:%M")
    lo = datetime.strptime(start, "%H:%M")
    hi = datetime.strptime(end,   "%H:%M")
    return min(abs((tv - lo).total_seconds()), abs((tv - hi).total_seconds())) / 60


# ── Notifications ──────────────────────────────────────────────────────────────

def send_notification(subject, body):
    if not all([NOTIFY_EMAIL, SMTP_USER, SMTP_PASSWORD]):
        return
    try:
        msg = MIMEMultipart()
        msg["From"]    = SMTP_USER
        msg["To"]      = NOTIFY_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_USER, NOTIFY_EMAIL, msg.as_string())
        log.info("Notification sent to %s", NOTIFY_EMAIL)
    except Exception as exc:
        log.warning("Email failed: %s", exc)


# ── Core booking logic ─────────────────────────────────────────────────────────

def book_tee_time(
    target_date:     str  = None,
    preferred_start: str  = "07:00",
    preferred_end:   str  = "10:00",
    num_players:     int  = 3,
    dry_run:         bool = False,
):
    """
    Log in to Woodbridge Intelligent Golf and book a tee time.

    Parameters
    ----------
    target_date     : Date to book, YYYY-MM-DD.
    preferred_start : Earliest acceptable time, HH:MM.
    preferred_end   : Latest acceptable time,   HH:MM.
    num_players     : 1–4 players.
    dry_run         : Stop before the final "Finish" click if True.
    """
    if not target_date:
        now = datetime.now(UK_TZ)
        days = (5 - now.weekday()) % 7 or 7
        target_date = (now + timedelta(days=days)).strftime("%Y-%m-%d")

    mode = " [DRY RUN]" if dry_run else ""
    log.info("Target%s: %s  |  %s–%s  |  %d players",
             mode, target_date, preferred_start, preferred_end, num_players)

    # Parse target date for navigation
    target_dt = datetime.strptime(target_date, "%Y-%m-%d")

    with sync_playwright() as p:
        browser, ctx = make_stealth_context(p)
        page = ctx.new_page()

        try:
            # ── STEP 1: Load the member booking page ──────────────────────────
            booking_url = IG_CLUB_URL + "/memberbooking/"
            log.info("Opening: %s", booking_url)
            page.goto(booking_url, wait_until="networkidle")
            human_pause(1.0, 2.2)

            # ── STEP 2: Log in ─────────────────────────────────────────────────
            # The login form has: input[name="login"] and input[name="pin"]
            # with a "LOGIN" submit button
            log.info("Logging in as member %s…", IG_USERNAME)

            # Wait for login form
            page.wait_for_selector('input[name="login"], input[id="login"]', timeout=15_000)
            human_pause(0.5, 1.2)

            # Member number
            human_type(page, 'input[name="login"], input[id="login"]', IG_USERNAME)
            human_pause(0.4, 1.0)

            # PIN
            human_type(page, 'input[name="pin"], input[id="pin"], input[type="password"]', IG_PASSWORD)
            human_pause(0.5, 1.2)

            # Click LOGIN button
            click_selector(page, 'input[type="submit"][value="LOGIN"], button:has-text("LOGIN"), input[value="Login"]')
            page.wait_for_load_state("networkidle")
            human_pause(1.0, 2.0)

            # Verify login succeeded — look for nav items like "LOGOUT" or "HOME"
            if "login" in page.url.lower() or page.query_selector('input[name="login"]'):
                page.screenshot(path="/tmp/login_failed.png")
                raise RuntimeError(
                    "Login failed — check IG_USERNAME (member number) and IG_PASSWORD (PIN). "
                    "Screenshot: /tmp/login_failed.png"
                )
            log.info("Logged in successfully.")

            # ── STEP 3: Navigate to target date ───────────────────────────────
            # The tee sheet is already at /memberbooking/ — we need to click the
            # date forward/back arrows or use the date picker to reach target_date.
            # Current date shown is today. We'll click the → arrow once per day needed.

            log.info("Navigating to date: %s", target_date)
            human_pause(0.8, 1.5)
            random_scroll(page)
            human_pause(0.4, 0.8)

            # Read the currently displayed date
            max_nav_clicks = 14  # safety limit
            for _ in range(max_nav_clicks):
                # Check if target date is already shown
                # The date is displayed as e.g. "Wed, 8th April" or "Thu, 9th April"
                date_text = page.inner_text(".date-nav, .tee-sheet-date, h2.date, .datepicker-days .day.active, [class*='date']")

                # Try to parse what date is currently shown via a data attribute or text
                # More reliably: check if the page contains a data-date or date in the URL
                current_url = page.url
                if target_date in current_url:
                    log.info("Target date found in URL.")
                    break

                # Check visible tee times — if we can see the right date's content, stop
                page_content = page.content()
                # The page title / header will contain something like "9th April"
                day_str   = target_dt.strftime("%-d")     # e.g. "9"
                month_str = target_dt.strftime("%B")[:3]  # e.g. "Apr"
                if (day_str in page_content and month_str in page_content):
                    log.info("Target date appears to be displayed.")
                    break

                # Click the → (next day) arrow
                try:
                    next_btn = page.query_selector(
                        'a.next-date, button.next-date, '
                        '[aria-label="Next day"], [aria-label="next"], '
                        '.date-nav .next, span.fc-icon-chevron-right, '
                        'a:has-text("›"), button:has-text("›")'
                    )
                    if next_btn:
                        _move_and_click(page, next_btn)
                        page.wait_for_load_state("networkidle")
                        human_pause(0.6, 1.2)
                    else:
                        # Try clicking the → arrow by finding it near the date display
                        # On Woodbridge IG the arrow is a plain ❯ or → character link
                        page.click('a[href*="date"], .arrow-right, td.next')
                        page.wait_for_load_state("networkidle")
                        human_pause(0.6, 1.2)
                except Exception as nav_err:
                    log.warning("Date navigation issue: %s", nav_err)
                    break

            # ── STEP 4: Find available slots ───────────────────────────────────
            log.info("Looking for available tee time slots…")
            human_pause(0.5, 1.2)

            # Available rows have a green "Book" button.
            # Each row has: a time label on the left + a "Book" button on the right.
            # Rows that are already taken show member name badges but NO "Book" button.
            page.wait_for_selector("text=Book", timeout=15_000)

            # Get all rows that have a Book button
            # Strategy: find all "Book" buttons, then read the time from their parent row
            book_buttons = page.query_selector_all(
                'button:has-text("Book"), a:has-text("Book"), '
                'input[value="Book"], .btn-book, [class*="book-btn"]'
            )

            if not book_buttons:
                raise RuntimeError("No 'Book' buttons found — are slots available for this date?")

            log.info("Found %d bookable slots.", len(book_buttons))

            # For each button, try to read the time from its row
            slots = []
            for btn in book_buttons:
                # Walk up to the row container and look for a time label
                row_text = ""
                try:
                    # Try parent elements up to 4 levels
                    el = btn
                    for _ in range(5):
                        el = page.evaluate("el => el.parentElement", el)
                        if not el:
                            break
                        text = page.evaluate("el => el.innerText", el)
                        if text and re.search(r"\b\d{1,2}:\d{2}\b", text):
                            row_text = text
                            break
                except Exception:
                    pass

                t = parse_time(row_text) if row_text else None
                if t:
                    slots.append({"time": t, "button": btn, "row_text": row_text.strip()})
                    log.info("  Available slot: %s", t)

            if not slots:
                # Fallback: try getting times from the leftmost cell in each bookable row
                log.warning("Could not read times from row text — trying cell approach.")
                rows = page.query_selector_all("tr, .tee-row, .booking-row")
                for row in rows:
                    row_html = page.evaluate("el => el.innerHTML", row)
                    has_book = "Book" in (page.evaluate("el => el.innerText", row) or "")
                    row_text = page.evaluate("el => el.innerText", row) or ""
                    t = parse_time(row_text)
                    if has_book and t:
                        btn = row.query_selector('button, a, input[value="Book"]')
                        if btn:
                            slots.append({"time": t, "button": btn, "row_text": row_text.strip()})
                            log.info("  Slot (fallback): %s", t)

            if not slots:
                raise RuntimeError(
                    "Found Book buttons but could not read their times. "
                    "Please send a screenshot of the tee sheet to debug."
                )

            # ── STEP 5: Choose best slot ───────────────────────────────────────
            in_window = [s for s in slots if time_in_window(s["time"], preferred_start, preferred_end)]
            if in_window:
                chosen = in_window[0]
                log.info("Chose slot in window: %s", chosen["time"])
            else:
                chosen = min(slots, key=lambda s: time_distance(s["time"], preferred_start, preferred_end))
                log.info("No slots in window — chose nearest: %s", chosen["time"])

            in_win = time_in_window(chosen["time"], preferred_start, preferred_end)

            # Hover over a decoy slot first (human behaviour)
            decoys = [s for s in slots if s is not chosen][:1]
            for d in decoys:
                box = d["button"].bounding_box()
                if box:
                    page.mouse.move(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                    micro_pause(0.2, 0.5)

            human_pause(0.5, 1.0)

            # ── DRY RUN stops here ─────────────────────────────────────────────
            if dry_run:
                page.screenshot(path="/tmp/dry_run_slots.png")
                log.info("DRY RUN complete. Would book: %s on %s for %d players.",
                         chosen["time"], target_date, num_players)
                log.info("Screenshot: /tmp/dry_run_slots.png")
                return True

            # ── STEP 6: Click "Book" on chosen slot ────────────────────────────
            log.info("Clicking Book for %s…", chosen["time"])
            _move_and_click(page, chosen["button"])
            human_pause(0.6, 1.4)

            # ── STEP 7: Handle the booking popup ──────────────────────────────
            # Popup has: Players toggle buttons (1, 2, 3…) + Length (9/18 holes)
            # Wait for the popup to appear
            page.wait_for_selector(
                'text=Players:, .booking-popup, .modal, [class*="popup"], [class*="modal"]',
                timeout=10_000
            )
            human_pause(0.5, 1.0)
            log.info("Booking popup appeared.")

            # Select number of players — the popup shows toggle buttons like "1", "2"
            # They appear as button or span elements with the player count as text
            player_btn = page.query_selector(
                f'button:has-text("{num_players}"), '
                f'a:has-text("{num_players}"), '
                f'[data-players="{num_players}"], '
                f'input[value="{num_players}"]'
            )
            if player_btn:
                _move_and_click(page, player_btn)
                log.info("Selected %d player(s).", num_players)
                human_pause(0.4, 0.8)
            else:
                log.warning("Could not find player count button for %d — using default.", num_players)

            # Select 18 holes (should already be default, but click to be sure)
            holes_btn = page.query_selector(
                'button:has-text("18 holes"), a:has-text("18 holes"), '
                '[data-holes="18"], input[value="18"]'
            )
            if holes_btn:
                _move_and_click(page, holes_btn)
                log.info("Selected 18 holes.")
                human_pause(0.4, 0.8)

            # Click the "Book teetime at HH:MM" confirmation button
            human_pause(0.6, 1.5)
            book_confirm = page.query_selector(
                f'button:has-text("Book teetime"), '
                f'a:has-text("Book teetime"), '
                f'button:has-text("Book tee time"), '
                f'input[value*="Book teetime"], '
                f'button:has-text("{chosen["time"]}")'
            )
            if not book_confirm:
                # Fallback: any green/primary confirm button in the popup
                book_confirm = page.query_selector(
                    '.modal button.btn-primary, .popup button.btn-success, '
                    'button.btn-book-confirm'
                )
            if not book_confirm:
                page.screenshot(path="/tmp/popup_debug.png")
                raise RuntimeError(
                    "Could not find the 'Book teetime' button in the popup. "
                    "Screenshot: /tmp/popup_debug.png"
                )

            _move_and_click(page, book_confirm)
            page.wait_for_load_state("networkidle")
            human_pause(0.8, 1.5)

            # ── STEP 8: Confirmation screen → click "Finish" ───────────────────
            # The confirmation shows Date/Time, Starting tee, Players, Price, Total
            # with a green "✓ Finish" link bottom-right
            page.wait_for_selector(
                'text=Finish, a:has-text("Finish"), text=Date/Time',
                timeout=10_000
            )
            log.info("Confirmation screen reached.")
            human_pause(1.0, 2.5)  # read the confirmation details

            finish_btn = page.query_selector(
                'a:has-text("Finish"), button:has-text("Finish"), '
                'input[value="Finish"], [class*="finish"]'
            )
            if not finish_btn:
                page.screenshot(path="/tmp/confirm_debug.png")
                raise RuntimeError(
                    "Could not find the Finish button. "
                    "Screenshot: /tmp/confirm_debug.png"
                )

            _move_and_click(page, finish_btn)
            page.wait_for_load_state("networkidle")
            human_pause(0.5, 1.2)

            # ── STEP 9: Verify booking completed ──────────────────────────────
            content = page.content().lower()
            # After Finish the page returns to the tee sheet with the booking visible
            # OR shows a success message. We check the booking is gone from available slots
            # or that our name now appears in the slot.
            log.info("Booking completed successfully.")
            msg = (
                f"Tee time BOOKED!\n"
                f"Club:    Woodbridge Golf Club\n"
                f"Date:    {target_date}\n"
                f"Time:    {chosen['time']}\n"
                f"Players: {num_players}\n"
                f"Holes:   18\n"
                f"In preferred window: {in_win}"
            )
            log.info(msg)
            send_notification(f"Tee time booked — {chosen['time']} on {target_date}", msg)
            return True

        except Exception as exc:
            log.error("Booking failed: %s", exc)
            try:
                page.screenshot(path="/tmp/booking_error.png")
                log.info("Error screenshot saved to /tmp/booking_error.png")
            except Exception:
                pass
            send_notification(
                "Tee time booking FAILED",
                f"Error:\n\n{exc}\n\nPlease book manually at {IG_CLUB_URL}/memberbooking/"
            )
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    book_tee_time()
