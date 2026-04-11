"""
Intelligent Golf Tee Time Booking Bot  —  v2
=============================================
Speed-first architecture for Woodbridge Golf Club:

  Phase 1 (before 8pm): Login once, navigate to the target date booking page,
                         wait silently on the page.
  Phase 2 (at 8pm):     Refresh the page, immediately grab the first available
                         Book button in the preferred time window, click it.
  Phase 3 (after Book): Add 2 guests from previous guest list, click Finish.

Retries re-use the already-open browser session — no re-login.
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

# ── Config ─────────────────────────────────────────────────────────────────────
IG_CLUB_URL  = os.environ["IG_CLUB_URL"]
IG_USERNAME  = os.environ["IG_USERNAME"]
IG_PASSWORD  = os.environ["IG_PASSWORD"]

NOTIFY_EMAIL  = os.environ.get("NOTIFY_EMAIL", "")
SMTP_HOST     = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")

UK_TZ = ZoneInfo("Europe/London")

# ── Browser fingerprints ───────────────────────────────────────────────────────
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

# ── Timing ─────────────────────────────────────────────────────────────────────
RELEASE_HOUR   = 20
RELEASE_MINUTE = 0
JITTER_MIN     = 1   # seconds after 20:00 to refresh (kept very short for speed)
JITTER_MAX     = 4


# ── Helpers ────────────────────────────────────────────────────────────────────

def human_pause(min_s=0.5, max_s=1.5):
    time.sleep(random.uniform(min_s, max_s))

def micro_pause(min_s=0.05, max_s=0.2):
    time.sleep(random.uniform(min_s, max_s))

def fast_click(page, selector):
    """Click as fast as possible — used after 8pm when speed matters."""
    el = page.query_selector(selector)
    if el:
        el.click()
        return True
    return False

def fast_click_element(element):
    element.click()

def human_type(page, selector, text):
    page.fill(selector, "")
    page.fill(selector, text)

def seconds_until_release():
    now    = datetime.now(UK_TZ)
    target = now.replace(hour=RELEASE_HOUR, minute=RELEASE_MINUTE,
                         second=0, microsecond=0)
    delta  = (target - now).total_seconds()
    return delta if delta > 0 else 0

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
        log.info("Notification sent.")
    except Exception as exc:
        log.warning("Email failed: %s", exc)

def make_stealth_context(playwright):
    viewport = random.choice(VIEWPORTS)
    browser  = playwright.chromium.launch(
        headless=True,
        args=["--disable-blink-features=AutomationControlled",
              "--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = browser.new_context(
        user_agent=random.choice(USER_AGENTS),
        viewport=viewport,
        locale="en-GB",
        timezone_id="Europe/London",
        screen={"width": viewport["width"], "height": viewport["height"]},
        color_scheme="light",
        extra_http_headers={
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "DNT": "1",
        },
    )
    context.add_init_script(STEALTH_JS)
    return browser, context


# ── Phase 1: Login and pre-position ───────────────────────────────────────────

def login_and_preposition(page, target_date):
    """
    Log in and navigate to the booking page for target_date.
    Returns when the page is loaded and ready — bot then waits for 8pm.
    """
    # Homepage first (looks natural)
    log.info("Opening homepage…")
    page.goto(IG_CLUB_URL, wait_until="networkidle", timeout=30_000)
    human_pause(1.0, 2.0)

    # Login
    log.info("Logging in…")
    page.goto(f"{IG_CLUB_URL}/login.php", wait_until="networkidle", timeout=30_000)
    human_pause(0.5, 1.0)

    page.wait_for_selector('input[name="memberid"]', timeout=20_000)
    human_type(page, 'input[name="memberid"]', IG_USERNAME)
    human_pause(0.3, 0.7)

    passwd_sel = None
    for sel in ['input[name="passwd"]', 'input[name="password"]', 'input[type="password"]']:
        if page.query_selector(sel):
            passwd_sel = sel
            break
    if not passwd_sel:
        raise RuntimeError("Password field not found.")
    human_type(page, passwd_sel, IG_PASSWORD)
    human_pause(0.3, 0.6)

    submit = page.query_selector('input[type="submit"], button[type="submit"]')
    if submit:
        submit.click()
    else:
        page.keyboard.press("Enter")
    page.wait_for_load_state("networkidle")

    if "login" in page.url.lower():
        raise RuntimeError("Login failed — check credentials.")
    log.info("Logged in. URL: %s", page.url)

    # Navigate to the booking page for the target date
    booking_url = f"{IG_CLUB_URL}/memberbooking/?date={target_date}"
    log.info("Pre-positioning on booking page: %s", booking_url)
    page.goto(booking_url, wait_until="networkidle", timeout=30_000)
    human_pause(0.5, 1.0)
    log.info("Ready and waiting on: %s | title: %s", page.url, page.title())
    page.screenshot(path="/tmp/preposition.png")


# ── Phase 2: Refresh at 8pm and grab a slot ───────────────────────────────────

def grab_slot(page, target_date, preferred_start, preferred_end, num_players):
    """
    Refresh the booking page and immediately click the best available Book button.
    Returns the chosen time string, or raises if nothing available.
    """
    booking_url = f"{IG_CLUB_URL}/memberbooking/?date={target_date}"

    log.info("Refreshing booking page at release time…")
    page.goto(booking_url, wait_until="networkidle", timeout=15_000)
    page.screenshot(path="/tmp/after_refresh.png")

    # Scan all rows for Book buttons
    all_rows = page.query_selector_all("tr")
    slots = []
    for row in all_rows:
        row_text = row.inner_text().strip()
        time_match = re.search(r"^(\d{2}:\d{2})", row_text)
        if not time_match:
            continue
        t = time_match.group(1)

        book_btn = row.query_selector('button:has-text("Book"), a:has-text("Book")')
        if not book_btn:
            continue

        # Check how many slots are free
        avail_match = re.search(r"(\d+)\s+slots?\s+available", row_text, re.IGNORECASE)
        if avail_match:
            empty_slots = int(avail_match.group(1))
        else:
            cells  = row.query_selector_all("td")
            filled = sum(
                1 for c in cells[1:]
                if c.inner_text().strip()
                and "book" not in c.inner_text().strip().lower()
                and "slot" not in c.inner_text().strip().lower()
            )
            empty_slots = max(0, 4 - filled)

        if empty_slots >= num_players:
            slots.append({"time": t, "book_btn": book_btn, "empty": empty_slots})
            log.info("  Bookable: %s (%d empty)", t, empty_slots)

    if not slots:
        page.screenshot(path="/tmp/no_slots.png")
        raise RuntimeError("No slots available with enough space.")

    chosen = nearest_slot(slots, preferred_start, preferred_end)
    if not chosen:
        raise RuntimeError("Could not pick a slot.")

    in_win = time_in_window(chosen["time"], preferred_start, preferred_end)
    log.info("Chosen: %s (in window: %s) — clicking Book NOW", chosen["time"], in_win)

    # Click immediately — no human delays here, speed is everything
    fast_click_element(chosen["book_btn"])
    human_pause(1.5, 2.5)
    page.screenshot(path="/tmp/after_book_click.png")
    log.info("Post-Book URL: %s", page.url)

    # After clicking Book, a Players/Length popup appears:
    #   Players: [1] [2] [3] [4]
    #   Length:  [9 holes] [18 holes]
    #   [Book teetime at HH:MM]
    # Must select correct player count, confirm 18 holes, then click confirm.
    try:
        page.wait_for_selector('button:has-text("Book teetime")', timeout=8_000)
        log.info("Players/Length popup detected.")
        page.screenshot(path="/tmp/popup_detected.png")

        # Use JS to find a button whose EXACT trimmed text is the player count.
        # This avoids matching buttons that merely contain the digit "3".
        player_clicked = page.evaluate(f"""
            () => {{
                const target = '{num_players}';
                const btns = Array.from(document.querySelectorAll('button'));
                const match = btns.find(b => b.textContent.trim() === target);
                if (match) {{ match.click(); return true; }}
                return false;
            }}
        """)
        if player_clicked:
            log.info("Selected %d players via exact JS match.", num_players)
        else:
            log.warning("Could not find exact player count button for %d.", num_players)
        human_pause(0.4, 0.7)

        # Ensure 18 holes is selected (click it to be sure)
        holes_clicked = page.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('button'));
                const match = btns.find(b => b.textContent.trim() === '18 holes');
                if (match) { match.click(); return true; }
                return false;
            }
        """)
        log.info("18 holes selected: %s", holes_clicked)
        human_pause(0.3, 0.5)

        # Click the "Book teetime at HH:MM" confirm button
        confirm_btn = page.query_selector('button:has-text("Book teetime")')
        if confirm_btn:
            log.info("Confirming popup: %s", confirm_btn.inner_text().strip())
            fast_click_element(confirm_btn)
            human_pause(2.5, 3.5)
            page.screenshot(path="/tmp/after_popup_confirm.png")
        else:
            log.warning("Book teetime confirm button not found after selections.")

    except PlaywrightTimeout:
        log.info("No players/length popup detected — continuing.")

    return chosen["time"], in_win


