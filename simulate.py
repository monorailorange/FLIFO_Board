"""
Generates a synthetic, self-consistent flight schedule for previewing the
board and debug view when real data is unavailable -- without touching
storage or requiring any credentials/params to change.

Deliberately reuses parser.parse_calendar() on a hand-built .ics blob rather
than constructing FlightEvent objects directly, so a simulated preview goes
through the *exact same* parsing/current-next/status pipeline the real feed
does. What you see in simulated mode is a faithful preview of the real
rendering path, not a separate mock UI to keep in sync by hand.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from parser import parse_calendar
from models import FlightEvent


def _local(utc_dt: datetime, tzname: str) -> tuple[str, str]:
    local = utc_dt.astimezone(ZoneInfo(tzname))
    return local.strftime("%d%b"), local.strftime("%H%M")


def _vevent(uid: str, dtstart: datetime, dtend: datetime, summary: str) -> str:
    fmt = "%Y%m%dT%H%M%SZ"
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{dtstart.strftime(fmt)}\r\n"
        f"DTSTART:{dtstart.strftime(fmt)}\r\n"
        f"DTEND:{dtend.strftime(fmt)}\r\n"
        f"SUMMARY:{summary}\r\n"
        "END:VEVENT\r\n"
    )


def _build_ics(now: datetime) -> bytes:
    events = []

    # 1. Already arrived, well outside the grace window -- exercises the
    #    ARRIVED status and confirms it's correctly excluded from current/next.
    dep = now - timedelta(hours=6)
    arr = dep + timedelta(hours=2, minutes=10)
    d1, t1 = _local(dep, "America/Los_Angeles")
    d2, t2 = _local(arr, "America/Denver")
    events.append(_vevent("sim-past", dep, arr, f"UA1812 SFO {d1} {t1} - DEN {d2} {t2}"))

    # 2. Currently en route -- this is what should render as "Current Flight".
    dep = now - timedelta(hours=1, minutes=15)
    arr = now + timedelta(hours=1, minutes=45)
    d1, t1 = _local(dep, "America/Denver")
    d2, t2 = _local(arr, "America/Chicago")
    events.append(_vevent("sim-current", dep, arr, f"UA2044 DEN {d1} {t1} - ORD {d2} {t2}"))

    # 3. Later the same day -- "Next Flight".
    dep = now + timedelta(hours=4)
    arr = dep + timedelta(hours=2, minutes=20)
    d1, t1 = _local(dep, "America/Chicago")
    d2, t2 = _local(arr, "America/New_York")
    events.append(_vevent("sim-next", dep, arr, f"UA560 ORD {d1} {t1} - EWR {d2} {t2}"))

    # 4. A multi-day block further out -- exercises the "No Flights Today:
    #    [CODE]" rendering path (see parser.MULTIDAY_BLOCK_MIN_DAYS).
    block_start = now + timedelta(days=6)
    block_end = block_start + timedelta(days=4)
    events.append(_vevent("sim-block", block_start, block_end, "OFF"))

    # 5. A deliberately malformed title -- exercises the parse-error row in
    #    the debug table.
    bad = now + timedelta(days=10)
    events.append(_vevent(
        "sim-bad-title", bad, bad + timedelta(hours=1), "TBD - schedule not yet published",
    ))

    ics = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//flifo_board//simulated//EN\r\n"
    ics += "".join(events)
    ics += "END:VCALENDAR\r\n"
    return ics.encode("utf-8")


def generate_events(now: datetime | None = None) -> list[FlightEvent]:
    """Returns a fresh set of FlightEvent (FLIGHT, BLOCK, and a parse-error
    row) computed relative to `now`, so status/current/next stay correct as
    real time passes. Nothing here reads or writes storage."""
    now = now or datetime.now(timezone.utc)
    raw = _build_ics(now)
    return parse_calendar(raw, fetched_at=now, lookback_days=30, lookahead_days=30)
