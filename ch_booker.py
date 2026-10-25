#!/usr/bin/env python3
"""
Chrome Hearts auto-booker (Waitwhile)

Two modes, picked when you start it:

  python3 ch_booker.py test   -> Chrome Hearts ASPEN, Oct 26. Fills out the whole
                                 form but does NOT click "Schedule booking".
                                 Nothing gets booked. Just proves it works.

  python3 ch_booker.py aspen  -> Aspen, Oct 26. REAL booking (cancel it after).
  python3 ch_booker.py nov6   -> NYC West Village, FIRST open day in the booking window
                                 (Nov 6, Nov 7 once it opens, or anything earlier).
                                 REAL booking (cancel it after). Checks every ~30s.

  python3 ch_booker.py learn  -> NYC West Village, WATCH ONLY. Never books. Records when
                                 new days get added and pings your phone when it happens.

  python3 ch_booker.py nyc    ->Chrome Hearts NEW YORK WEST VILLAGE, trip days Jan 24-27.
                                 Books the earliest opening on any of those days, for real.
                                 (Days only become bookable ~13 days ahead, so until about
                                 Jan 11 it just reports "not bookable yet".)

It checks about every 60-80 seconds. Stop anytime with Control + C.

It also watches for the moment a new day is added to the booking window, saves it
in ch_release_<store>.json, and checks every ~5 seconds around that time each day after.

Add --server (e.g. "python3 ch_booker.py nov6 --server") to run on a cloud machine:
no browser window, no "press Enter" prompts, alerts go to your phone through ntfy
(set CH_NTFY_TOPIC).
"""

import json
import os
import platform
import random
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

# ===================== YOUR INFO =====================
# Your details live in ch_info.json next to this script (kept out of GitHub), e.g.
#   {"FIRST_NAME": "...", "LAST_NAME": "...", "PHONE": "...", "EMAIL": "...",
#    "GUEST_NAMES": "...", "NTFY_TOPIC": "..."}
# On GitHub Actions they come from repo secrets instead (CH_FIRST_NAME, CH_PHONE, ...).
try:
    _INFO = json.loads(Path(__file__).with_name("ch_info.json").read_text())
except FileNotFoundError:
    _INFO = {}


def _setting(key):
    return os.environ.get("CH_" + key) or _INFO.get(key, "")


FIRST_NAME = _setting("FIRST_NAME")
LAST_NAME = _setting("LAST_NAME")
PHONE = _setting("PHONE")
EMAIL = _setting("EMAIL")
GUEST_NAMES = _setting("GUEST_NAMES")
PARTY_SIZE = "2"
SERVICE = "Personal Shopping"
NTFY_TOPIC = _setting("NTFY_TOPIC")  # phone alerts, e.g. "mike-ch-slots-4821"
CHECK_EVERY_SECONDS = 60
# =====================================================

# --server: no browser window, no "press Enter" prompts, alerts go to your phone (ntfy).
SERVER = "--server" in sys.argv
# --minutes N: stop cleanly after N minutes (GitHub Actions jobs max out at 6 hours).
_m = re.search(r"--minutes[= ](\d+)", " ".join(sys.argv))
STOP_AFTER_SECONDS = int(_m.group(1)) * 60 if _m else None

# New days get added to the booking window once a day. The script records when
# it sees that happen and checks every FAST_EVERY_SECONDS around that time.
# One file per store, e.g. ch_release_chromehearts.json (set in main()).
STATE_FILE = None
STORE_TZ = ZoneInfo("America/New_York")
FAST_EVERY_SECONDS = 5
FAST_BEFORE_MIN = 10     # start fast checks this long before the usual release time
FAST_AFTER_MIN = 20      # ...and keep going this long after

MODES = {
    "test": {
        "url": "https://waitwhile.com/locations/chromeheartsaspen",
        "name": "Chrome Hearts Aspen (TEST - will NOT book)",
        "dates": [("Oct", 26)],
        "dry_run": True,
    },
    "aspen": {
        "url": "https://waitwhile.com/locations/chromeheartsaspen",
        "name": "Chrome Hearts Aspen (REAL booking - cancel after)",
        "dates": [("Oct", 26)],
        "dry_run": False,
    },
    "nov6": {
        "url": "https://waitwhile.com/locations/chromehearts",
        "name": "Chrome Hearts New York West Village (REAL booking, first open day - cancel after)",
        "dates": "any",
        "dry_run": False,
        "every": 30,
    },
    "learn": {
        "url": "https://waitwhile.com/locations/chromehearts",
        "name": "Chrome Hearts New York West Village (WATCH ONLY - never books)",
        "dates": "any",
        "dry_run": True,
        "watch_only": True,
        "every": 30,
    },
    "nyc": {
        "url": "https://waitwhile.com/locations/chromehearts",
        "name": "Chrome Hearts New York West Village (trip Jan 24-27)",
        "dates": [("Jan", 24), ("Jan", 25), ("Jan", 26), ("Jan", 27)],
        "dry_run": False,
    },
}

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:AM|PM)\b")

