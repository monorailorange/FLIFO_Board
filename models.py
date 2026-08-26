"""Data model for a single parsed flight event."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class FlightEvent:
    # Stable identity for a specific occurrence (recurring events get one
    # row per occurrence, keyed on uid + departure time).
    uid: str
    occurrence_key: str

    raw_summary: str
    flight_number: str

    dep_code: str
    arr_code: str

    dep_tz: str
    arr_tz: str

    # Timezone-aware datetimes.
    dep_dt_local: datetime
    arr_dt_local: datetime
    dep_dt_utc: datetime
    arr_dt_utc: datetime

    # Raw ICS bookkeeping, kept for the debug calendar view.
    ics_dtstart: Optional[datetime]
    ics_dtend: Optional[datetime]
    ics_last_modified: Optional[datetime]

    fetched_at: datetime

    parse_ok: bool = True
    parse_error: Optional[str] = None

    # "FLIGHT" (normal parsed flight) or "BLOCK" (a multi-day non-flight
    # entry -- day off, reserve, vacation, etc -- identified by a short code
    # rather than a flight-shaped title). See parser.MULTIDAY_BLOCK_MIN_DAYS.
    event_type: str = "FLIGHT"
    block_code: Optional[str] = None

    # Gate assignments. The ICS feed has no gate data today -- this is
    # reserved space for a future live gate-assignment API. Until that's
    # wired in, these stay None and the board renders "No Gate".
    dep_gate: Optional[str] = None
    arr_gate: Optional[str] = None

    # "ICS" (came from the subscribed calendar feed) or "MANUAL" (added by
    # hand from the debug view -- see manual_entry.py). Only MANUAL rows can
    # ever be deleted; storage.delete_manual_event() enforces this at the
    # SQL level, not just in the UI.
    source: str = "ICS"

    # Live data from FlightAware AeroAPI (see aeroapi_client.py /
    # aeroapi_sync.py). All None until a poll actually fills them in --
    # ICS-only ("Local Timing" mode) never touches these. OOOI = the
    # standard aviation-ops sequence: Out (leaves the gate), Off (wheels
    # up), On (wheels down), In (reaches the gate).
    actual_out: Optional[datetime] = None
    actual_off: Optional[datetime] = None
    actual_on: Optional[datetime] = None
    actual_in: Optional[datetime] = None
    estimated_out: Optional[datetime] = None
    estimated_in: Optional[datetime] = None
    # Estimated touchdown -- distinct from estimated_in (estimated *gate*
    # arrival, which includes taxi-in time on top). Used to trigger the
    # fast pre-touchdown polling cadence against the right reference point
    # (see aeroapi_sync._cadence_for()) instead of approximating off of
    # estimated_in, which can be meaningfully later.
    estimated_on: Optional[datetime] = None
    departure_delay_sec: Optional[int] = None
    arrival_delay_sec: Optional[int] = None
    aeroapi_status: Optional[str] = None       # raw status string from AeroAPI, for debugging
    aeroapi_updated_at: Optional[datetime] = None  # last successful poll for this record

    # The full status vocabulary the board's pills understand (see
    # static/style.css's .status-* rules). ON TIME / EN ROUTE / ARRIVED are
    # derivable from a static schedule (see status() below); DELAYED,
    # CANCELLED, TAXIING/LEFT GATE, DIVERTED, and LANDED all require live
    # AeroAPI data -- see aeroapi_status() below and aeroapi_sync.py.
    STATUSES = (
        "ON TIME", "TAXIING/LEFT GATE", "EN ROUTE", "DELAYED", "LANDED",
        "ARRIVED", "CANCELLED", "DIVERTED",
    )

    def status(self, now: datetime) -> str:
        """
        Schedule-based status ("Local Timing" mode). No live ops feed, so
        this can only place a flight into one of three schedule-derived
        buckets -- it has no way to know about delays, cancellations,
        diversions, or gate/taxi state. See live_status() for the
        AeroAPI-aware equivalent.
        """
        if now < self.dep_dt_utc:
            return "ON TIME"
        if now < self.arr_dt_utc:
            return "EN ROUTE"
        return "ARRIVED"

    def live_status(self, now: datetime) -> str:
        """
        AeroAPI-aware status, following the OOOI sequence actually reported
        for this flight rather than the published schedule. Falls back to
        status() wherever AeroAPI hasn't reported that stage yet.
        """
        raw = (self.aeroapi_status or "").lower()
        if "cancel" in raw:
            return "CANCELLED"
        if "divert" in raw:
            return "DIVERTED"
        if self.actual_in:
            return "ARRIVED"
        if self.actual_on:
            return "LANDED"
        if self.actual_off:
            return "EN ROUTE"
        if self.actual_out:
            return "TAXIING/LEFT GATE"
        if self.departure_delay_sec:
            return "DELAYED"
        return self.status(now)

    def effective_dep_dt_utc(self) -> datetime:
        """Departure time to display in AeroAPI mode: the actual gate-out
        time once it happened; otherwise the delay-adjusted estimate once a
        delay is known; otherwise the originally published time."""
        if self.actual_out:
            return self.actual_out
        if self.departure_delay_sec is not None and self.estimated_out:
            return self.estimated_out
        return self.dep_dt_utc

    def effective_arr_dt_utc(self) -> datetime:
        """Arrival time to display in AeroAPI mode, mirroring
        effective_dep_dt_utc() -- actual_in > delay-adjusted estimate >
        published time."""
        if self.actual_in:
            return self.actual_in
        if self.arrival_delay_sec is not None and self.estimated_in:
            return self.estimated_in
        return self.arr_dt_utc

    def to_dict(self) -> dict:
        d = asdict(self)
        for key, value in d.items():
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        return d
