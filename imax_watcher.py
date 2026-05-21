#!/usr/bin/env python3
"""
IMAX Melbourne Ticket Watcher — GitHub Actions version
Checks the coming soon page every run and sends a Telegram alert when
tickets become available (or when page status changes) for target films.
State is persisted in state.json, which the workflow commits back to the repo.
"""

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

COMING_SOON_URL = "https://imaxmelbourne.com.au/coming_soon"
STATE_FILE      = Path("state.json")

TARGET_FILMS = [
    {
        "name":     "Dune: Part Three (IMAX 70mm)",
        "keywords": ["dune", "three"],
    },
    {
        "name":     "The Odyssey (IMAX 70mm)",
        "keywords": ["odyssey"],
    },
]

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def fetch_page(url: str) -> str | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        log.warning("Fetch failed: %s", e)
        return None


def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     message,
            "parse_mode":               "HTML",
            "disable_web_page_preview": False,
        }, timeout=10)
        r.raise_for_status()
        log.info("Telegram message sent.")
        return True
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)
        return False


def text_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ─────────────────────────────────────────────
# SCRAPING
# ─────────────────────────────────────────────

def heading_matches(heading: str, keywords: list) -> bool:
    h = heading.lower()
    return all(kw.lower() in h for kw in keywords)


def parse_coming_soon(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for link in soup.select("h2 a[href]"):
        heading = link.get_text(strip=True)
        container = link.find_parent(["section", "article", "div"]) or link.parent.parent
        section_text = container.get_text(separator=" ", strip=True)

        ticket_tag = container.find("a", string=lambda s: s and "sessions" in s.lower())
        tickets_url = ticket_tag["href"] if ticket_tag and ticket_tag.get("href") else ""

        opens = ""
        for node in container.stripped_strings:
            if node.lower().startswith("opens:"):
                opens = node.replace("Opens:", "").replace("opens:", "").strip()
                break

        results.append({
            "heading":     heading,
            "opens":       opens,
            "section_text": section_text,
            "tickets_url": tickets_url,
            "sold_out":    "sold out" in section_text.lower(),
            "on_sale":     "tickets on sale" in section_text.lower(),
        })

    return results

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def main():
    html = fetch_page(COMING_SOON_URL)
    if not html:
        log.error("Could not fetch page — skipping this run.")
        sys.exit(0)   # exit 0 so the workflow doesn't show as failed

    all_movies = parse_coming_soon(html)
    state = load_state()
    state_changed = False

    for target in TARGET_FILMS:
        key = target["name"]
        match = next(
            (m for m in all_movies if heading_matches(m["heading"], target["keywords"])),
            None,
        )

        if not match:
            log.info("[%s] Not found on page yet.", key)
            continue

        current_hash = text_hash(match["section_text"])
        prev         = state.get(key, {})
        prev_hash    = prev.get("hash", "")
        first_seen   = not prev_hash

        log.info(
            "[%s] Opens: %s | On sale: %s | Sold out: %s | Hash: %s",
            key, match["opens"] or "TBC", match["on_sale"], match["sold_out"],
            current_hash[:8],
        )

        # ── Alert logic ──────────────────────────────────────────────────
        alert = None

        tickets_now_available = (
            match["on_sale"]
            and not match["sold_out"]
            and bool(match["tickets_url"])
        )

        if tickets_now_available and not prev.get("alerted_available"):
            alert = "🎟️ <b>Tickets are available NOW!</b>"

        elif not first_seen and current_hash != prev_hash:
            # Something changed — might be a new batch, new wording, etc.
            alert = "⚠️ Page status has changed — check for new tickets."

        if alert:
            ticket_line = (
                f'\n🔗 <a href="{match["tickets_url"]}">Book tickets</a>'
                if match["tickets_url"]
                else f'\n🔗 <a href="{COMING_SOON_URL}">IMAX Melbourne coming soon</a>'
            )
            send_telegram(
                f"🎬 <b>IMAX Melbourne</b>\n\n"
                f"<b>{match['heading']}</b>\n"
                f"📅 Opens: {match['opens'] or 'TBC'}\n\n"
                f"{alert}"
                f"{ticket_line}"
            )

        # ── Update state ─────────────────────────────────────────────────
        new_entry = {
            "hash":              current_hash,
            "sold_out":          match["sold_out"],
            "on_sale":           match["on_sale"],
            "tickets_url":       match["tickets_url"],
            "opens":             match["opens"],
            "alerted_available": tickets_now_available or prev.get("alerted_available", False),
        }

        if new_entry != prev:
            state[key] = new_entry
            state_changed = True

    if state_changed:
        save_state(state)
        log.info("State updated.")
    else:
        log.info("No state changes.")


if __name__ == "__main__":
    main()