CLICK_DATE_JS = r"""([mon, day]) => {
  const re = new RegExp('^\\s*[A-Za-z]{3}\\s+' + day + '\\s+' + mon + '\\s*$');
  let best = null;
  for (const el of document.querySelectorAll('body *')) {
    const t = (el.innerText || '').trim();
    if (re.test(t) && (!best || best.contains(el))) best = el;
  }
  if (!best) return false;
  best.scrollIntoView({block: 'center', inline: 'center'});
  best.click();
  return true;
}"""

# Every date tile in the picker ("Sun 25 Oct"), earliest first, as [mon, day].
LIST_DATES_JS = r"""() => {
  const re = /^\s*[A-Za-z]{3}\s+(\d{1,2})\s+([A-Za-z]{3})\s*$/;
  const tiles = [];
  for (const el of document.querySelectorAll('body *')) {
    const m = (el.innerText || '').trim().match(re);
    if (!m) continue;
    const last = tiles[tiles.length - 1];
    if (last && last.el.contains(el)) tiles.pop();   // keep the innermost match
    tiles.push({el, mon: m[2], day: Number(m[1])});
  }
  return tiles.map(t => [t.mon, t.day]);
}"""

# Waitwhile's location prompt. Granting it up front means no popup.
STORE_LOCATION = {"latitude": 40.7359, "longitude": -74.0036}  # West Village


def log(msg):
    print(f"[{datetime.now().strftime('%b %d %I:%M:%S %p')}] {msg}", flush=True)


def alert(title, msg, url=""):
    log(f"*** {title}: {msg} ***")
    safe = msg.replace('"', "'")
    try:
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification "{safe}" with title "{title}" sound name "Glass"'])
            subprocess.run(["say", title])
        elif platform.system() == "Windows":
            import winsound
            for _ in range(6):
                winsound.Beep(1200, 350)
        else:
            print("\a" * 5)
    except Exception as e:
        log(f"(local alert failed: {e})")
    if NTFY_TOPIC:
        try:
            headers = {"Title": title, "Priority": "urgent", "Tags": "rotating_light"}
            if url:
                headers["Click"] = url
            req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                         data=msg.encode("utf-8"), headers=headers)
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"(phone push failed: {e})")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def note_last_date(state, last):
    """Called every check with the last bookable day. Records when a new one appears."""
    now = datetime.now(STORE_TZ)
    prev, prev_check = state.get("last_date"), state.get("last_check")
    if prev and last != prev and prev_check:
        before = datetime.fromisoformat(prev_check)
        if (now - before).total_seconds() < 15 * 60:
            state.setdefault("releases", []).append({"after": prev_check, "seen": now.isoformat()})
            msg = (f"New day {last} was added between {before:%I:%M:%S %p} and "
                   f"{now:%I:%M:%S %p} New York time.")
            log(f"{msg} Will check fast around then every day.")
            alert("New Chrome Hearts day added", msg)
        else:
            log(f"New day {last} was added sometime since {before:%b %d %I:%M %p} "
                f"New York time (script was off, so can't pin down when)")
    state["last_date"], state["last_check"] = last, now.isoformat()
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"(couldn't save {STATE_FILE.name}: {e})")


