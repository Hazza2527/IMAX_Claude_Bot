#!/usr/bin/env python3
"""
IMAX Melbourne ticket watcher.

Watches imaxmelbourne.com.au for the moment DUNE: PART THREE either
(a) gets an on-sale date announced, or (b) has tickets actually go live.

The signal we care about most is structural, not textual: when tickets are
bookable, IMAX adds a link to their ticketing system —

    coming soon listing:  <span class="btn-sessions"><a href=".../films/HO00000626">
    film page submenu:    <li><a href=".../films/HO00000626">Session Times &amp; Tickets</a>

That link is currently absent for Dune and present for every on-sale film, so
its appearance is an unambiguous "tickets are bookable now" trigger.

On top of that we diff the page text, so any wording change at all (a new
on-sale date, a new presale batch, a reworded sold-out notice) still reaches
you even if the structural signal never fires. The date/banner detection below
only decides how loudly to shout — the text diff is the actual safety net.

Run modes:
    python watcher.py                 one check, no alerts sent (add --send)
    python watcher.py --send          one check, alerts sent
    python watcher.py --send --minutes 340 --commit    CI polling loop
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo

    MELBOURNE = ZoneInfo("Australia/Melbourne")
except Exception:  # pragma: no cover - no tzdata available
    MELBOURNE = timezone(timedelta(hours=10))

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

SITE = "https://imaxmelbourne.com.au"
STATE_FILE = Path("state.json")

# Add another dict here to watch a second film. "match" is matched against the
# listing headings on the coming soon / now showing pages (all words must appear).
FILMS = [
    {
        "key": "dune-part-three",
        "name": "DUNE: PART THREE",
        "film_page": f"{SITE}/movie/dune-part-three",
        "match": ["dune"],
    },
]

LISTING_PAGES = {
    "coming soon page": f"{SITE}/coming_soon",
    "now showing page": f"{SITE}/now_showing",
}

# Links into the ticketing system. Their presence == tickets are bookable.
BOOKING_RE = re.compile(r"https?://web\.imaxmelbourne\.com\.au/films/[A-Za-z0-9]+")

# imaxmelbourne.com.au sits behind Queue-it (customer "museumsvictoria", event
# "imaxtickets"). A fraction of requests get the waiting room served *in place
# of* the real page. That is not a page change — diffing against it produces an
# endless alternating alert — so a queued response is treated as "no reading
# taken", exactly like a failed fetch.
QUEUEIT_RE = re.compile(
    r"queue-it\.net|data-queueit-tag-eventid|<title>\s*Queue-it\s*</title>", re.I
)
QUEUE_USERS_RE = re.compile(r'"usersInLineAheadOfYou":\s*(null|\d+)')
QUEUE_WAIT_RE = re.compile(r'"whichIsIn":\s*"([^"]*)"')
QUEUE_PAUSED_RE = re.compile(r'"queuePaused":\s*(true|false)')

# Don't re-announce a merely-armed queue more than this often.
QUEUE_QUIET_HOURS = float(os.environ.get("QUEUE_QUIET_HOURS", "6"))
# A queue with people actually in it is a real rush — alert harder, and sooner.
QUEUE_BUSY_QUIET_MINUTES = float(os.environ.get("QUEUE_BUSY_QUIET_MINUTES", "20"))

# "on sale" style cue followed closely by something that looks like a date/time.
# "may" is deliberately not in MONTHS — as a word it is far too ambiguous.
_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_WEEKDAYS = (
    r"mon(?:day)?|tues?(?:day)?|wed(?:nesday)?|thur?s?(?:day)?"
    r"|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
)
_SALE_CUE = (
    r"on sale|sale begins|sale starts|goes on sale|tickets? available"
    r"|available from|releas\w+\s+on|book(?:ing)?s? open"
)
_DATE_TOKEN = (
    rf"\b(?:{_WEEKDAYS})\b"
    rf"|\b(?:{_MONTHS})\b\s*\.?\s*\d{{1,2}}\b"
    rf"|\b\d{{1,2}}\s*(?:st|nd|rd|th)?\s+(?:{_MONTHS})\b"
    rf"|\b\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm)\b"
    rf"|\b\d{{1,2}}/\d{{1,2}}\b"
)
ON_SALE_DATE_RE = re.compile(
    rf"(?:{_SALE_CUE}).{{0,80}}?(?:{_DATE_TOKEN})", re.I | re.S
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Poll faster while Melbourne is awake — announcements come in business hours.
DAY_INTERVAL = int(os.environ.get("DAY_INTERVAL", "45"))
NIGHT_INTERVAL = int(os.environ.get("NIGHT_INTERVAL", "180"))
DAY_START_HOUR, DAY_END_HOUR = 7, 23

# Reassurance ping so silence always means "working", never "broken".
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "24"))

# Consecutive failed cycles before we warn that the watcher has gone blind.
FAILURE_ALERT_THRESHOLD = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watcher")


# ─────────────────────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────


def send_telegram(text: str, *, send: bool) -> bool:
    """Send a message. Returns True if delivered (or if sending is disabled)."""
    if not send:
        log.info("[dry run] would send:\n%s\n", re.sub(r"<[^>]+>", "", text))
        return True

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text[:4000],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            log.info("Telegram message sent.")
            return True
        except requests.RequestException as exc:
            log.warning("Telegram send attempt %d failed: %s", attempt, exc)
            time.sleep(2 * attempt)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────────────────────────────────────


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def fetch(url: str) -> str | None:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-AU,en;q=0.9"},
            timeout=20,
        )
        r.raise_for_status()
        return r.text
    except requests.RequestException as exc:
        log.warning("Fetch failed for %s: %s", url, exc)
        return None


def parse_queue_page(page_html: str) -> dict:
    """Pull the queue's own status out of a Queue-it waiting room page."""
    users = QUEUE_USERS_RE.search(page_html)
    wait = QUEUE_WAIT_RE.search(page_html)
    paused = QUEUE_PAUSED_RE.search(page_html)

    ahead = None
    if users and users.group(1) != "null":
        ahead = int(users.group(1))

    return {
        "users_ahead": ahead,
        "wait": wait.group(1) if wait else "",
        "paused": paused.group(1) == "true" if paused else False,
        # An empty queue is the site simply being armed; people in front of you
        # means a sale is actually being rushed right now.
        "busy": bool(ahead),
    }