# -- Phase 3: Add guests and finish --------------------------------------------

def add_guests_and_finish(page, num_players, dry_run=False):
    """
    Add (num_players - 1) guests from the previous guest list, then click Finish.
    Flow per partner:
      1. "Who are you playing with?" modal appears automatically
      2. Click "A GUEST"
      3. Guest list appears — pick a random name
      4. Repeat for next partner
    Then click the Finish link on the summary screen.
    """
    if dry_run:
        log.info("DRY RUN -- skipping guest selection and finish.")
        return True

    partners_needed = num_players - 1
    log.info("Adding %d guest(s)...", partners_needed)

    excluded = {
        "ANOTHER MEMBER", "A GUEST", "CANCEL", "FINISH", "BOOK",
        "ADD A NEW GUEST", "Another Member", "A Guest",
        "Cancel", "Finish", "Book", "Add a new guest",
    }

    for partner_num in range(1, partners_needed + 1):
        log.info("--- Partner %d of %d ---", partner_num, partners_needed)

        # Wait for the "Who are you playing with?" modal
        try:
            page.wait_for_selector(
                'button:has-text("A GUEST"), button:has-text("A Guest")',
                timeout=15_000
            )
        except PlaywrightTimeout:
            page.screenshot(path=f"/tmp/partner_{partner_num}_modal_fail.png")
            log.warning("Guest modal not found for partner %d -- skipping.", partner_num)
            break

        page.screenshot(path=f"/tmp/partner_{partner_num}_modal.png")
        log.info("Guest modal visible. Clicking A GUEST...")

        guest_modal_btn = page.query_selector(
            'button:has-text("A GUEST"), button:has-text("A Guest")'
        )
        if guest_modal_btn:
            fast_click_element(guest_modal_btn)
        human_pause(1.0, 1.8)
        page.screenshot(path=f"/tmp/partner_{partner_num}_guestlist.png")

        # Pick a random name from the guest list
        # The list appears as plain buttons — exclude known action button labels
        all_btns = page.query_selector_all("button")
        guest_btns = [
            b for b in all_btns
            if b.inner_text().strip() and b.inner_text().strip() not in excluded
        ]

        if not guest_btns:
            log.warning("No previous guests found for partner %d.", partner_num)
            break

        pick = random.choice(guest_btns)
        log.info("Selected guest: %s", pick.inner_text().strip())
        fast_click_element(pick)
        human_pause(1.2, 2.0)

    # All partners added -- now click Finish
    # From screenshots: Finish is a link "tick Finish" in bottom-right of summary
    human_pause(1.5, 2.5)
    page.screenshot(path="/tmp/summary.png")
    log.info("Looking for Finish link...")

    finish_el = None
    for sel in [
        'a:has-text("Finish")',
        'button:has-text("Finish")',
        'input[value="Finish"]',
        '[class*=finish]',
        'a[href*=finish]',
    ]:
        finish_el = page.query_selector(sel)
        if finish_el:
            log.info("Found Finish via: %s", sel)
            break

    if not finish_el:
        page.screenshot(path="/tmp/no_finish.png")
        raise RuntimeError("Finish button/link not found. Screenshot saved.")

    log.info("Clicking Finish...")
    fast_click_element(finish_el)
    page.wait_for_load_state("networkidle")
    human_pause(0.5, 1.0)

    content = page.content().lower()
    page.screenshot(path="/tmp/after_finish.png")
    # Success if confirmation message or "send emails" text is visible
    if any(w in content for w in [
        "confirmed", "booked", "booking reference",
        "thank you", "success", "send emails", "email"
    ]):
        return True
    else:
        raise RuntimeError("Booking confirmation not found after Finish.")


