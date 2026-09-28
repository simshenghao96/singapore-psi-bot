import io
from html import escape
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
PM25_URL = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
ADVISORY_URL = "https://www.moh.gov.sg/others/haze/"
SGT = ZoneInfo("Asia/Singapore")
REGIONS = ["north", "south", "east", "west", "central"]
COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#8C6400"]
STYLES = ["-", "--", "-.", ":", (0, (5, 2, 1, 2))]
BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "last_sent_psi.txt"
PM25_STATE_FILE = BASE_DIR / "last_sent_pm25.txt"
REPORT_FILE = BASE_DIR / "psi_reports.json"
ERRORS = (requests.RequestException, ValueError, KeyError, TypeError,
          AttributeError, OSError, RuntimeError)


def format_html(lines):
    # Escape all content first; only our own bold tags are inserted.
    prefixes = (
        "🇸🇬", "🌫️", "📈", "📋", "📍", "⬆️ Highest:",
        "Central:", "Latest 1-hour PM2.5 status:",
        "Based on the highest regional", "•", "⚠️ OLD DATA:",
        "If the air", "If you feel unwell",
    )
    return "\n".join(
        f"<b>{escape(line)}</b>" if line.startswith(prefixes)
        else escape(line)
        for line in lines
    )


def get_pm25_advisory(value):
    # NEA/MOH personal guide for activities during the next hour.
    if value <= 55:
        return ["• Usual activities can continue; take your health and symptoms into account."]
    if value <= 150:
        return [
            "• General population: Do less vigorous outdoor activity during the next hour.",
            "• Vulnerable people: Skip vigorous outdoor activity during the next hour.",
        ]
    if value <= 250:
        return [
            "• General population: Skip vigorous outdoor activity during the next hour.",
            "• Vulnerable people: Stay out of outdoor activities during the next hour.",
        ]
    return [
        "• General population: Spend as little time as possible on outdoor activities during the next hour.",
        "• Vulnerable people: Stay out of outdoor activities during the next hour.",
    ]


def parse_time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("Timestamp has no timezone.")
    return result.astimezone(SGT)


def save_text(path, text):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def fetch_items(day=None, url=PSI_URL):
    params = {} if day is None else {"date": day.isoformat()}
    items, seen = [], set()
    for _ in range(20):
        response = requests.get(url, params=params, timeout=20)
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


def read_values(item, field="psi_twenty_four_hourly"):
    raw = item["readings"][field]
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
    lines = ["📈 24-HOUR PSI — LONGER-TERM CONTEXT",
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
        lines += ["", "📋 24-HOUR PSI ADVICE — BASED ON HIGHEST REGIONAL PSI",
                  f"Based on the highest regional PSI: {highest}",
                  f"Region(s): {regions}", "Other regions may be in a different band.",
                  "This is a measured PSI reading, not tomorrow's forecast.", ""]
        lines += get_health_advisory(highest)
        lines += ["", "If you feel unwell, seek medical advice, especially if you are in a vulnerable group."]
    else:
        lines += ["", "Check the latest official conditions before planning outdoor activities."]
    lines += ["", "For immediate outdoor plans, refer to the 1-hour PM2.5 section above.",
              "https://www.haze.gov.sg/", "", "Readings: NEA / data.gov.sg",
              "Health guidance: MOH", ADVISORY_URL]
    return format_html(lines), reading_time


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
        "chat_id": chat_id, "text": message, "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True}
    })


def get_pm25_indicator(value):
    # NEA's 1-hour PM2.5 concentration bands, in micrograms/m3.
    # These are NOT the PSI bands.
    if value <= 55:
        return "🟢 Normal"
    if value <= 150:
        return "🟡 Elevated"
    if value <= 250:
        return "🟠 High"
    return "🔴 Very High"