def _strip(fragment, selectors: tuple[str, ...]):
    """Return a copy of `fragment` with the given selectors removed."""
    clone = BeautifulSoup(str(fragment), "html.parser")
    for selector in selectors:
        for el in clone.select(selector):
            el.decompose()
    return clone


def parse_listing_page(page_html: str) -> dict[str, dict]:
    """Snapshot every film block on a coming soon / now showing page."""
    soup = BeautifulSoup(page_html, "html.parser")
    listings: dict[str, dict] = {}

    for block in soup.select("div.film-listing"):
        title_el = block.select_one("h2.film-title") or block.select_one("h2")
        if not title_el:
            continue
        title = norm(title_el.get_text())
        text_col = block.select_one("div.film-text") or block

        banner_el = text_col.select_one("p.opening-date span.red-text")
        banner = norm(banner_el.get_text()) if banner_el else ""

        opens = ""
        opens_el = text_col.select_one("p.opening-date")
        if opens_el:
            opens = norm(_strip(opens_el, ("span.red-text",)).get_text())
            opens = opens.removeprefix("Opens:").strip()

        sessions_el = text_col.select_one("span.btn-sessions a[href]")
        sessions_url = sessions_el["href"].strip() if sessions_el else ""
        if not sessions_url:
            found = BOOKING_RE.search(str(block))
            sessions_url = found.group(0) if found else ""

        link_el = text_col.select_one("h2.film-title a[href]") or block.select_one(
            'a[href^="/movie/"]'
        )
        movie_url = urljoin(SITE, link_el["href"]) if link_el else ""

        body = norm(
            _strip(
                text_col, ("p.opening-date", "div.margintop-20", "h2", "script", "style")
            ).get_text(" ")
        )

        listings[title] = {
            "present": True,
            "title": title,
            "movie_url": movie_url,
            "opens": opens,
            "banner": banner,
            "sessions_url": sessions_url,
            "body": body,
        }

    return listings


