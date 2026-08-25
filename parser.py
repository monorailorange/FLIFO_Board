"""
Parses the raw .ics payload into FlightEvent objects.

Expected VEVENT SUMMARY format (times are local to each airport):

    UA123 SFO 19Aug 0830 - ORD 19Aug 1420

  FLIGHT_NUMBER DEP_CODE DEP_DATE{DDMmm} DEP_TIME - ARR_CODE ARR_DATE{DDMmm} ARR_TIME
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

import airportsdata
import recurring_ical_events
from icalendar import Calendar

from models import FlightEvent

logger = logging.getLogger(__name__)

_IATA_AIRPORTS = airportsdata.load("IATA")
_ICAO_AIRPORTS = airportsdata.load("ICAO")

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

TITLE_PATTERN = re.compile(
    r"""^\s*
    (?P<flight_number>[A-Za-z0-9]+)\s+
    (?P<dep_code>[A-Za-z]{3,4})\s+
    (?P<dep_date>\d{1,2}[A-Za-z]{3})\s+
    (?P<dep_time>\d{1,2}:?\d{2})\s*
    -\s*
    (?P<arr_code>[A-Za-z]{3,4})\s+
    (?P<arr_date>\d{1,2}[A-Za-z]{3})\s+
    (?P<arr_time>\d{1,2}:?\d{2})
    \s*\.?\s*$
    """,
    re.VERBOSE,
)

# Keywords that, if present in DESCRIPTION, override the schedule-inferred
# status. This is a forward-compatible hook: the feed is schedule-only today,
# but if the host later surfaces real ops status this is where to wire it in.
_STATUS_KEYWORDS = ("CANCELLED", "CANCELED", "DELAYED", "DIVERTED", "ARRIVED", "DEPARTED")

# A non-flight-shaped event spanning at least this many elapsed days is
# treated as a schedule "block" (day off, reserve, vacation, etc) rather
# than a parse failure. Real flights never span this long, so this is safe
# to key off elapsed duration rather than trying to whitelist known codes.
MULTIDAY_BLOCK_MIN_DAYS = 2

# Crew Scheduling's subscribed calendar gives flight titles with a bare
# flight number ("1206 TPA 12Aug 1738 - EWR 12Aug 2035"), no carrier code
# -- since it's this pilot's own airline's calendar, every flight on it is
# inherently DEFAULT_CARRIER_CODE's. AeroAPI's /flights/{ident} lookup
# needs a carrier-qualified ident ("UA1206") to know which airline's
# flight 1206 to match, though -- a bare number could match any carrier's,
# or nothing at all. This app is built around one specific airline (see
# the hardcoded ua_white.png airline column, "UNITED AIRLINES" branding,
# etc), so hardcoding the prefix here is consistent with that rather than
# a config knob nothing else in the app treats as pluggable.
DEFAULT_CARRIER_CODE = "UA"


class FlightParseError(ValueError):
    pass


def normalize_flight_number(raw: str) -> str:
    """Uppercases and, if it's bare digits with no carrier prefix at all,
    prepends DEFAULT_CARRIER_CODE -- see the constant's comment above.
    Anything already carrying a letter prefix (a manually-typed "UA1526",
    a codeshare like "AA1526", etc) is left exactly as given. Public: also
    used by manual_entry.py for the "Add Flight (via AeroAPI)" form's
    typed flight number, so a bare "593" there gets the same treatment
    before it's used as the AeroAPI lookup ident."""
    value = raw.upper()
    return f"{DEFAULT_CARRIER_CODE}{value}" if value.isdigit() else value


def lookup_airport_tz(code: str) -> str:
    """Public: also used by manual_entry.py for AeroAPI-sourced records,
    which arrive as structured (code, datetime) pairs rather than a title
    string to run through parse_summary()."""
    code = code.upper()
    info = _IATA_AIRPORTS.get(code)
    if not info and len(code) == 4:
        info = _ICAO_AIRPORTS.get(code)
    if not info:
        raise FlightParseError(f"Unknown airport code: {code}")
    return info["tz"]


def _closest_year_date(month: int, day: int, anchor: date) -> date:
    """Pick whichever of anchor.year-1/anchor.year/anchor.year+1 puts this
    month/day closest to the anchor date. Handles ICS DTSTART landing on
    the "wrong" side of a year boundary relative to the title's date."""
    best = None
    best_delta = None
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # e.g. Feb 29 in a non-leap year
        delta = abs((candidate - anchor).days)
        if best_delta is None or delta < best_delta:
            best, best_delta = candidate, delta
    if best is None:
        raise FlightParseError(f"Invalid date {month:02d}/{day:02d}")
    return best


