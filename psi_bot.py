import io
import json
import math
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")  # Render images on GitHub without a display.
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests

PSI_URL = "https://api-open.data.gov.sg/v2/real-time/api/psi"
ADVISORY_URL = "https://www.moh.gov.sg/others/haze/"
SGT = ZoneInfo("Asia/Singapore")
REGIONS = ["north", "south", "east", "west", "central"]
COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#8C6400"]
STYLES = ["-", "--", "-.", ":", (0, (5, 2, 1, 2))]
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "last_sent_psi.txt"
REPORT_FILE = BASE_DIR / "psi_reports.json"
ERRORS = (requests.RequestException, ValueError, KeyError, TypeError,
          AttributeError, OSError, RuntimeError)


def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp has no timezone.")
    return result.astimezone(SGT)


def save_text(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def fetch_items(day=None):
    params = {} if day is None else {"date": day.isoformat()}
    items, seen = [], set()
    for _ in range(20):
        response = requests.get(PSI_URL, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise ValueError("PSI API returned an error.")
        data = payload["data"]
        if not isinstance(data["items"], list):
            raise ValueError("Invalid PSI response.")
        items.extend(data["items"])
        token = data.get("paginationToken")
        if not token:
            return items
        if not isinstance(token, str) or token in seen:
            raise ValueError("Invalid pagination token.")
        seen.add(token)
        params["paginationToken"] = token
    raise ValueError("Too many API pages; refusing an incomplete response.")


def read_values(item):
    raw = item["readings"]["psi_twenty_four_hourly"]
    values = {}
    for region in REGIONS:
        value = raw[region]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0 or value != int(value)):
            raise ValueError(f"Invalid PSI for {region}.")
        values[region] = int(value)
    return values


def get_psi_indicator(psi):
    for limit, label in [(50, "🟢 Good"), (100, "🟡 Moderate"),
                         (200, "🟠 Unhealthy"), (300, "🔴 Very Unhealthy")]:
        if psi <= limit:
            return label
    return "🟣 Hazardous"


def get_health_advisory(psi):
    if psi <= 100:
        return ["• All groups: Continue usual activities."]
    if psi <= 200:
        return [
            "• Healthy adults: Cut back on outdoor exercise that is intense or lasts several hours.",
            "• Older adults, pregnant people and children: Keep such exercise to a minimum.",
            "• People with chronic heart or lung conditions: Do not do such exercise outdoors."
        ]
    if psi <= 300:
        return [
            "• Healthy adults: Do not exercise outdoors intensely or for several hours.",
            "• Older adults, pregnant people and children: Keep time outdoors to a minimum.",
            "• People with chronic heart or lung conditions: Avoid outdoor activity."
        ]
    return [
        "• Healthy adults: Keep time outdoors to a minimum.",
        "• Older adults, pregnant people, children and people with chronic heart or lung conditions: Avoid outdoor activity."
    ]


def get_psi_message():
    items = fetch_items()
    if not items:
        raise ValueError("No PSI readings available.")
    latest = max(items, key=lambda item: parse_time(item["timestamp"]))
    readings = read_values(latest)
    reading_time = parse_time(latest["timestamp"])
    is_stale = (datetime.now(SGT) - reading_time).total_seconds() > 7200
    lines = ["🇸🇬 Singapore Air Quality Update", "24-hour PSI",
             f"Reading time: {reading_time:%d %b %Y, %I:%M %p} SGT", ""]
    if is_stale:
        lines += ["⚠️ OLD DATA: These readings are over 2 hours old.",
                  "They may not reflect current conditions.", ""]
    for region in REGIONS:
        psi = readings[region]
        lines.append(f"{region.title()}: {psi} — {get_psi_indicator(psi)}")
    if not is_stale:
        highest = max(readings.values())
        regions = ", ".join(r.title() for r in REGIONS if readings[r] == highest)
        lines += ["", "📋 General health advice",
                  f"Based on the highest regional PSI: {highest}",
                  f"Region(s): {regions}", "Other regions may be in a different band.", ""]
        lines += get_health_advisory(highest)
        lines += ["", "If you feel unwell, seek medical advice, especially if you are in a vulnerable group."]
    else:
        lines += ["", "Check the latest official conditions before planning outdoor activities."]
    lines += ["", "For immediate outdoor plans, also check 1-hour PM2.5:",
              "https://www.haze.gov.sg/", "", "Readings: NEA / data.gov.sg",
              "Health guidance: MOH", ADVISORY_URL]
    return "\n".join(lines), reading_time


def telegram_request(token, method, **kwargs):
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            timeout=60, **kwargs
        )
        result = response.json()
    except (requests.RequestException, ValueError):
        # Never print request exceptions: they can contain the bot token.
        raise RuntimeError("Telegram connection failed; delivery is unconfirmed.") from None
    if response.status_code != 200 or not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError("Telegram rejected the request. Check bot permissions and secrets.")


def send_telegram_message(token, chat_id, message):
    telegram_request(token, "sendMessage", json={
        "chat_id": chat_id, "text": message,
        "link_preview_options": {"is_disabled": True}
    })


def check_latest(token, chat_id):
    message, reading_time = get_psi_message()
    if STATE_FILE.exists():
        previous = parse_time(STATE_FILE.read_text(encoding="utf-8").strip())
        if reading_time <= previous:
            print("No newer PSI reading. Skipping Telegram message.")
            return
    send_telegram_message(token, chat_id, message)
    save_text(STATE_FILE, reading_time.isoformat() + "\n")
    print(f"New PSI update sent: {reading_time:%d %b %Y, %I:%M %p} SGT")


