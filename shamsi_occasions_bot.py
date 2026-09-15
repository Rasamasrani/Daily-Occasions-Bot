#!/usr/bin/env python3
"""
Shamsi Occasions Telegram Bot
==============================

Sends the Shamsi (Jalali/Persian) calendar occasions for "today" to a
Telegram group chat, every day at a configured time.

Data source
-----------
Occasions/holidays are pulled from the pipe2time.ir project's static JSON
mirror (hosted on GitHub Pages), which scrapes time.ir and republishes it
per Shamsi year:

    GET https://hmarzban.github.io/pipe2time.ir/api/{year}/events.json

No API key is required. Being static files on GitHub Pages, this is far
more durable than a small personally-hosted API server.

Setup
-----
1. Create a bot with @BotFather on Telegram and copy the bot token.
2. Add the bot to your target group and make it an admin (or at least
   give it permission to post messages).
3. Get the group's chat_id (see get_chat_id() below, or forward a message
   from the group to @userinfobot / use getUpdates).
4. Install dependencies:

       pip install requests jdatetime

5. Set the environment variables below (or edit the DEFAULT_* constants),
   then run:

       python3 shamsi_occasions_bot.py --once   # send immediately, once
       python3 shamsi_occasions_bot.py          # run forever, daily at SEND_TIME

   For production use, it's usually simpler and more robust to run this
   script with `--once` from a cron job / systemd timer scheduled for
   your desired time, rather than leaving the built-in loop running.

Environment variables
----------------------
TELEGRAM_BOT_TOKEN   Bot token from @BotFather                 (required)
TELEGRAM_CHAT_ID     Target group chat id, e.g. -1001234567890  (required)
SEND_TIME            "HH:MM" 24h, time of day to send (default 08:00)
TIMEZONE             IANA timezone name (default "Asia/Tehran")
"""

import os
import sys
import time
import argparse
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import jdatetime

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEND_TIME = os.environ.get("SEND_TIME", "08:00")          # 24h "HH:MM"
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Tehran")

HOLIDAY_API_URL = "https://hmarzban.github.io/pipe2time.ir/api/{year}/events.json"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("shamsi-occasions-bot")

# Make jdatetime render weekday/month names in Persian (rather than the
# default English transliteration) for %A / %B in strftime.
jdatetime.set_locale(jdatetime.FA_LOCALE)


# --------------------------------------------------------------------------
# Shamsi date helpers
# --------------------------------------------------------------------------

def get_shamsi_today(tz_name: str = TIMEZONE) -> jdatetime.date:
    """Return today's date as a jdatetime.date, in the given timezone."""
    now = datetime.now(ZoneInfo(tz_name))
    return jdatetime.date.fromgregorian(date=now.date())


def format_shamsi_header(jdate: jdatetime.date) -> str:
    """e.g. 'سه‌شنبه ۲۴ شهریور ۱۴۰۵' (weekday, day, month, year in Persian)."""
    return jdate.strftime("%A %d %B %Y")


# --------------------------------------------------------------------------
# Occasions fetching
# --------------------------------------------------------------------------

def fetch_occasions(jdate: jdatetime.date, timeout: int = 15) -> dict:
    """
    Fetch today's occasions for the given Shamsi date.

    Downloads that Shamsi year's full events file (one JSON array covering
    all 12 months) and filters it down to just today's date, since the
    source only publishes per-year files rather than a per-day endpoint.

    Returns a dict like:
        {"is_holiday": bool, "events": [{"description": str,
                                          "is_holiday": bool}, ...]}
    Raises requests.RequestException on network/HTTP failure.
    """
    url = HOLIDAY_API_URL.format(year=jdate.year)
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    all_events = resp.json()

    target = f"{jdate.year:04d}/{jdate.month:02d}/{jdate.day:02d}"
    todays = [ev for ev in all_events if ev.get("jDate") == target]

    events = [
        {"description": ev.get("text", "").strip(), "is_holiday": bool(ev.get("isHoliday"))}
        for ev in todays
    ]
    return {
        "is_holiday": any(ev["is_holiday"] for ev in events),
        "events": events,
    }


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------

def format_message(jdate: jdatetime.date, data: dict) -> str:
    header = format_shamsi_header(jdate)
    events = data.get("events", [])
    is_holiday = data.get("is_holiday", False)

    lines = [f"📅 تاریخ امروز: ، {header}"]
    if is_holiday:
        lines.append("🔴 امروز تعطیل رسمی است")
    lines.append("")

    if not events:
        lines.append("مناسبت خاصی برای امروز ثبت نشده است.")
    else:
        for ev in events:
            desc = ev.get("description", "").strip()
            if not desc:
                continue
            marker = "🔴" if ev.get("is_holiday") else "▫️"
            lines.append(f"{marker} {desc}")

    return "\n".join(lines)


# --------------------------------------------------------------------------
# Telegram sending
# --------------------------------------------------------------------------

def send_telegram_message(token: str, chat_id: str, text: str, timeout: int = 15) -> None:
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set."
        )
    url = TELEGRAM_API_URL.format(token=token)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API returned an error: {result}")


def get_chat_id(token: str) -> None:
    """
    Utility helper: prints recent chat_ids seen by the bot via getUpdates.
    Send any message in the target group (mentioning the bot, or just any
    message if the bot is already a member) and then run:

        python3 shamsi_occasions_bot.py --get-chat-id
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    seen = set()
    for update in data.get("result", []):
        msg = update.get("message") or update.get("channel_post")
        if not msg:
            continue
        chat = msg.get("chat", {})
        key = (chat.get("id"), chat.get("title") or chat.get("username"))
        if key not in seen:
            seen.add(key)
            print(f"chat_id: {chat.get('id')}   title: {chat.get('title') or chat.get('username')}")
    if not seen:
        print(
            "No chats found yet. Send a message in the target group first "
            "(with the bot already added to it), then re-run this command."
        )


# --------------------------------------------------------------------------
# Core run + scheduling
# --------------------------------------------------------------------------

def run_once() -> None:
    jdate = get_shamsi_today()
    log.info("Fetching occasions for Shamsi date %s", jdate)
    try:
        data = fetch_occasions(jdate)
    except requests.RequestException as exc:
        log.error("Failed to fetch occasions: %s", exc)
        return
    text = format_message(jdate, data)
    try:
        send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)
    except Exception as exc:
        log.error("Failed to send Telegram message: %s", exc)
        return
    log.info("Occasions sent successfully.")


def seconds_until_next_run(send_time: str, tz_name: str) -> float:
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    hour, minute = (int(p) for p in send_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_forever() -> None:
    log.info(
        "Starting daily loop. Will send every day at %s (%s).",
        SEND_TIME, TIMEZONE,
    )
    while True:
        wait_s = seconds_until_next_run(SEND_TIME, TIMEZONE)
        log.info("Sleeping %.0f seconds until next send.", wait_s)
        time.sleep(wait_s)
        run_once()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Shamsi occasions Telegram bot")
    parser.add_argument("--once", action="store_true", help="Send the occasions once and exit (e.g. for cron).")
    parser.add_argument("--get-chat-id", action="store_true", help="List chat_ids visible to the bot and exit.")
    args = parser.parse_args()

    if args.get_chat_id:
        if not TELEGRAM_BOT_TOKEN:
            sys.exit("Set TELEGRAM_BOT_TOKEN first.")
        get_chat_id(TELEGRAM_BOT_TOKEN)
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        sys.exit(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables "
            "before running (see the module docstring for details)."
        )

    if args.once:
        run_once()
    else:
        run_forever()


if __name__ == "__main__":
    main()