def _parse_ddmmm(text: str) -> tuple[int, int]:
    m = re.match(r"^(\d{1,2})([A-Za-z]{3})$", text)
    if not m:
        raise FlightParseError(f"Bad date token: {text!r}")
    day = int(m.group(1))
    mon_key = m.group(2).lower()
    if mon_key not in _MONTHS:
        raise FlightParseError(f"Unrecognized month abbreviation: {m.group(2)!r}")
    return _MONTHS[mon_key], day


def _parse_hhmm(text: str) -> tuple[int, int]:
    digits = text.replace(":", "")
    digits = digits.zfill(4)
    if len(digits) != 4 or not digits.isdigit():
        raise FlightParseError(f"Bad time token: {text!r}")
    hour, minute = int(digits[:2]), int(digits[2:])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise FlightParseError(f"Time out of range: {text!r}")
    return hour, minute


def parse_summary(summary: str, anchor: date) -> dict:
    """Parse a SUMMARY string into raw fields (no timezone resolution yet)."""
    match = TITLE_PATTERN.match(summary or "")
    if not match:
        raise FlightParseError(f"Title does not match expected format: {summary!r}")
    fields = match.groupdict()

    dep_month, dep_day = _parse_ddmmm(fields["dep_date"])
    dep_date = _closest_year_date(dep_month, dep_day, anchor)
    dep_hour, dep_minute = _parse_hhmm(fields["dep_time"])

    arr_month, arr_day = _parse_ddmmm(fields["arr_date"])
    # Arrival year: same year as departure unless that would put the
    # arrival date before the departure date (i.e. it wrapped into the
    # next year, e.g. a New Year's Eve red-eye).
    arr_date = date(dep_date.year, arr_month, arr_day) if _valid_date(dep_date.year, arr_month, arr_day) else None
    if arr_date is None or arr_date < dep_date:
        arr_date = date(dep_date.year + 1, arr_month, arr_day)
    arr_hour, arr_minute = _parse_hhmm(fields["arr_time"])

    return {
        "flight_number": normalize_flight_number(fields["flight_number"]),
        "dep_code": fields["dep_code"].upper(),
        "arr_code": fields["arr_code"].upper(),
        "dep_date": dep_date,
        "dep_hour": dep_hour,
        "dep_minute": dep_minute,
        "arr_date": arr_date,
        "arr_hour": arr_hour,
        "arr_minute": arr_minute,
    }


def _valid_date(year: int, month: int, day: int) -> bool:
    try:
        date(year, month, day)
        return True
    except ValueError:
        return False


def build_flight_event(
    summary: str,
    uid: str,
    occurrence_anchor: datetime,
    ics_dtstart: Optional[datetime],
    ics_dtend: Optional[datetime],
    ics_last_modified: Optional[datetime],
    fetched_at: datetime,
) -> FlightEvent:
    anchor_date = (ics_dtstart or occurrence_anchor).date()
    fields = parse_summary(summary, anchor_date)

    dep_tz_name = lookup_airport_tz(fields["dep_code"])
    arr_tz_name = lookup_airport_tz(fields["arr_code"])
    dep_tz = ZoneInfo(dep_tz_name)
    arr_tz = ZoneInfo(arr_tz_name)

    dep_dt_local = datetime(
        fields["dep_date"].year, fields["dep_date"].month, fields["dep_date"].day,
        fields["dep_hour"], fields["dep_minute"], tzinfo=dep_tz,
    )
    arr_dt_local = datetime(
        fields["arr_date"].year, fields["arr_date"].month, fields["arr_date"].day,
        fields["arr_hour"], fields["arr_minute"], tzinfo=arr_tz,
    )

    occurrence_key = f"{uid}|{dep_dt_local.isoformat()}"

    return FlightEvent(
        uid=uid,
        occurrence_key=occurrence_key,
        raw_summary=summary,
        flight_number=fields["flight_number"],
        dep_code=fields["dep_code"],
        arr_code=fields["arr_code"],
        dep_tz=dep_tz_name,
        arr_tz=arr_tz_name,
        dep_dt_local=dep_dt_local,
        arr_dt_local=arr_dt_local,
        dep_dt_utc=dep_dt_local.astimezone(timezone.utc),
        arr_dt_utc=arr_dt_local.astimezone(timezone.utc),
        ics_dtstart=ics_dtstart,
        ics_dtend=ics_dtend,
        ics_last_modified=ics_last_modified,
        fetched_at=fetched_at,
        parse_ok=True,
        parse_error=None,
    )


def _is_multiday_span(dtstart: Optional[datetime], dtend: Optional[datetime]) -> bool:
    if dtstart is None or dtend is None:
        return False
    elapsed_days = (dtend - dtstart).total_seconds() / 86400
    return elapsed_days >= MULTIDAY_BLOCK_MIN_DAYS


