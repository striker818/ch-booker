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
                                 new days get added, plus every time slot that opens up and
                                 how long it lasts (ch_openings_chromehearts.csv).
  python3 ch_booker.py report -> Shows patterns in those openings.

  python3 ch_booker.py nyc    ->Chrome Hearts NEW YORK WEST VILLAGE, trip days Jan 24-27.
                                 Books the earliest opening on any of those days, for real.
                                 (Days only become bookable ~13 days ahead, so until about
                                 Jan 11 it just reports "not bookable yet".)

It checks every 10 seconds, on the clock (:00, :10, :20... or, with --stagger,
:05, :15, :25... so two trackers together cover every 5 seconds). Control + C stops it.

It also watches for the moment a new day is added to the booking window, saves it
in ch_release_<store>.json, and checks every ~5 seconds around that time each day after.

Add --server (e.g. "python3 ch_booker.py nov6 --server") to run on a cloud machine:
no browser window, no "press Enter" prompts, alerts go to your phone through ntfy
(set CH_NTFY_TOPIC).
"""

import csv
import json
import math
import os
import platform
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
CHECK_EVERY_SECONDS = 10
# =====================================================

# --server: no browser window, no "press Enter" prompts, alerts go to your phone (ntfy).
SERVER = "--server" in sys.argv
# --minutes N: stop cleanly after N minutes (GitHub Actions jobs max out at 6 hours).
_m = re.search(r"--minutes[= ](\d+)", " ".join(sys.argv))
STOP_AFTER_SECONDS = int(_m.group(1)) * 60 if _m else None
# --stagger: check halfway between the other tracker's checks. Checks run on the clock
# (Mac at :00, :10, :20..., GitHub with --stagger at :05, :15, :25...), so together
# the page gets looked at every 5 seconds.
STAGGER = "--stagger" in sys.argv

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
        "every": 10,
    },
    "learn": {
        "url": "https://waitwhile.com/locations/chromehearts",
        "name": "Chrome Hearts New York West Village (WATCH ONLY - never books)",
        "dates": "any",
        "dry_run": True,
        "watch_only": True,
        "every": 10,
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
  const button = best.closest('button');
  if (button && button.getAttribute('aria-disabled') === 'true') return false;  // greyed out
  best.scrollIntoView({block: 'center', inline: 'center'});
  best.click();
  return true;
}"""

# Every bookable date tile in the picker ("Sun 25 Oct"), earliest first, as [mon, day].
# The newest day can show up greyed out (aria-disabled) before it opens; those are skipped.
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
  return tiles
    .filter(t => { const b = t.el.closest('button');
                   return !(b && b.getAttribute('aria-disabled') === 'true'); })
    .map(t => [t.mon, t.day]);
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
    if prev and prev_check and slot_date(last, now) > slot_date(prev, now):
        before = datetime.fromisoformat(prev_check)
        if (now - before).total_seconds() < 15 * 60:
            state.setdefault("releases", []).append({"after": prev_check, "seen": now.isoformat(),
                                                     "kind": "bookable"})
            msg = (f"New day {last} was added between {before:%I:%M:%S %p} and "
                   f"{now:%I:%M:%S %p} New York time.")
            log(f"{msg} Will check fast around then every day.")
            alert("New Chrome Hearts day added", msg)
        else:
            log(f"New day {last} was added sometime since {before:%b %d %I:%M %p} "
                f"New York time (script was off, so can't pin down when)")
    state["last_date"], state["last_check"] = last, now.isoformat()
    save_state(state)


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log(f"(couldn't save {STATE_FILE.name}: {e})")


# ----- opening tracker (learn mode): every slot that appears, and how long it lasts -----

OPENINGS_FILE = None   # e.g. ch_openings_chromehearts.csv, set in main()
OPENINGS_COLUMNS = ["event", "appointment_date", "appointment_weekday", "appointment_time",
                    "seen_at_ny", "seen_weekday", "days_ahead", "open_for_minutes", "note"]