def get_pm25_message(now=None):
    now = now or datetime.now(SGT)
    items = fetch_items(url=PM25_URL)
    if not items:
        raise ValueError("No PM2.5 readings available.")
    latest = max(items, key=lambda item: parse_time(item["timestamp"]))
    reading_time = parse_time(latest["timestamp"])
    readings = read_values(latest, "pm25_one_hourly")
    age = (now - reading_time).total_seconds()
    if age < -300:
        raise ValueError("PM2.5 timestamp is unexpectedly in the future.")
    stale = age > 7200
    highest = max(readings.values())
    highest_regions = ", ".join(
        r.title() for r in REGIONS if readings[r] == highest
    )
    lines = [
        "🌫️ 1-HOUR PM2.5 — RECENT AIR QUALITY",
        "Average fine-particle concentration over the past hour",
        f"Reading time: {reading_time:%d %b %Y, %I:%M %p} SGT",
        "",
    ]
    if stale:
        lines += [
            "⚠️ OLD DATA: These readings are over 2 hours old.",
            "Current conditions cannot be assessed from this update.",
            "",
        ]
    for region in REGIONS:
        value = readings[region]
        lines.append(
            f"{region.title()}: {value} µg/m³ — {get_pm25_indicator(value)}"
        )
    if not stale:
        lines += [
            "",
            f"Latest 1-hour PM2.5 status: {get_pm25_indicator(highest)}",
            f"Based on the highest regional reading: {highest} µg/m³",
            f"Region(s): {highest_regions}",
        ]
        if highest <= 55:
            lines.append("All five regions are within NEA's Normal band.")
        elif highest <= 150:
            lines.append("Elevated fine-particle levels in at least one region.")
        elif highest <= 250:
            lines.append("High fine-particle levels in at least one region.")
        else:
            lines.append("Very high fine-particle levels in at least one region.")
        lines.append("Check your own region; other regions may have a different band.")
        lines += [
            "",
            "📍 CENTRAL — ADVICE FOR THE NEXT HOUR",
            f"Based on Central's 1-hour PM2.5: {readings['central']} µg/m³ "
            f"— {get_pm25_indicator(readings['central'])}",
        ]
        lines += get_pm25_advisory(readings["central"])
        lines += [
            "Vulnerable people include older adults, pregnant people, children, "
            "and people with chronic heart or lung disease.",
            "If the air is irritating, reduce exposure and strenuous outdoor activity "
            "even if the regional reading is Normal.",
            "If you feel unwell, seek medical advice.",
        ]
    lines += [
        "",
        "This is a 1-hour average, not an instantaneous reading.",
        "Normal does not mean zero pollution. PM2.5 alone does not identify its source.",
        "For immediate outdoor-activity guidance: https://www.haze.gov.sg/",
    ]
    return format_html(lines), reading_time


def check_latest(token, chat_id):
    # Track each API separately: one may publish before the other.
    sources = [
        ("1-hour PM2.5", PM25_STATE_FILE, get_pm25_message),
        ("24-hour PSI", STATE_FILE, get_psi_message),
    ]
    sections, updates = [], []
    failed = False
    for label, path, getter in sources:
        try:
            previous = (parse_time(path.read_text(encoding="utf-8").strip())
                        if path.exists() else None)
            message, reading_time = getter()
            if previous is not None and reading_time < previous:
                raise ValueError("API returned an older reading than previously sent.")
            sections.append(message)
            if previous is None or reading_time > previous:
                updates.append((path, reading_time, label))
        except ERRORS as error:
            print(f"{label} unavailable ({type(error).__name__}).")
            sections.append(
                f"⚠️ {label}: valid latest readings are unavailable. "
                "No current assessment is made from this source."
            )
            failed = True
    if updates:
        message = "<b>🇸🇬 Singapore Air Quality Update</b>\n\n" + "\n\n".join(sections)
        send_telegram_message(token, chat_id, message)
        # Save only after Telegram confirms delivery.
        for path, reading_time, label in updates:
            save_text(path, reading_time.isoformat() + "\n")
            print(f"New {label} update sent: {reading_time:%d %b %Y, %I:%M %p} SGT")
    else:
        print("No newer PSI or PM2.5 reading. Skipping Telegram message.")
    if failed:
        raise RuntimeError("One or more air-quality sources could not be checked.")


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


def get_report_caption(kind, period, hours, hourly):
    # Use the same readings as the chart, across all five regions.
    entries = [
        (values[region], timestamp, region)
        for timestamp, values in sorted(hourly.items())
        for region in REGIONS
    ]
    if not entries:
        raise ValueError("No readings available for the summary.")

    lowest = min(entries, key=lambda entry: entry[0])
    highest = max(entries, key=lambda entry: entry[0])
    categories = list(dict.fromkeys(
        get_psi_indicator(value)
        for value in sorted({entry[0] for entry in entries})
    ))

    def describe(entry):
        value, timestamp, region = entry
        when = (timestamp.strftime("%I:%M %p") if kind == "daily"
                else timestamp.strftime("%a %d %b, %I:%M %p"))
        return f"{value} — {region.title()}, {when}"

    unhealthy_hours = sum(
        any(values[region] > 100 for region in REGIONS)
        for values in hourly.values()
    )
    lines = [
        f"📊 Singapore PSI — {kind.title()} Summary",
        period,
        "",
        "Recorded categories: " + "; ".join(categories),
        f"⬇️ Lowest: {describe(lowest)}",
        f"⬆️ Highest: {describe(highest)}",
    ]
    if (sum(entry[0] == lowest[0] for entry in entries) > 1
            or sum(entry[0] == highest[0] for entry in entries) > 1):
        lines.append("Ties: earliest hour, then first region shown.")

    lines.append("")
    if unhealthy_hours:
        lines.append(
            f"⚠️ At least one region recorded Unhealthy PSI or higher "
            f"in {unhealthy_hours} of {len(hourly)} available hourly snapshots."
        )
    else:
        lines.append("✅ No Unhealthy-or-higher PSI readings in the available data.")

    lines += ["", f"Hours available: {len(hourly)}/{hours}."]
    if len(hourly) < hours:
        lines.append("⚠️ Incomplete data: summary covers available readings only; chart gaps are missing hours.")
    lines += [
        "Based on hourly snapshots of 24-hour PSI, not instantaneous conditions.",
        "All times Singapore time (SGT).",
        "Source: NEA / data.gov.sg",
    ]
    return "\n".join(lines)


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
            caption = get_report_caption(kind, period, hours, hourly)
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