def _make_block_event(summary: str, uid: str, dtstart: datetime, dtend: datetime,
                       ics_last_modified: Optional[datetime], fetched_at: datetime) -> FlightEvent:
    code = (summary or "").strip()
    occurrence_key = f"{uid}|block|{dtstart.isoformat()}"
    return FlightEvent(
        uid=uid,
        occurrence_key=occurrence_key,
        raw_summary=summary,
        flight_number="",
        dep_code="",
        arr_code="",
        dep_tz="",
        arr_tz="",
        dep_dt_local=dtstart,
        arr_dt_local=dtend,
        dep_dt_utc=dtstart,
        arr_dt_utc=dtend,
        ics_dtstart=dtstart,
        ics_dtend=dtend,
        ics_last_modified=ics_last_modified,
        fetched_at=fetched_at,
        parse_ok=True,
        parse_error=None,
        event_type="BLOCK",
        block_code=code,
    )


def _make_error_event(summary: str, uid: str, error: str, fetched_at: datetime,
                       ics_dtstart: Optional[datetime] = None,
                       ics_dtend: Optional[datetime] = None,
                       ics_last_modified: Optional[datetime] = None) -> FlightEvent:
    """A placeholder row so unparseable events still show up in the debug
    calendar view instead of silently vanishing."""
    now = fetched_at
    occurrence_key = f"{uid}|error|{uuid.uuid4().hex[:8]}"
    return FlightEvent(
        uid=uid,
        occurrence_key=occurrence_key,
        raw_summary=summary,
        flight_number="",
        dep_code="",
        arr_code="",
        dep_tz="",
        arr_tz="",
        dep_dt_local=now,
        arr_dt_local=now,
        dep_dt_utc=now,
        arr_dt_utc=now,
        ics_dtstart=ics_dtstart,
        ics_dtend=ics_dtend,
        ics_last_modified=ics_last_modified,
        fetched_at=fetched_at,
        parse_ok=False,
        parse_error=error,
    )


def _as_datetime(value) -> Optional[datetime]:
    """icalendar can hand back date or datetime objects; normalize to
    tz-aware datetime (assume UTC for naive values / bare dates)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def parse_calendar(
    raw_ics: bytes,
    fetched_at: datetime,
    lookback_days: int = 14,
    lookahead_days: int = 120,
) -> list[FlightEvent]:
    """Parse raw .ics bytes into a list of FlightEvent (including failed
    parses as parse_ok=False rows, for the debug view)."""
    calendar = Calendar.from_ical(raw_ics)

    window_start = fetched_at - timedelta(days=lookback_days)
    window_end = fetched_at + timedelta(days=lookahead_days)

    occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)

    events: list[FlightEvent] = []
    for component in occurrences:
        summary = str(component.get("SUMMARY", ""))
        uid = str(component.get("UID", "")) or f"no-uid-{uuid.uuid4().hex[:8]}"
        dtstart = _as_datetime(component.get("DTSTART").dt if component.get("DTSTART") else None)
        dtend = _as_datetime(component.get("DTEND").dt if component.get("DTEND") else None)
        last_modified = _as_datetime(
            component.get("LAST-MODIFIED").dt if component.get("LAST-MODIFIED") else None
        )

        if TITLE_PATTERN.match(summary or ""):
            # Flight-shaped title -- parse it as a flight. A failure here
            # (e.g. unrecognized airport code) is a genuine parse error,
            # regardless of how long the event spans.
            try:
                events.append(build_flight_event(
                    summary=summary,
                    uid=uid,
                    occurrence_anchor=dtstart or fetched_at,
                    ics_dtstart=dtstart,
                    ics_dtend=dtend,
                    ics_last_modified=last_modified,
                    fetched_at=fetched_at,
                ))
            except FlightParseError as exc:
                logger.warning("Could not parse event %r: %s", summary, exc)
                events.append(_make_error_event(
                    summary, uid, str(exc), fetched_at, dtstart, dtend, last_modified,
                ))
        elif _is_multiday_span(dtstart, dtend):
            # Not flight-shaped, but spans multiple days -- a schedule block
            # (day off, reserve, vacation, etc), not a parse failure.
            events.append(_make_block_event(summary, uid, dtstart, dtend, last_modified, fetched_at))
        else:
            error = f"Title does not match expected format: {summary!r}"
            logger.warning("Could not parse event %r: %s", summary, error)
            events.append(_make_error_event(
                summary, uid, error, fetched_at, dtstart, dtend, last_modified,
            ))

    return events