# ── Main entry point ───────────────────────────────────────────────────────────

def book_tee_time(
    target_date:     str  = None,
    preferred_start: str  = "07:00",
    preferred_end:   str  = "10:00",
    num_players:     int  = 3,
    dry_run:         bool = False,
):
    """
    Full booking flow:
      1. Login and navigate to the booking page (before 8pm)
      2. Wait until 8pm then refresh and grab best slot
      3. Add guests and click Finish

    On retry, the browser session is reused — no re-login.
    Pass an already-open (page, browser) tuple via _session to reuse.
    """
    if not target_date:
        now = datetime.now(UK_TZ)
        days_ahead  = (5 - now.weekday()) % 7 or 7
        target_date = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

    log.info("Target: %s | window: %s–%s | players: %d | dry_run: %s",
             target_date, preferred_start, preferred_end, num_players, dry_run)

    with sync_playwright() as p:
        browser, context = make_stealth_context(p)
        page = context.new_page()

        try:
            # ── Phase 1: Login and pre-position ───────────────────────────────
            login_and_preposition(page, target_date)

            # ── Wait until 8pm (only if release hasn't happened yet) ───────────
            # Release is exactly 7 days before the target date at 20:00 UK time.
            # If the target is < 7 days away the release has already passed —
            # skip the wait and book immediately.
            now_dt     = datetime.now(UK_TZ)
            target_dt  = datetime.strptime(target_date, "%Y-%m-%d")
            release_dt = datetime(
                target_dt.year, target_dt.month, target_dt.day,
                RELEASE_HOUR, RELEASE_MINUTE, 0, tzinfo=UK_TZ
            ) - timedelta(days=7)

            wait = (release_dt - now_dt).total_seconds()

            if wait <= 0:
                log.info("Release time has already passed — booking immediately.")
            else:
                log.info("Waiting %.1fs until release at %s UK time…",
                         wait, release_dt.strftime("%H:%M:%S"))
                while wait > 5:
                    time.sleep(5)
                    wait = (release_dt - datetime.now(UK_TZ)).total_seconds()
                time.sleep(max(0, wait))

                jitter = random.uniform(JITTER_MIN, JITTER_MAX)
                log.info("Release time reached. Jitter: %.1fs", jitter)
                time.sleep(jitter)

            # ── Phase 2: Refresh and grab slot ────────────────────────────────
            chosen_time = None
            for attempt in range(1, 6):
                log.info("Grab attempt %d/5…", attempt)
                try:
                    chosen_time, in_win = grab_slot(
                        page, target_date, preferred_start, preferred_end, num_players
                    )
                    break
                except Exception as exc:
                    log.warning("Grab attempt %d failed: %s", attempt, exc)
                    if attempt < 5:
                        time.sleep(random.uniform(2, 4))
                    else:
                        raise

            if not chosen_time:
                raise RuntimeError("Could not grab any slot.")

            # ── Phase 3: Add guests and finish ────────────────────────────────
            success = add_guests_and_finish(page, num_players, dry_run=dry_run)

            if success:
                msg = (f"✅ Tee time BOOKED!\n"
                       f"Date:    {target_date}\n"
                       f"Time:    {chosen_time}\n"
                       f"Players: {num_players}\n"
                       f"In preferred window: {in_win}")
                log.info(msg)
                send_notification(
                    f"⛳ Tee time booked – {chosen_time} on {target_date}", msg
                )
                return True

        except Exception as exc:
            log.error("Booking failed: %s", exc)
            send_notification(
                "❌ Tee time booking FAILED",
                f"Error:\n\n{exc}\n\nPlease book manually."
            )
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    book_tee_time()
