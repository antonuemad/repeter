"""Presentation-only gap explanations. Never changes eligibility or schedules work."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def empty_day(plan, day, now, *, has_targets=True):
    zone = ZoneInfo(plan.timezone)
    today = now.astimezone(zone).date()
    if day < plan.start_date:
        return "before_start"
    if day < today:
        return "elapsed"
    if day > max(today, plan.start_date) + timedelta(days=29):
        return "outside_window"
    if day.weekday() not in plan.weekdays:
        return "off_day"
    valid = []
    for value in plan.times:
        local = datetime.combine(day, datetime.strptime(value, "%H:%M").time()).replace(tzinfo=zone)
        at = local.astimezone(UTC)
        if at.astimezone(zone).replace(tzinfo=None) == local.replace(tzinfo=None):
            valid.append(at)
    if not valid:
        return "clock_gap"
    if all(at <= now for at in valid):
        return "elapsed"
    return "no_upcoming" if has_targets else "no_pages"


def recovery(entry):
    reason = entry.reason
    detail = entry.detail if reason == "no_eligible" else None
    key = detail or reason
    section = (
        "accounts"
        if reason in {"account_disconnected", "account_reconnect", "account_unavailable"}
        else "schedule"
        if reason == "exhausted"
        else "activity"
        if reason == "pending"
        else "slot"
        if reason in {"excluded", "replacement_unavailable"}
        else "content"
    )
    return {"key": key, "section": section}


def gap_summary(result):
    counts = Counter()
    example = {}
    for row in result["rows"]:
        for entry in row.entries:
            if entry.video is None:
                info = recovery(entry)
                counts[info["key"]] += 1
                example.setdefault(info["key"], (info, entry, row.at))
    return [
        dict(**example[key][0], count=count, entry=example[key][1], at=example[key][2])
        for key, count in counts.items()
    ]