def slot_date(label, now):
    """'Nov 6' -> date, picking the year that puts it near now (handles Dec -> Jan)."""
    d = datetime.strptime(f"{label} {now.year}", "%b %d %Y").date()
    if (d - now.date()).days < -60:
        d = d.replace(year=now.year + 1)
    return d


def scan_open_slots(page, tiles):
    """Every open time in the booking window right now, as a set of ("Nov 6", "1:30 PM").
    Only reads the page - never picks a time."""
    body = page.inner_text("body")
    m = re.search(r"No available times for the next (\d+) days", body)
    if "No available times this day" in body and m and int(m.group(1)) + 1 >= len(tiles):
        return set()
    slots = set()
    for mon, day in tiles:
        if not page.evaluate(CLICK_DATE_JS, [mon, day]):
            continue
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        if "No available times this day" in body:
            continue
        slots.update((f"{mon} {day}", t) for t in dict.fromkeys(TIME_RE.findall(body)))
    return slots


def write_opening(row):
    new_file = not OPENINGS_FILE.exists()
    with OPENINGS_FILE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OPENINGS_COLUMNS)
        if new_file:
            w.writeheader()
        w.writerow(row)


def record_openings(state, slots):
    """Compares with the last check: logs slots that just appeared and ones that got taken."""
    now = datetime.now(STORE_TZ)
    prev = {tuple(k.split("|")): v for k, v in state.get("open_slots", {}).items()}
    last_scan = state.get("slots_checked")
    gap = (now - datetime.fromisoformat(last_scan)).total_seconds() if last_scan else None
    unsure = gap is None or gap > 15 * 60   # script was off, so timings are rough

    for day, t in sorted(slots - set(prev)):
        appt = slot_date(day, now)
        log(f"OPENING: {day} {t} just appeared")
        write_opening({"event": "opened", "appointment_date": appt.isoformat(),
                       "appointment_weekday": appt.strftime("%A"), "appointment_time": t,
                       "seen_at_ny": now.strftime("%Y-%m-%d %H:%M:%S"),
                       "seen_weekday": now.strftime("%A"),
                       "days_ahead": (appt - now.date()).days, "open_for_minutes": "",
                       "note": "seen after the script was off" if unsure and prev else ""})
    for (day, t), first_seen in sorted(prev.items()):
        if (day, t) in slots:
            continue
        appt = slot_date(day, now)
        mins = (now - datetime.fromisoformat(first_seen)).total_seconds() / 60
        log(f"TAKEN: {day} {t} is gone after about {mins:.0f} min")
        write_opening({"event": "taken", "appointment_date": appt.isoformat(),
                       "appointment_weekday": appt.strftime("%A"), "appointment_time": t,
                       "seen_at_ny": now.strftime("%Y-%m-%d %H:%M:%S"),
                       "seen_weekday": now.strftime("%A"),
                       "days_ahead": (appt - now.date()).days,
                       "open_for_minutes": f"{mins:.1f}",
                       "note": "script was off for a while, so this is a rough upper bound"
                               if unsure else ""})

    state["open_slots"] = {f"{d}|{t}": prev.get((d, t), now.isoformat()) for d, t in slots}
    state["slots_checked"] = now.isoformat()
    save_state(state)


GITHUB_RAW = "https://raw.githubusercontent.com/striker818/ch-booker/main/"


