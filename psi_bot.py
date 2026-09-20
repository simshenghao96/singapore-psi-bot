import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


PSI_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
SGT = ZoneInfo("Asia/Singapore")


def get_psi_message():
    response = requests.get(PSI_URL, timeout=30)
    response.raise_for_status()
    data = response.json()

    if data.get("code") != 0:
        raise ValueError("PSI API returned an error.")

    items = data["data"]["items"]

    if not items:
        raise ValueError("No PSI readings are available.")

    latest = max(items, key=lambda item: item["timestamp"])
    readings = latest["readings"]["psi_twenty_four_hourly"]

    reading_time = datetime.fromisoformat(
        latest["timestamp"]
    ).astimezone(SGT)

    lines = [
        "🇸🇬 Singapore 24-hour PSI",
        f"Reading time: {reading_time:%d %b %Y, %I:%M %p} SGT",
        ""
    ]

    for region in ["north", "south", "east", "west", "central"]:
        lines.append(f"{region.title()}: {readings[region]}")

    age_seconds = (
        datetime.now(SGT) - reading_time
    ).total_seconds()

    if age_seconds > 7200:
        lines.extend([
            "",
            "⚠️ These readings are over 2 hours old."
        ])

    lines.extend([
        "",
        "Source: NEA / data.gov.sg"
    ])

    return "\n".join(lines)


def send_telegram_message(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message
            },
            timeout=30
        )
        result = response.json()
    except (requests.RequestException, ValueError):
        # Do not print the request URL, which contains the token.
        raise RuntimeError(
            "Telegram connection failed. Check Telegram and try again."
        ) from None

    if response.status_code != 200 or not result.get("ok"):
        raise RuntimeError(
            f"Telegram rejected the message "
            f"(error code: {result.get('error_code', response.status_code)}). "
            "Check your token, chat ID, and that you have pressed Start."
        )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")
        return 1

    try:
        message = get_psi_message()
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError
    ):
        print("Unable to retrieve PSI readings. No message sent.")
        return 1

    try:
        send_telegram_message(token, chat_id, message)
    except RuntimeError as error:
        print(error)
        return 1

    print("PSI update sent successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
