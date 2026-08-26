"""
Builds manually-entered flight/block records for the debug view's "add a
record" forms. These never touch the ICS feed or its parsing window --
they're constructed directly and handed to storage.save_events() like any
other FlightEvent, just tagged source="MANUAL" so they (and only they) can
later be deleted -- see storage.delete_manual_event().

A manual flight typed as a title is deliberately run through the exact
same title parser the ICS feed uses (parser.build_flight_event), so it's
held to the same format and gets the same airport-timezone resolution --
no separate, looser validation path to keep in sync by hand. When AeroAPI
is the active status source, add_manual_flight_via_aeroapi() offers a
second path: flight number + departure station + date, with AeroAPI
supplying everything else.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aeroapi_client
from models import FlightEvent
from parser import build_flight_event, lookup_airport_tz, normalize_flight_number


def add_manual_flight(
    title: str,
    dep_gate: Optional[str] = None,
    arr_gate: Optional[str] = None,
) -> FlightEvent:
    """Parse `title` (same format as an ICS SUMMARY, e.g.
    "UA123 SFO 19Aug 0830 - ORD 19Aug 1420") into a FlightEvent. Raises
    parser.FlightParseError if it doesn't match -- let that propagate to
    the caller so the same message shown for bad ICS titles applies here.
    """
    title = (title or "").strip()
    now = datetime.now(timezone.utc)
    uid = f"manual-{uuid.uuid4().hex}"

    event = build_flight_event(
        summary=title,
        uid=uid,
        occurrence_anchor=now,
        ics_dtstart=None,
        ics_dtend=None,
        ics_last_modified=None,
        fetched_at=now,
    )
    event.source = "MANUAL"
    event.dep_gate = (dep_gate or "").strip() or None
    event.arr_gate = (arr_gate or "").strip() or None
    return event


def add_manual_flight_via_aeroapi(
    ident: str,
    dep_station: str,
    dep_date: date,
    api_key: str,
    dep_gate: Optional[str] = None,
    arr_gate: Optional[str] = None,
) -> FlightEvent:
    """
    Look the flight up on AeroAPI by flight number + departure station +
    departure date (station+date disambiguate cases where the same number
    flies more than once on the same day) and build a full FlightEvent from
    the result -- route, scheduled times, and whatever live OOOI/delay/
    status data AeroAPI already has, all in one shot, instead of typing out
    the title-string format by hand.

    Raises ValueError if nothing matches (bad ident/station/date, or the
    flight isn't in AeroAPI's window yet) or if an airport code AeroAPI
    returns isn't one this app's timezone lookup recognizes.
    Raises aeroapi_client.AeroApiError for actual request failures.
    """
    ident = normalize_flight_number((ident or "").strip())
    dep_station = (dep_station or "").strip().upper()
    if not ident:
        raise ValueError("A flight number is required.")
    if not dep_station:
        raise ValueError("A departure station is required to disambiguate same-day flight numbers.")

    result = aeroapi_client.find_flight_for_new_record(ident, dep_station, dep_date, api_key)
    if result is None:
        raise ValueError(
            f"No AeroAPI match for {ident} departing {dep_station} on {dep_date.isoformat()}. "
            "Check the flight number, station, and date -- AeroAPI may also not have this "
            "flight in its window yet (too far in the future)."
        )

    dep_code = result["dep_code"]
    arr_code = result["arr_code"]
    if not dep_code or not arr_code:
        raise ValueError("AeroAPI's match was missing an origin/destination airport code.")

    dep_tz_name = lookup_airport_tz(dep_code)
    arr_tz_name = lookup_airport_tz(arr_code)
    dep_tz = ZoneInfo(dep_tz_name)
    arr_tz = ZoneInfo(arr_tz_name)

    dep_dt_utc = result["dep_dt_utc"]
    arr_dt_utc = result["arr_dt_utc"]
    dep_dt_local = dep_dt_utc.astimezone(dep_tz)
    arr_dt_local = arr_dt_utc.astimezone(arr_tz)

    now = datetime.now(timezone.utc)
    uid = f"manual-{uuid.uuid4().hex}"
    flight_ident = result.get("ident") or ident
    raw_summary = (
        f"{flight_ident} {dep_code} {dep_dt_local.strftime('%d%b')} {dep_dt_local.strftime('%H%M')}"
        f" - {arr_code} {arr_dt_local.strftime('%d%b')} {arr_dt_local.strftime('%H%M')}"
    )

    event = FlightEvent(
        uid=uid,
        occurrence_key=f"{uid}|{dep_dt_local.isoformat()}",
        raw_summary=raw_summary,
        flight_number=flight_ident,
        dep_code=dep_code,
        arr_code=arr_code,
        dep_tz=dep_tz_name,
        arr_tz=arr_tz_name,
        dep_dt_local=dep_dt_local,
        arr_dt_local=arr_dt_local,
        dep_dt_utc=dep_dt_utc,
        arr_dt_utc=arr_dt_utc,
        ics_dtstart=None,
        ics_dtend=None,
        ics_last_modified=None,
        fetched_at=now,
        parse_ok=True,
        parse_error=None,
        event_type="FLIGHT",
        source="MANUAL",
        dep_gate=(dep_gate or "").strip() or result.get("dep_gate") or None,
        arr_gate=(arr_gate or "").strip() or result.get("arr_gate") or None,
        # Seed the live fields immediately from this same lookup, rather
        # than waiting for the next scheduled AeroAPI poll to fill them in.
        actual_out=result["actual_out"],
        actual_off=result["actual_off"],
        actual_on=result["actual_on"],
        actual_in=result["actual_in"],
        estimated_out=result["estimated_out"],
        estimated_in=result["estimated_in"],
        estimated_on=result["estimated_on"],
        departure_delay_sec=result["departure_delay_sec"],
        arrival_delay_sec=result["arrival_delay_sec"],
        aeroapi_status=("Cancelled" if result["cancelled"] else "Diverted" if result["diverted"] else result["status"]),
        aeroapi_updated_at=now,
    )
    return event


def add_manual_block(code: str, start_date: date, end_date: date) -> FlightEvent:
    """Build a BLOCK record spanning [start_date, end_date] inclusive.
    Unlike the ICS parser's multi-day heuristic, there's no minimum-duration
    check -- this is an explicit, deliberate action, not a title-format
    fallback guess."""
    code = (code or "").strip()
    if not code:
        raise ValueError("A code is required (e.g. OFF, VAC, RSV).")
    if end_date < start_date:
        raise ValueError("End date must be on or after the start date.")

    now = datetime.now(timezone.utc)
    uid = f"manual-{uuid.uuid4().hex}"
    dep_dt = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    # Exclusive end (end_date + 1 day), matching how the parser treats an
    # all-day ICS DTEND -- so a single-day block still spans that whole day.
    arr_dt = datetime.combine(end_date, time.min, tzinfo=timezone.utc) + timedelta(days=1)

    occurrence_key = f"{uid}|block|{dep_dt.isoformat()}"
    return FlightEvent(
        uid=uid,
        occurrence_key=occurrence_key,
        raw_summary=code,
        flight_number="",
        dep_code="",
        arr_code="",
        dep_tz="",
        arr_tz="",
        dep_dt_local=dep_dt,
        arr_dt_local=arr_dt,
        dep_dt_utc=dep_dt,
        arr_dt_utc=arr_dt,
        ics_dtstart=None,
        ics_dtend=None,
        ics_last_modified=None,
        fetched_at=now,
        parse_ok=True,
        parse_error=None,
        event_type="BLOCK",
        block_code=code,
        source="MANUAL",
    )