def parse_film_page(page_html: str) -> dict:
    """Snapshot an individual /movie/<slug> page."""
    soup = BeautifulSoup(page_html, "html.parser")

    # Scope to the submenu first. Only fall back to a page-wide search if the
    # submenu is missing entirely, so an unrelated carousel can't fake a hit.
    sessions_url = ""
    submenu = soup.select_one("nav.submenu")
    if submenu is not None:
        found = BOOKING_RE.search(str(submenu))
        sessions_url = found.group(0) if found else ""
    else:
        log.warning("Film page has no nav.submenu — falling back to page-wide scan.")
        found = BOOKING_RE.search(page_html)
        sessions_url = found.group(0) if found else ""

    opendate_el = soup.select_one("div.opendate")
    opens = norm(_strip(opendate_el, ("span.red-text",)).get_text()) if opendate_el else ""
    opens = opens.removeprefix("Opens:").strip()

    banner_el = soup.select_one("div.opendate span.red-text")
    banner = norm(banner_el.get_text()) if banner_el else ""

    column = soup.select_one("div.right-column") or soup.select_one("#content")
    body = (
        norm(_strip(column, ("nav.submenu", "script", "style")).get_text(" "))
        if column
        else ""
    )

    return {
        "present": bool(body or sessions_url),
        "opens": opens,
        "banner": banner,
        "sessions_url": sessions_url,
        "body": body,
    }


# ─────────────────────────────────────────────────────────────────────────────
# DIFFING / FORMATTING
# ─────────────────────────────────────────────────────────────────────────────


