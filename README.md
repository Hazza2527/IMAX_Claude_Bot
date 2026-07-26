# IMAX Melbourne ticket watcher

Telegram alerts the moment **DUNE: PART THREE** gets an on-sale date or tickets
actually go live at IMAX Melbourne.

## How it decides something happened

When a film is bookable, IMAX adds a link to their ticketing system — a
`Sessions & Tickets` button on the [coming soon](https://imaxmelbourne.com.au/coming_soon)
listing, and a `Session Times & Tickets` item in the film page submenu:

```html
<span class="btn-sessions"><a href="https://web.imaxmelbourne.com.au/films/HO00000626">
```

Dune has no such link right now; every on-sale film does. So the link appearing
is an unambiguous **tickets are bookable** trigger, and it is checked on both
the coming soon page and the film page.

Everything else is a safety net layered on top:

| Trigger | Meaning |
| --- | --- |
| 🚨 Booking link appears | Tickets are live — includes the direct booking URL |
| 📅 On-sale cue near a date | A release date looks like it was just announced |
| 📣 Red status banner changes | e.g. blank → `TICKETS ON SALE NOW!` |
| 🆕 New matching listing | A separate entry appears, e.g. an IMAX Laser version |
| ⚠️ Page text changes | Anything else at all, with a before/after diff |
| 🔌 Site unreachable | 20 failed checks in a row — the watcher has gone blind |
| 💤 Daily heartbeat | Proof it is still alive, so silence always means "no news" |

The date detection is only there to set the urgency of the message. The text
diff is the real guarantee: **any** wording change reaches you even if every
smarter check misses it.

All the signals from one cycle are merged into a **single** Telegram message,
most important first, so a ticket drop buzzes your phone once rather than seven
times.

## How it runs

GitHub's `schedule:` trigger bottoms out at 5 minutes and is regularly delayed
10–20 minutes under load. Instead, one job polls in a loop for ~5h40m (the job
cap is 6h) and dispatches its own successor before exiting:

- **~45 seconds** between checks, 7am–11pm Melbourne time
- **~3 minutes** overnight
- A `*/30` cron watchdog restarts the chain if a run is ever killed outright
- A `concurrency` group guarantees only one poller exists at a time

This requires the repo to be **public**, where Actions minutes are free and
unlimited. On a private repo this cadence would cost roughly $130/month.
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` live in Actions secrets, which are
**not** exposed by a public repo.

## Files

| File | Purpose |
| --- | --- |
| `watcher.py` | All the scraping, diffing and alerting |
| `.github/workflows/watch.yml` | The self-chaining poll loop |
| `state.json` | Last seen snapshot, committed back by the bot |

## Running it locally

```bash
pip install requests beautifulsoup4
python watcher.py            # one check, prints what it would send
python watcher.py --send     # one check, actually sends to Telegram
```

`--send` needs `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment.

## Watching another film

Add an entry to `FILMS` in `watcher.py`. `match` words are matched against the
listing headings, and all of them must appear:

```python
FILMS = [
    {
        "key": "dune-part-three",
        "name": "DUNE: PART THREE",
        "film_page": f"{SITE}/movie/dune-part-three",
        "match": ["dune"],
    },
]
```

Matching on just `["dune"]` deliberately catches any Dune listing, including a
separate IMAX Laser or 4K entry appearing alongside the 70mm one.

## Known limits

- **Queue-it.** `web.imaxmelbourne.com.au` sits behind Cloudflare and returns
  `403` to scripted requests, so the old Queue-it detector could never have
  worked from CI and has been removed. The watcher only reads
  `imaxmelbourne.com.au`, which is not blocked.
- **A queue is still a queue.** The alert tells you a sale is live; it cannot
  hold a place for you. Open the link immediately.
