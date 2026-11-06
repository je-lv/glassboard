"""Quiet hours (B56, ERRATA E38): is a member inside its daily quiet window, and until when. Stdlib only.

A window is `start`-`end` local time (HH:MM, 24 h) in an IANA time zone; `end` before `start` crosses
midnight (22:00-07:00). Equal start and end is refused (it would mean always or never).
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")
TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]{0,30}(/[A-Za-z0-9_+\-]{1,30}){0,2}$")


def parse_hhmm(v: str) -> time:
    m = HHMM.match(v or "")
    if not m:
        raise ValueError("times are HH:MM, 24-hour")
    return time(int(m.group(1)), int(m.group(2)))


def zone(name: str) -> ZoneInfo:
    if not TZ_RE.match(name or ""):
        raise ValueError("time zone is an IANA name like Europe/Madrid or UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("unknown time zone") from None


def validate(start: str, end: str, tz: str) -> None:
    if parse_hhmm(start) == parse_hhmm(end):
        raise ValueError("start and end must differ")
    zone(tz)


def state(start: str | None, end: str | None, tz: str | None, now: datetime | None = None) -> dict | None:
    """None without a window; else {window, active, until (UTC ISO, the next boundary)}."""
    if not (start and end and tz):
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(zone(tz))
    s, e = parse_hhmm(start), parse_hhmm(end)
    t = now.time().replace(second=0, microsecond=0)
    active = (s <= t < e) if s < e else (t >= s or t < e)
    boundary = e if active else s
    day = now.date()
    nxt = datetime.combine(day, boundary, tzinfo=now.tzinfo)
    if nxt <= now:
        nxt = datetime.combine(day + timedelta(days=1), boundary, tzinfo=now.tzinfo)
    return {"window": f"{start}-{end} {tz}", "active": active,
            "until": nxt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def of_member(row, now: datetime | None = None) -> dict | None:
    keys = row.keys()
    if "quiet_start" not in keys:
        return None
    q = state(row["quiet_start"], row["quiet_end"], row["quiet_tz"], now)
    if q is not None:
        q["urgent"] = bool(row["quiet_urgent"])
    return q