def fetch_from_github(name):
    """A file from the GitHub tracker repo, or None. Uses curl because this Mac's
    Python can't verify HTTPS certificates."""
    try:
        r = subprocess.run(["curl", "-fsSL", "--max-time", "15", GITHUB_RAW + name],
                           capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def merge_github_releases(state):
    """Pulls in the release times the GitHub tracker has learned."""
    text = fetch_from_github(STATE_FILE.name)
    if not text:
        return
    try:
        theirs = real_releases(json.loads(text))
    except ValueError:
        return
    mine = state.setdefault("releases", [])
    known = {r["seen"] for r in mine}
    added = [r for r in theirs if r["seen"] not in known]
    if added:
        mine.extend(added)
        mine.sort(key=lambda r: r["seen"])
        save_state(state)
        log(f"Picked up {len(added)} release time(s) from the GitHub tracker")


def merge_openings(*sources):
    """Combines opening logs from both trackers. When both saw the same slot open (or
    get taken) within 3 minutes, keeps the earliest sighting."""
    merged = []
    for r in sorted((r for rows in sources for r in rows), key=lambda r: r["seen_at_ny"]):
        seen = datetime.strptime(r["seen_at_ny"], "%Y-%m-%d %H:%M:%S")
        if any(m["event"] == r["event"] and m["appointment_date"] == r["appointment_date"]
               and m["appointment_time"] == r["appointment_time"]
               and abs((seen - datetime.strptime(m["seen_at_ny"], "%Y-%m-%d %H:%M:%S"))
                       .total_seconds()) <= 180
               for m in merged):
            continue
        merged.append(r)
    return merged


def print_report(slug):
    """python3 ch_booker.py report - patterns in the openings seen so far."""
    from collections import Counter
    from statistics import median
    # Both trackers write their own copy (GitHub's lives in the repo), so merge them.
    gh_csv = fetch_from_github(f"ch_openings_{slug}.csv")
    local = Path(__file__).with_name(f"ch_openings_{slug}.csv")
    rows = merge_openings(list(csv.DictReader((gh_csv or "").splitlines())),
                          list(csv.DictReader(local.read_text().splitlines()))
                          if local.exists() else [])
    state = {"releases": []}
    for text in (fetch_from_github(f"ch_release_{slug}.json"),
                 Path(__file__).with_name(f"ch_release_{slug}.json").read_text()
                 if Path(__file__).with_name(f"ch_release_{slug}.json").exists() else None):
        if text:
            known = {r["seen"] for r in state["releases"]}
            state["releases"] += [r for r in real_releases(json.loads(text))
                                  if r["seen"] not in known]
    opened = [r for r in rows if r["event"] == "opened"]
    taken = [r for r in rows if r["event"] == "taken" and r["open_for_minutes"] and not r["note"]]

    print(f"\n=== Chrome Hearts {slug} - what's been seen so far ===")
    print("(GitHub tracker + this Mac combined"
          f"{'' if gh_csv is not None else ' - GitHub unreachable, Mac only'}; "
          "GitHub saves at the end of each ~5.5-hour run)\n")
    rel = real_releases(state)
    print(f"New days added to the booking window: {len(rel)} seen")
    for r in rel:
        print(f"   {datetime.fromisoformat(r['seen']).astimezone(STORE_TZ):%a %b %d}, "
              f"between {datetime.fromisoformat(r['after']).astimezone(STORE_TZ):%I:%M:%S %p} and "
              f"{datetime.fromisoformat(r['seen']).astimezone(STORE_TZ):%I:%M:%S %p} NY time")
    print(f"\nOpenings (a time slot becoming free): {len(opened)} seen")
    if not opened:
        print("   none yet")
        return

    def show(title, values, order=None):
        c = Counter(values)
        keys = order if order else [k for k, _ in c.most_common()]
        print(f"\n{title}")
        for k in keys:
            if c[k]:
                print(f"   {k:<12} {'#' * c[k]} {c[k]}")

    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    show("Which appointment DAYS opened up:", [r["appointment_weekday"] for r in opened], days)
    show("Which appointment TIMES opened up:", [r["appointment_time"] for r in opened],
         sorted({r["appointment_time"] for r in opened},
                key=lambda t: datetime.strptime(t, "%I:%M %p")))
    show("Day of the week the opening APPEARED:", [r["seen_weekday"] for r in opened], days)
    show("Hour (NY time) the opening APPEARED:",
         [datetime.strptime(r["seen_at_ny"], "%Y-%m-%d %H:%M:%S").strftime("%I %p")
          for r in opened],
         sorted({datetime.strptime(r["seen_at_ny"], "%Y-%m-%d %H:%M:%S").strftime("%I %p")
                 for r in opened}, key=lambda h: datetime.strptime(h, "%I %p")))
    ahead = [int(r["days_ahead"]) for r in opened]
    print(f"\nHow far ahead the opened slots were: {min(ahead)} to {max(ahead)} days "
          f"(typically {median(ahead):.0f})")
    if taken:
        mins = [float(r["open_for_minutes"]) for r in taken]
        print(f"How long an opening lasted before someone took it: typically "
              f"{median(mins):.1f} min (shortest {min(mins):.1f}, longest {max(mins):.1f}, "
              f"from {len(mins)} timed)")
    if len(opened) < 10:
        print("\n(Fewer than 10 openings so far - too few to call anything a pattern yet.)")
    print()


def real_releases(state):
    """Release records from when a day became bookable. Older records (no "kind") were
    logged when a greyed-out tile merely appeared, so they don't count."""
    return [r for r in state.get("releases", []) if r.get("kind") == "bookable"]


def release_seconds(state):
    """Usual release time as seconds after midnight New York time, or None if not seen yet."""
    secs = []
    for r in real_releases(state)[-7:]:
        t = datetime.fromisoformat(r["seen"]).astimezone(STORE_TZ)
        secs.append(t.hour * 3600 + t.minute * 60 + t.second)
    return sorted(secs)[len(secs) // 2] if secs else None


def wait_period(state, normal_every=CHECK_EVERY_SECONDS):
    """Seconds between checks right now: FAST_EVERY_SECONDS near the release time."""
    rel = release_seconds(state)
    if rel is not None:
        now = datetime.now(STORE_TZ)
        now_s = now.hour * 3600 + now.minute * 60 + now.second
        diff = (now_s - rel + 43200) % 86400 - 43200   # seconds past release, -12h..+12h
        if -FAST_BEFORE_MIN * 60 <= diff <= FAST_AFTER_MIN * 60:
            return FAST_EVERY_SECONDS
    return normal_every


def next_wait(state, normal_every):
    """Seconds to sleep until the next check slot on the clock. Slots are every
    normal_every seconds (FAST_EVERY_SECONDS near the release time), shifted by half
    a slot with --stagger. A check that runs long just takes the next free slot."""
    period = wait_period(state, normal_every)
    phase = period / 2 if STAGGER else 0
    now = time.time()
    next_slot = math.floor((now - phase) / period) * period + phase + period
    return next_slot - now


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
    if mode == "report":
        print_report("chromehearts")
        return
    if mode not in MODES:
        print("Start it with one of these:\n"
              "  python3 ch_booker.py test   (safe test on Aspen, books nothing)\n"
              "  python3 ch_booker.py aspen  (REAL booking, Aspen Oct 26)\n"
              "  python3 ch_booker.py nov6   (REAL booking, NYC West Village, first open day)\n"
              "  python3 ch_booker.py learn  (watch only, learns when new days open, never books)\n"
              "  python3 ch_booker.py report (patterns in the openings learn mode has seen)\n"
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
    global STATE_FILE, OPENINGS_FILE
    slug = cfg['url'].rstrip('/').split('/')[-1]
    STATE_FILE = Path(__file__).with_name(f"ch_release_{slug}.json")
    OPENINGS_FILE = Path(__file__).with_name(f"ch_openings_{slug}.csv")
    state = load_state()
    if not SERVER:
        merge_github_releases(state)
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
                    if tiles:
                        slots = scan_open_slots(page, tiles)
                        record_openings(state, slots)
                        log(f"Bookable through {tiles[-1][0]} {tiles[-1][1]} | open now: "
                            + (", ".join(f"{d} {t}" for d, t in sorted(slots)) or "nothing"))
                    else:
                        log("couldn't read the date tiles")
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
            fast = wait_period(state) == FAST_EVERY_SECONDS
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
