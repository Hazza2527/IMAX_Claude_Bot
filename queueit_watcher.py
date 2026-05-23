#!/usr/bin/env python3
"""
IMAX Melbourne — Queue-it Watcher
Hits the direct film booking pages every run and checks for Queue-it activation.
Queue-it appearing means a sale is live or imminent — alert immediately.
State is saved to queueit_state.json and committed back by the workflow.
"""

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

STATE_FILE = Path("queueit_state.json")

# Direct film booking pages on the IMAX Melbourne ticketing system
FILM_PAGES = [
    {
        "name": "The Odyssey (IMAX 70mm)",
        "url":  "https://web.imaxmelbourne.com.au/films/HO00000547",
    },
    {
        "name": "Dune: Part Three (IMAX 70mm)",
        "url":  "https://web.imaxmelbourne.com.au/films/HO00000610",
    },
]

# Strings that indicate Queue-it is active in the page or redirect URL
QUEUEIT_INDICATORS = [
    "queue-it.net",
    "queueit",
    "queue-fair",
    "waiting room",
    "you are in the queue",
    "waitingroom",
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

def fetch_page(url: str) -> tuple[str, str] | tuple[None, None]:
    """
    Fetch a URL, following redirects.
    Returns (final_url, html) or (None, None) on failure.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
    }
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        r.raise_for_status()
        return r.url, r.text
    except requests.RequestException as e:
        log.warning("Fetch failed for %s: %s", url, e)
        return None, None


def queueit_detected(final_url: str, html: str) -> tuple[bool, str]:
    """
    Check for Queue-it indicators in the final URL or page content.
    Returns (detected: bool, reason: str).
    """
    combined = (final_url + " " + html).lower()
    for indicator in QUEUEIT_INDICATORS:
        if indicator.lower() in combined:
            return True, indicator
    return False, ""


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
# MAIN
# ─────────────────────────────────────────────

def main():
    state = load_state()
    state_changed = False

    for film in FILM_PAGES:
        name = film["name"]
        url  = film["url"]

        final_url, html = fetch_page(url)
        if html is None:
            log.warning("[%s] Could not fetch page — skipping.", name)
            continue

        detected, reason = queueit_detected(final_url, html)
        prev_detected = state.get(name, {}).get("queueit_active", False)

        log.info(
            "[%s] Queue-it active: %s | Final URL: %s",
            name, detected, final_url[:80],
        )

        if detected and not prev_detected:
            # Queue-it just activated — get in NOW
            send_telegram(
                f"🚨 <b>QUEUE-IT DETECTED — Act now!</b>\n\n"
                f"<b>{name}</b>\n"
                f"Queue-it has just activated on the booking page — "
                f"a sale is live or about to go live.\n\n"
                f"🔗 <a href=\"{url}\">Open booking page</a>\n\n"
                f"<i>Get in the queue immediately!</i>"
            )

        elif not detected and prev_detected:
            # Queue-it just deactivated — sale window may have closed
            send_telegram(
                f"ℹ️ <b>Queue-it deactivated</b>\n\n"
                f"<b>{name}</b>\n"
                f"The Queue-it waiting room is no longer active on the booking page. "
                f"The sale window may have closed or sold out.\n\n"
                f"🔗 <a href=\"{url}\">Check booking page</a>"
            )

        new_entry = {"queueit_active": detected}
        if new_entry != state.get(name):
            state[name] = new_entry
            state_changed = True

    if state_changed:
        save_state(state)
        log.info("State updated.")
    else:
        log.info("No state changes.")


if __name__ == "__main__":
    main()