def diff_summary(old: str, new: str, limit: int = 700) -> str:
    """A compact word-level before/after of two blocks of text."""
    old_words, new_words = old.split(), new.split()
    removed, added = [], []
    matcher = difflib.SequenceMatcher(None, old_words, new_words, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed.append(" ".join(old_words[i1:i2]))
        if tag in ("replace", "insert"):
            added.append(" ".join(new_words[j1:j2]))

    parts = []
    if removed:
        parts.append("➖ <b>Removed:</b> " + html.escape(" … ".join(removed))[:limit])
    if added:
        parts.append("➕ <b>Added:</b> " + html.escape(" … ".join(added))[:limit])
    return "\n\n".join(parts) or "(no visible text difference)"


def has_on_sale_date(snapshot: dict) -> bool:
    blob = f"{snapshot.get('banner', '')} {snapshot.get('body', '')}"
    return bool(ON_SALE_DATE_RE.search(blob))


# ─────────────────────────────────────────────────────────────────────────────
# CHECK LOGIC
# ─────────────────────────────────────────────────────────────────────────────


def collect(film: dict) -> tuple[dict, dict, bool, dict | None]:
    """
    Fetch every surface for a film.

    Returns (snapshots, matching_listings, ok, queue). `ok` is False if any page
    could not be read — either a failed fetch or a Queue-it waiting room served
    in place of the real page. Either way the previous state is left untouched,
    so an outage or a queue is never mistaken for a change.
    """
    snapshots: dict[str, dict] = {}
    matching_listings: dict[str, dict] = {}
    ok = True
    queue: dict | None = None

    def read(url: str) -> str | None:
        """Fetch a page, retrying past the queue — only some requests get it."""
        nonlocal ok, queue
        for attempt in range(3):
            page = fetch(url)
            if page is None:
                ok = False
                return None
            if not QUEUEIT_RE.search(page):
                return page
            queue = parse_queue_page(page)
            log.info(
                "Queue-it waiting room for %s (attempt %d/3, %s)",
                url,
                attempt + 1,
                f"{queue['users_ahead']} ahead" if queue["busy"] else "queue empty",
            )
            time.sleep(2)
        ok = False
        return None

    for surface, url in LISTING_PAGES.items():
        page = read(url)
        if page is None:
            continue
        listings = parse_listing_page(page)
        matches = {
            title: snap
            for title, snap in listings.items()
            if all(word.lower() in title.lower() for word in film["match"])
        }
        matching_listings.update(matches)
        # The primary listing is the first match on this surface. Any *other*
        # matching listing is still tracked for booking links further down.
        snapshots[surface] = next(iter(matches.values()), {"present": False})

    page = read(film["film_page"])
    if page is not None:
        snapshots["film page"] = parse_film_page(page)

    return snapshots, matching_listings, ok, queue


# Placeholder for "which page(s) this happened on", filled in after identical
# events from different surfaces have been merged together.
WHERE = "«WHERE»"


def evaluate(film: dict, snapshots: dict, listings: dict, prev: dict) -> list[tuple]:
    """
    Compare fresh snapshots against stored ones.

    Returns (priority, signature, surface, template) tuples. The same real-world
    change usually shows up on several pages at once, so identical events are
    merged by signature before anything is sent.
    """
    events: list[tuple[int, str, str, str]] = []
    prev_surfaces = prev.get("surfaces", {})
    name = html.escape(film["name"])

    for surface, now in snapshots.items():
        before = prev_surfaces.get(surface)
        if before is None:
            continue  # first sighting — baseline is recorded, nothing to compare

        if not now.get("present"):
            if before.get("present"):
                events.append(
                    (
                        3,
                        "missing",
                        surface,
                        f"👻 <b>{name} disappeared from the {WHERE}</b>\n\n"
                        f"That usually means the listing moved or was rebuilt — "
                        f"worth a look, and check the watcher still matches it.",
                    )
                )
            continue

        if not before.get("present"):
            events.append(
                (
                    1,
                    "appeared",
                    surface,
                    f"✨ <b>{name} appeared on the {WHERE}</b>\n\n"
                    f"{html.escape(now.get('body', ''))[:600]}",
                )
            )

        booking = now.get("sessions_url", "")
        if booking and booking != before.get("sessions_url", ""):
            events.append(
                (
                    0,
                    f"booking:{booking}",
                    surface,
                    f"🚨🎟️ <b>TICKETS ARE LIVE — {name}</b>\n\n"
                    f"A booking link just appeared on the {WHERE}.\n\n"
                    f'👉 <a href="{html.escape(booking)}">BOOK NOW</a>\n'
                    f"<code>{html.escape(booking)}</code>\n\n"
                    f"<i>Open it immediately — join any queue before you do "
                    f"anything else.</i>",
                )
            )

        was_banner, now_banner = before.get("banner", ""), now.get("banner", "")
        if now_banner != was_banner:
            events.append(
                (
                    1,
                    f"banner:{was_banner}>{now_banner}",
                    surface,
                    f"📣 <b>{name} — status banner changed</b> ({WHERE})\n\n"
                    f"Was: {html.escape(was_banner or '(none)')}\n"
                    f"Now: <b>{html.escape(now_banner or '(cleared)')}</b>",
                )
            )

        body_diff = diff_summary(before.get("body", ""), now.get("body", ""))

        if has_on_sale_date(now) and not has_on_sale_date(before):
            events.append(
                (
                    1,
                    f"onsale:{body_diff}",
                    surface,
                    f"📅 <b>{name} — an on-sale date looks like it was just "
                    f"announced</b> ({WHERE})\n\n{body_diff}",
                )
            )

        was_opens, now_opens = before.get("opens", ""), now.get("opens", "")
        if now_opens != was_opens:
            events.append(
                (
                    2,
                    f"opens:{was_opens}>{now_opens}",
                    surface,
                    f"📆 <b>{name} — opening date changed</b> ({WHERE})\n\n"
                    f"Was: {html.escape(was_opens or '(none)')}\n"
                    f"Now: <b>{html.escape(now_opens or '(none)')}</b>",
                )
            )

        if now.get("body", "") != before.get("body", ""):
            events.append(
                (
                    4,
                    f"text:{body_diff}",
                    surface,
                    f"⚠️ <b>{name} — page text changed</b> ({WHERE})\n\n{body_diff}",
                )
            )

    # Booking links across *every* matching listing, not just the primary one.
    # If a separate "DUNE: PART THREE - IMAX LASER" entry shows up with its own
    # tickets while the 70mm listing stays sold out, this is what catches it.
    # Shares the booking:<url> signature, so it merges with the per-surface
    # check above rather than double-alerting.
    prev_bookings = prev.get("listing_bookings", {})
    for title, snap in listings.items():
        url = snap.get("sessions_url", "")
        if url and url != prev_bookings.get(title, ""):
            events.append(
                (
                    0,
                    f"booking:{url}",
                    "listings",
                    f"🚨🎟️ <b>TICKETS ARE LIVE — {html.escape(title)}</b>\n\n"
                    f"A booking link just appeared ({WHERE}).\n\n"
                    f'👉 <a href="{html.escape(url)}">BOOK NOW</a>\n'
                    f"<code>{html.escape(url)}</code>\n\n"
                    f"<i>Open it immediately — join any queue before you do "
                    f"anything else.</i>",
                )
            )

    # A brand new separate listing, e.g. a "DUNE: PART THREE - IMAX LASER" entry
    # appearing alongside the 70mm one.
    known = set(prev.get("listings", []))
    if known:
        for title in listings:
            if title not in known:
                events.append(
                    (
                        1,
                        f"newlisting:{title}",
                        "listings",
                        f"🆕 <b>New listing matching “{html.escape(film['match'][0])}”</b>"
                        f"\n\n<b>{html.escape(title)}</b>\n"
                        f"A separate entry just appeared — possibly a new format "
                        f"or session batch.",
                    )
                )

    return events


def compose(events: list[tuple]) -> str | None:
    """
    Merge duplicate events across pages and render them as ONE message.

    A single real change (tickets going live) shows up on both the coming soon
    listing and the film page. Sending one message per surface per signal turns
    one event into a burst of near-identical notifications, which is exactly
    what you don't want when you need to act on the first one.
    """
    merged: dict[str, dict] = {}
    for priority, signature, surface, template in events:
        entry = merged.setdefault(
            signature, {"priority": priority, "template": template, "surfaces": []}
        )
        if surface not in entry["surfaces"]:
            entry["surfaces"].append(surface)

    if not merged:
        return None

    blocks = []
    for entry in sorted(merged.values(), key=lambda e: e["priority"]):
        where = html.escape(" and ".join(entry["surfaces"]))
        blocks.append(entry["template"].replace(WHERE, where))

    return ("\n\n" + "─" * 18 + "\n\n").join(blocks)


def status_line(snapshots: dict) -> str:
    """One-line human summary used in baseline and heartbeat messages."""
    bits = []
    for surface, snap in snapshots.items():
        if not snap.get("present"):
            bits.append(f"{surface}: not listed")
            continue
        booking = "BOOKING LINK PRESENT" if snap.get("sessions_url") else "no booking link"
        banner = snap.get("banner") or "no banner"
        bits.append(f"{surface}: {booking}, {banner}")
    return "\n".join(f"• {html.escape(b)}" for b in bits)


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read state file (%s) — starting fresh.", exc)
    return {"version": 3, "films": {}}


def save_state(state: dict) -> None:
    state["last_check"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def git_persist() -> None:
    """Commit the state file so it survives between CI runs. Never fatal."""
    try:
        subprocess.run(["git", "add", str(STATE_FILE)], check=True, capture_output=True)
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if staged.returncode == 0:
            return
        subprocess.run(
            ["git", "commit", "-m", "chore: update watcher state"],
            check=True,
            capture_output=True,
        )
        for attempt in range(3):
            pushed = subprocess.run(["git", "push"], capture_output=True, text=True)
            if pushed.returncode == 0:
                return
            log.warning("Push attempt %d failed, rebasing…", attempt + 1)
            subprocess.run(["git", "pull", "--rebase"], capture_output=True)
        log.warning("Could not push state after 3 attempts.")
    except Exception as exc:  # noqa: BLE001 - persistence must never kill the loop
        log.warning("State commit failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────


def handle_queue(state: dict, queue: dict, *, send: bool) -> bool:
    """
    Report the Queue-it waiting room. Returns True if state changed.

    An armed-but-empty queue is the site on standby and only worth mentioning
    occasionally. A queue with people in it means a sale is actually being
    rushed, which is the single most urgent thing this watcher can tell you.
    """
    now = datetime.now(timezone.utc)
    record = state.setdefault("queue", {})
    changed = False

    quiet = (
        timedelta(minutes=QUEUE_BUSY_QUIET_MINUTES)
        if queue["busy"]
        else timedelta(hours=QUEUE_QUIET_HOURS)
    )
    due = True
    if record.get("last_alert"):
        try:
            due = now - datetime.fromisoformat(record["last_alert"]) >= quiet
        except ValueError:
            due = True
    # An empty queue filling up is always worth saying immediately.
    if queue["busy"] and not record.get("was_busy"):
        due = True

    if due:
        if queue["busy"]:
            waiting = f" ({html.escape(queue['wait'])})" if queue["wait"] else ""
            message = (
                f"🚨 <b>QUEUE IS FILLING — a sale is probably live</b>\n\n"
                f"Queue-it reports <b>{queue['users_ahead']}</b> people ahead of "
                f"you in line{waiting}.\n\n"
                f"The queue only fills during a real rush. Get in it now — your "
                f"position depends on when you join, not when you read this.\n\n"
                f'👉 <a href="{FILMS[0]["film_page"]}">Open IMAX Melbourne</a>'
            )
        else:
            message = (
                f"🧍 <b>Queue-it is armed on imaxmelbourne.com.au</b>\n\n"
                f"The waiting room is being served on some requests, but the "
                f"queue is empty and passing straight through — so this is the "
                f"site on standby, not a sale in progress.\n\n"
                f"They don't usually arm it for nothing. Worth being ready.\n\n"
                f'👉 <a href="{FILMS[0]["film_page"]}">Open IMAX Melbourne</a>'
            )
        if send_telegram(message, send=send):
            record["last_alert"] = now.isoformat(timespec="seconds")
            changed = True

    if record.get("was_busy") != queue["busy"]:
        record["was_busy"] = queue["busy"]
        changed = True

    return changed


def check_cycle(state: dict, *, send: bool) -> bool:
    """One pass over every film. Returns True if state changed and was saved."""
    dirty = False

    for film in FILMS:
        key = film["key"]
        prev = state.setdefault("films", {}).setdefault(key, {})
        snapshots, listings, ok, queue = collect(film)

        if queue is not None and handle_queue(state, queue, send=send):
            dirty = True

        if not ok:
            if queue is not None:
                # Being put in the queue is not a failure — the site is up, we
                # just didn't get a clean reading. Skip without touching the
                # snapshot or the blindness counter.
                log.info("[%s] skipped — queued on every attempt.", key)
                continue
            # An outage must never be mistaken for a change, so leave the stored
            # snapshot untouched and only record that the fetch failed.
            failures = prev.get("consecutive_failures", 0) + 1
            prev["consecutive_failures"] = failures
            log.warning("[%s] incomplete fetch (%d in a row)", key, failures)
            if failures == FAILURE_ALERT_THRESHOLD:
                send_telegram(
                    f"🔌 <b>Watcher can't reach imaxmelbourne.com.au</b>\n\n"
                    f"{failures} checks in a row have failed. The site may be down "
                    f"or blocking the watcher — worth checking manually.",
                    send=send,
                )
            dirty = True
            continue

        prev["consecutive_failures"] = 0
        first_run = "surfaces" not in prev

        for surface, snap in snapshots.items():
            marker = "TICKETS" if snap.get("sessions_url") else "—"
            log.info(
                "[%s] %-12s present=%-5s booking=%-8s banner=%r",
                key,
                surface,
                snap.get("present", False),
                marker,
                snap.get("banner", ""),
            )

        if first_run:
            message = (
                f"👀 <b>Watching {html.escape(film['name'])}</b>\n\n"
                f"{status_line(snapshots)}\n\n"
                f"You'll get a message the moment a booking link appears or the "
                f"wording changes."
            )
        else:
            message = compose(evaluate(film, snapshots, listings, prev))

        if message:
            footer = f'\n\n🔗 <a href="{film["film_page"]}">Film page</a>'
            if not send_telegram(message + footer, send=send):
                # Leave the old snapshot in place so the next cycle retries the
                # alert rather than silently swallowing it.
                log.error("[%s] alert delivery failed — not advancing state.", key)
                continue

        # Only mark the state dirty on a real difference — otherwise the CI loop
        # would commit an identical state file every single cycle.
        seen_listings = sorted(listings)
        seen_bookings = {
            title: snap.get("sessions_url", "")
            for title, snap in listings.items()
            if snap.get("sessions_url")
        }
        if (
            prev.get("surfaces") != snapshots
            or prev.get("listings") != seen_listings
            or prev.get("listing_bookings") != seen_bookings
        ):
            prev["surfaces"] = snapshots
            prev["listings"] = seen_listings
            prev["listing_bookings"] = seen_bookings
            dirty = True

    # Heartbeat, so a long silence always means "nothing has changed".
    if HEARTBEAT_HOURS > 0:
        last = state.get("last_heartbeat")
        due = True
        if last:
            try:
                due = datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(
                    hours=HEARTBEAT_HOURS
                )
            except ValueError:
                due = True
        if due and not state.get("_first_ever", True):
            summaries = []
            for film in FILMS:
                snaps = state["films"].get(film["key"], {}).get("surfaces", {})
                summaries.append(f"<b>{html.escape(film['name'])}</b>\n{status_line(snaps)}")
            if send_telegram(
                "💤 <b>Still watching — nothing has changed.</b>\n\n"
                + "\n\n".join(summaries),
                send=send,
            ):
                state["last_heartbeat"] = datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                dirty = True
        elif due:
            state["last_heartbeat"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            dirty = True

    state["_first_ever"] = False

    if dirty:
        save_state(state)
    return dirty


def poll_interval() -> int:
    hour = datetime.now(MELBOURNE).hour
    return DAY_INTERVAL if DAY_START_HOUR <= hour <= DAY_END_HOUR else NIGHT_INTERVAL


def main() -> int:
    parser = argparse.ArgumentParser(description="IMAX Melbourne ticket watcher")
    parser.add_argument(
        "--send", action="store_true", help="actually send Telegram messages"
    )
    parser.add_argument(
        "--minutes",
        type=float,
        default=0,
        help="keep polling for this many minutes (0 = single check)",
    )
    parser.add_argument(
        "--commit", action="store_true", help="git commit state.json when it changes"
    )
    args = parser.parse_args()

    state = load_state()
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None

    while True:
        started = time.monotonic()
        try:
            if check_cycle(state, send=args.send) and args.commit:
                git_persist()
        except Exception as exc:  # noqa: BLE001 - a bad cycle must not end the loop
            log.exception("Check cycle failed: %s", exc)

        if deadline is None:
            return 0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.info("Poll window finished.")
            return 0

        wait = poll_interval() - (time.monotonic() - started)
        time.sleep(max(1.0, min(wait, remaining)))


if __name__ == "__main__":
    sys.exit(main())