def period_rows(start, end, cache):
    # end is exclusive. Fetch history rather than relying on prior bot runs.
    rows = {}
    day = start.date()
    while day < end.date():
        if day not in cache:
            cache[day] = fetch_items(day)
        for item in cache[day]:
            timestamp = parse_time(item["timestamp"])
            if not start <= timestamp < end:
                continue
            try:
                values = read_values(item)
            except (ValueError, KeyError, TypeError):
                continue  # A missing/invalid hour becomes a gap, never zero.
            # Use the newest revision if the API repeats a reading timestamp.
            updated = parse_time(item.get("updatedTimestamp") or item["timestamp"])
            if timestamp not in rows or updated > rows[timestamp][0]:
                rows[timestamp] = (updated, values)
        day += timedelta(days=1)
    # Select the latest timestamp in each hour; normally PSI is hourly.
    hourly = {}
    for timestamp in sorted(rows):
        hourly[timestamp.replace(minute=0, second=0, microsecond=0)] = rows[timestamp][1]
    return hourly


def make_chart(kind, start, end, hourly):
    hours = int((end - start).total_seconds() // 3600)
    times = [start + timedelta(hours=i) for i in range(hours)]
    period = (f"{start:%d %b %Y}" if kind == "daily" else
              f"{start:%d %b %Y} - {end - timedelta(days=1):%d %b %Y}")
    fig, ax = plt.subplots(figsize=(12, 6.8), dpi=160)
    try:
        fig.patch.set_facecolor("#F7F9FC")
        ax.set_facecolor("white")
        for region, color, style in zip(REGIONS, COLORS, STYLES):
            values = [hourly.get(t, {}).get(region, float("nan")) for t in times]
            ax.plot(times, values, label=region.title(), color=color,
                    linestyle=style, linewidth=2, marker="o",
                    markersize=3 if kind == "daily" else 1.6, alpha=0.85)
        ax.set_title(f"Singapore PSI | {kind.title()} Report\n{period}",
                     fontsize=18, fontweight="bold", pad=20)
        ax.set_ylabel("24-hour PSI", fontsize=12)
        ax.set_xlabel("Time (Singapore time)" if kind == "daily" else "Date (Singapore time)")
        ax.set_xlim(start, end - timedelta(minutes=1))
        ax.set_ylim(bottom=0)
        if kind == "daily":
            ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3), tz=SGT))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=SGT))
        else:
            ax.xaxis.set_major_locator(mdates.DayLocator(tz=SGT))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%a\n%d %b", tz=SGT))
        ax.grid(True, alpha=0.2)
        ax.spines[["top", "right"]].set_visible(False)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=5, frameon=False)
        fig.text(0.06, 0.035,
                 f"Source: NEA / data.gov.sg | Hours available: {len(hourly)}/{hours}\n"
                 "Hourly snapshots of 24-hour PSI. Missing hours are gaps; equal readings may overlap.",
                 fontsize=9, color="#475569")
        fig.subplots_adjust(left=0.08, right=0.98, top=0.83, bottom=0.28)
        output = io.BytesIO()
        fig.savefig(output, format="png", facecolor=fig.get_facecolor())
        output.seek(0)
        return output, period, hours
    finally:
        plt.close(fig)


def check_reports(token, chat_id, now=None):
    now = now or datetime.now(SGT)
    today = datetime.combine(now.date(), time.min, tzinfo=SGT)
    monday = today - timedelta(days=today.weekday())
    state = json.loads(REPORT_FILE.read_text(encoding="utf-8")) if REPORT_FILE.exists() else {}
    if not isinstance(state, dict):
        raise ValueError("Invalid report state.")
    cache, failed = {}, False
    for kind, end, days in [("daily", today, 1), ("weekly", monday, 7)]:
        start = end - timedelta(days=days)
        key = (end - timedelta(days=1)).date().isoformat()
        try:
            previous = state.get(kind)
            if previous is not None and date.fromisoformat(previous) >= date.fromisoformat(key):
                continue
            hourly = period_rows(start, end, cache)
            expected = days * 24
            if not hourly:
                raise ValueError("No historical readings available.")
            # Allow delayed readings to appear during the first hour after midnight.
            if len(hourly) < expected and now < end + timedelta(hours=1):
                print(f"{kind.title()} chart waiting for missing readings; retry next run.")
                continue
            picture, period, hours = make_chart(kind, start, end, hourly)
            caption = (f"📊 Singapore PSI — {kind.title()} Report\n{period}\n"
                       f"Hourly 24-hour PSI for all five regions.\n"
                       f"Hours available: {len(hourly)}/{hours}. All times SGT.")
            if len(hourly) < hours:
                caption += "\n⚠️ Incomplete data: missing hours are gaps in the chart."
            with picture:
                telegram_request(token, "sendPhoto", data={"chat_id": chat_id, "caption": caption},
                                 files={"photo": (f"psi_{kind}_{key}.png", picture, "image/png")})
            state[kind] = key
            save_text(REPORT_FILE, json.dumps(state, indent=2) + "\n")
            print(f"{kind.title()} chart sent for period ending {key}.")
        except ERRORS as error:
            print(f"{kind.title()} report failed ({type(error).__name__}); retry next run.")
            failed = True
    return failed


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID secret.")
        return 1
    failed = False
    try:
        check_latest(token, chat_id)
    except ERRORS as error:
        print(f"Latest PSI update failed ({type(error).__name__}).")
        failed = True
    # Reports must still run even if the latest reading was already sent.
    try:
        failed = check_reports(token, chat_id) or failed
    except ERRORS as error:
        print(f"Report check failed ({type(error).__name__}). Check psi_reports.json and API access.")
        failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