def release_seconds(state):
    """Usual release time as seconds after midnight New York time, or None if not seen yet."""
    secs = []
    for r in state.get("releases", [])[-7:]:
        t = datetime.fromisoformat(r["seen"]).astimezone(STORE_TZ)
        secs.append(t.hour * 3600 + t.minute * 60 + t.second)
    return sorted(secs)[len(secs) // 2] if secs else None


def next_wait(state, normal_every):
    """Seconds to sleep before the next check: short near the release time."""
    rel = release_seconds(state)
    jitter = random.uniform(0, 10)
    if rel is None:
        return normal_every + jitter
    now = datetime.now(STORE_TZ)
    now_s = now.hour * 3600 + now.minute * 60 + now.second
    diff = (now_s - rel + 43200) % 86400 - 43200   # seconds past release, -12h..+12h
    if -FAST_BEFORE_MIN * 60 <= diff <= FAST_AFTER_MIN * 60:
        return FAST_EVERY_SECONDS + random.uniform(0, 2)
    until_fast = -FAST_BEFORE_MIN * 60 - diff
    if 0 < until_fast < normal_every + jitter:
        return until_fast
    return normal_every + jitter


def release_label(state):
    rel = release_seconds(state)
    if rel is None:
        return "release time not known yet (learning it)"
    return (f"new days usually appear ~{rel // 3600 % 12 or 12}:{rel // 60 % 60:02d} "
            f"{'AM' if rel < 43200 else 'PM'} New York time")


def go_to_date_picker(page, url):
    page.goto(url, wait_until="domcontentloaded")
    page.get_by_text("Schedule a booking", exact=True).first.click(timeout=30000)
    page.get_by_text(SERVICE, exact=True).first.click(timeout=30000)
    page.get_by_text(PARTY_SIZE, exact=True).first.click(timeout=30000)
    page.get_by_text("Select date and time").first.wait_for(timeout=30000)
    page.wait_for_timeout(2500)


def find_open_times(page, dates):
    """Clicks each target date. Returns (label, [times]) for the first date with openings.
    dates == "any" means every date the picker shows, earliest first."""
    if dates == "any":
        dates = page.evaluate(LIST_DATES_JS)
        if not dates:
            log("couldn't find any date tiles, screenshot saved to ch_check.png")
            page.screenshot(path="ch_check.png")
            return None, []
        # The picker opens on the first day and says how many days after it are empty.
        body = page.inner_text("body")
        m = re.search(r"No available times for the next (\d+) days", body)
        if "No available times this day" in body and m and int(m.group(1)) + 1 >= len(dates):
            log(f"{dates[0][0]} {dates[0][1]} - {dates[-1][0]} {dates[-1][1]}: no open times")
            return None, []
    for mon, day in dates:
        label = f"{mon} {day}"
        if not page.evaluate(CLICK_DATE_JS, [mon, day]):
            log(f"{label}: not bookable yet (outside the booking window)")
            continue
        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        if "No available times this day" in body:
            log(f"{label}: in window, no open times")
            continue
        times = list(dict.fromkeys(TIME_RE.findall(body)))
        if times:
            return label, times
        log(f"{label}: couldn't read times, screenshot saved to ch_check.png")
        page.screenshot(path="ch_check.png")
    return None, []


def answer_yes_dropdowns(page):
    # The Yes questions are dropdowns with a hidden typing box inside.
    # Focus each one, type "Yes", press Enter.
    boxes = page.locator('input[role="combobox"]')
    for i in range(boxes.count()):
        box = boxes.nth(i)
        box.scroll_into_view_if_needed()
        box.focus()
        page.keyboard.type("Yes")
        page.wait_for_timeout(400)
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
    if page.get_by_text("Select", exact=True).count() > 0:
        raise RuntimeError("couldn't answer all the Yes questions")


def fill_and_book(page, time_text, dry_run):
    """Returns 'dry', 'booked', or raises."""
    page.get_by_text(time_text, exact=True).first.click(timeout=15000)
    details = page.get_by_text("Enter your details").first
    robot_check = page.get_by_text("verify you're not a robot").first
    details.or_(robot_check).first.wait_for(timeout=30000)
    if robot_check.is_visible():
        # The site's reCAPTCHA. The script never answers it; a person has to.
        if SERVER:
            raise RuntimeError("the site asked for an \"I'm not a robot\" check")
        alert("Tick I'm not a robot",
              f"{time_text} is in hand - tick the box in the browser window, "
              f"the script finishes the rest")
        details.wait_for(timeout=180000)
    page.get_by_placeholder("First name").fill(FIRST_NAME)
    page.get_by_placeholder("Last name").fill(LAST_NAME)
    page.get_by_placeholder("Phone number").fill(PHONE)
    page.get_by_placeholder("Email").fill(EMAIL)
    page.get_by_placeholder("Enter", exact=True).first.fill(GUEST_NAMES)
    answer_yes_dropdowns(page)
    page.wait_for_timeout(500)

    button = page.get_by_role("button", name="Schedule booking")
    if not button.is_enabled():
        raise RuntimeError("form filled but the Schedule booking button is still grayed out")

    if dry_run:
        page.screenshot(path="ch_test_form.png", full_page=True)
        return "dry"

    button.click()
    page.wait_for_timeout(6000)
    page.screenshot(path="ch_result.png", full_page=True)
    if "/details" in page.url:
        raise RuntimeError("clicked Schedule booking but stayed on the form (maybe a code or error)")
    return "booked"


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if mode not in MODES:
        print("Start it with one of these:\n"
              "  python3 ch_booker.py test   (safe test on Aspen, books nothing)\n"
              "  python3 ch_booker.py aspen  (REAL booking, Aspen Oct 26)\n"
              "  python3 ch_booker.py nov6   (REAL booking, NYC West Village, first open day)\n"
              "  python3 ch_booker.py learn  (watch only, learns when new days open, never books)\n"
              "  python3 ch_booker.py nyc    (REAL booking, NYC West Village, Jan 24-27)")
        return
    cfg = MODES[mode]
    dates_txt = ("the first open day" if cfg["dates"] == "any"
                 else ", ".join(f"{m} {d}" for m, d in cfg["dates"]))
    missing = [k for k in ("FIRST_NAME", "LAST_NAME", "PHONE", "EMAIL", "GUEST_NAMES")
               if not globals()[k]]
    if missing:
        print(f"Missing your details: {', '.join(missing)}. "
              f"Put them in ch_info.json (or CH_* environment variables).")
        sys.exit(1)
    global STATE_FILE
    STATE_FILE = Path(__file__).with_name(f"ch_release_{cfg['url'].rstrip('/').split('/')[-1]}.json")
    state = load_state()
    booked_key = f"booked_{mode}"   # per mode, so the Nov 6 booking doesn't block the trip
    if state.get(booked_key) and not cfg["dry_run"]:
        log(f"Already booked {state[booked_key]} - not booking again. "
            f"Remove \"{booked_key}\" from {STATE_FILE.name} to start over.")
        return
    log(f"{cfg['name']} | watching {dates_txt} | {SERVICE}, party of {PARTY_SIZE}. "
        f"Control + C to stop.")
    log(f"{release_label(state)}{' | server mode' if SERVER else ''}")
    if SERVER and not NTFY_TOPIC:
        log("WARNING: server mode with no CH_NTFY_TOPIC set - you won't get phone alerts")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=SERVER)
        context = browser.new_context(permissions=["geolocation"],
                                      geolocation=STORE_LOCATION)
        page = context.new_page()
        was_fast = False
        alerted = {}
        started = time.time()
        while True:
            if STOP_AFTER_SECONDS and time.time() - started > STOP_AFTER_SECONDS:
                log("Time limit reached, stopping (the next scheduled run picks up from here)")
                return
            try:
                go_to_date_picker(page, cfg["url"])
                tiles = page.evaluate(LIST_DATES_JS)
                if tiles:
                    note_last_date(state, f"{tiles[-1][0]} {tiles[-1][1]}")
                if cfg.get("watch_only"):
                    log(f"Bookable through {tiles[-1][0]} {tiles[-1][1]}" if tiles
                        else "couldn't read the date tiles")
                    label, times = None, []
                else:
                    label, times = find_open_times(page, cfg["dates"])
                if times:
                    log(f"{label} open times: {', '.join(times)}. Grabbing {times[0]}...")
                    start = time.time()
                    try:
                        result = fill_and_book(page, times[0], cfg["dry_run"])
                        log(f"Form filled{' and submitted' if result == 'booked' else ''} "
                            f"in {time.time() - start:.1f} seconds")
                    except Exception as e:
                        page.screenshot(path="ch_error.png", full_page=True)
                        where = "on your phone" if SERVER else "in the browser window"
                        slot = f"{label} {times[0]}"
                        if time.time() - alerted.get(slot, 0) > 600:   # once per slot per 10 min
                            alerted[slot] = time.time()
                            alert("Chrome Hearts slot open",
                                  f"{slot} is open but auto-booking hit a snag ({e}). "
                                  f"Book it {where} NOW.", cfg["url"])
                        else:
                            log(f"Still stuck on {slot} ({e}), retrying")
                        if SERVER:
                            time.sleep(next_wait(state, cfg.get("every", CHECK_EVERY_SECONDS)))
                        else:
                            input("\nFinish booking in the browser window. "
                                  "Press Enter to keep watching, or Control + C to quit... ")
                        continue
                    if result == "dry":
                        alert("Test worked",
                              f"Filled the whole form for {label} {times[0]}. "
                              f"Did NOT book. Screenshot: ch_test_form.png")
                        if not SERVER:
                            input("\nTest done. Look over the form in the browser window, "
                                  "then press Enter to close (nothing was booked)... ")
                        return
                    state[booked_key] = f"{label} {times[0]}"
                    STATE_FILE.write_text(json.dumps(state, indent=2))
                    alert("Chrome Hearts booked",
                          f"Booked {label} at {times[0]}. Check {EMAIL} for the confirmation.",
                          cfg["url"])
                    if not SERVER:
                        input("\nBooked! Check your email for the confirmation. "
                              "Press Enter to close... ")
                    return
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"Check failed ({type(e).__name__}: {e}). Screenshot saved to ch_error.png")
                try:
                    page.screenshot(path="ch_error.png")
                except Exception:
                    pass
            wait = next_wait(state, cfg.get("every", CHECK_EVERY_SECONDS))
            fast = wait <= FAST_EVERY_SECONDS + 2
            if fast != was_fast:
                log("Release time is close: checking every few seconds" if fast
                    else "Past the release window: back to normal checks")
                was_fast = fast
            time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
