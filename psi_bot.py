import math
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


# ==================================================
# SETTINGS
# ==================================================

PSI_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
ADVISORY_URL = "https://www.moh.gov.sg/others/haze/"

SGT = ZoneInfo("Asia/Singapore")
REGIONS = ["north", "south", "east", "west", "central"]

STATE_FILE = Path(__file__).resolve().parent / "last_sent_psi.txt"


# ==================================================
# PSI INDICATORS
# ==================================================

def get_psi_indicator(psi):
    if psi <= 50:
        return "🟢 Good"
    if psi <= 100:
        return "🟡 Moderate"
    if psi <= 200:
        return "🟠 Unhealthy"
    if psi <= 300:
        return "🔴 Very Unhealthy"

    return "🟣 Hazardous"


# ==================================================
# HEALTH ADVISORIES
# ==================================================

def get_health_advisory(psi):
    if psi <= 100:
        return [
            "• All groups: Continue usual activities."
        ]

    if psi <= 200:
        return [
            "• Healthy adults: Cut back on outdoor exercise "
            "that is intense or lasts several hours.",

            "• Older adults, pregnant people and children: "
            "Keep such exercise to a minimum.",

            "• People with chronic heart or lung conditions: "
            "Do not do such exercise outdoors."
        ]

    if psi <= 300:
        return [
            "• Healthy adults: Do not exercise outdoors "
            "intensely or for several hours.",

            "• Older adults, pregnant people and children: "
            "Keep time outdoors to a minimum.",

            "• People with chronic heart or lung conditions: "
            "Avoid outdoor activity."
        ]

    return [
        "• Healthy adults: Keep time outdoors to a minimum.",

        "• Older adults, pregnant people, children and people "
        "with chronic heart or lung conditions: Avoid outdoor activity."
    ]


# ==================================================
# FETCH PSI AND BUILD THE MESSAGE
# ==================================================

def get_psi_message():
    response = requests.get(PSI_URL, timeout=30)
    response.raise_for_status()

    data = response.json()

    if data.get("code") != 0:
        raise ValueError("PSI API returned an error.")

    items = data["data"]["items"]

    if not items:
        raise ValueError("No PSI readings are available.")

    latest = max(
        items,
        key=lambda item: datetime.fromisoformat(item["timestamp"])
    )

    raw_readings = latest["readings"]["psi_twenty_four_hourly"]
    readings = {}

    for region in REGIONS:
        value = raw_readings[region]

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or value != int(value)
        ):
            raise ValueError(f"Invalid PSI reading for {region}.")

        readings[region] = int(value)

    reading_time = datetime.fromisoformat(latest["timestamp"])

    if reading_time.tzinfo is None:
        raise ValueError("PSI timestamp has no timezone.")

    reading_time = reading_time.astimezone(SGT)

    age_seconds = (
        datetime.now(SGT) - reading_time
    ).total_seconds()

    is_stale = age_seconds > 7200

    lines = [
        "🇸🇬 Singapore Air Quality Update",
        "24-hour PSI",
        f"Reading time: {reading_time:%d %b %Y, %I:%M %p} SGT",
        ""
    ]

    if is_stale:
        lines.extend([
            "⚠️ OLD DATA: These readings are over 2 hours old.",
            "They may not reflect current conditions.",
            ""
        ])

    for region in REGIONS:
        psi = readings[region]
        indicator = get_psi_indicator(psi)

        lines.append(
            f"{region.title()}: {psi} — {indicator}"
        )

    if not is_stale:
        highest_psi = max(readings.values())

        highest_regions = ", ".join(
            region.title()
            for region in REGIONS
            if readings[region] == highest_psi
        )

        lines.extend([
            "",
            "📋 General health advice",
            f"Based on the highest regional PSI: {highest_psi}",
            f"Region(s): {highest_regions}",
            "Other regions may be in a different band.",
            ""
        ])

        lines.extend(get_health_advisory(highest_psi))

        lines.extend([
            "",
            "If you feel unwell, seek medical advice, especially "
            "if you are in a vulnerable group."
        ])

    else:
        lines.extend([
            "",
            "Check the latest official conditions before "
            "planning outdoor activities."
        ])

    lines.extend([
        "",
        "For immediate outdoor plans, also check 1-hour PM2.5:",
        "https://www.haze.gov.sg/",
        "",
        "Readings: NEA / data.gov.sg",
        "Health guidance: MOH",
        ADVISORY_URL
    ])

    return "\n".join(lines), reading_time


# ==================================================
# SEND MESSAGE TO TELEGRAM
# ==================================================

def send_telegram_message(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        response = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "link_preview_options": {
                    "is_disabled": True
                }
            },
            timeout=30
        )

        result = response.json()

    except (requests.RequestException, ValueError):
        # Do not print the URL because it contains the token.
        raise RuntimeError(
            "Telegram connection failed. Try again later."
        ) from None

    if not isinstance(result, dict):
        raise RuntimeError("Telegram returned an unexpected response.")

    if response.status_code != 200 or not result.get("ok"):
        error_code = result.get(
            "error_code",
            response.status_code
        )

        raise RuntimeError(
            f"Telegram rejected the message (error code: {error_code}). "
            "Check the token, channel username and bot posting permission."
        )


# ==================================================
# CHECK FOR A NEW READING AND SEND IT
# ==================================================

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")
        return 1

    try:
        message, reading_time = get_psi_message()

    except (
        requests.RequestException,
        ValueError,
        KeyError,
        TypeError
    ):
        print("Unable to retrieve valid PSI readings. No message sent.")
        return 1

    # Skip readings already sent, including older readings.
    if STATE_FILE.exists():
        try:
            saved_timestamp = STATE_FILE.read_text(
                encoding="utf-8"
            ).strip()

            last_sent_time = datetime.fromisoformat(saved_timestamp)

            if last_sent_time.tzinfo is None:
                raise ValueError("Saved timestamp has no timezone.")

        except (OSError, ValueError):
            print("Unable to read last_sent_psi.txt. Check its contents.")
            return 1

        if reading_time <= last_sent_time:
            print(
                "No newer PSI reading. "
                "Skipping Telegram message."
            )
            return 0

    try:
        send_telegram_message(token, chat_id, message)

    except RuntimeError as error:
        print(error)
        return 1

    # Save only after Telegram confirms the message was sent.
    try:
        STATE_FILE.write_text(
            reading_time.isoformat() + "\n",
            encoding="utf-8"
        )

    except OSError:
        print("Message sent, but its timestamp could not be saved.")
        return 1

    print(
        f"New PSI update sent: "
        f"{reading_time:%d %b %Y, %I:%M %p} SGT"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
