"""
Picks the "current" and "next" flight out of the full parsed schedule.

Rule (collapses the two stated cases into one): a flight is considered
"expired" once ARRIVAL_GRACE_MINUTES have passed since its arrival. The
current flight is the earliest (by departure time) non-expired flight in
the schedule -- which is naturally either the first flight of today (if none
has started yet), a flight currently in the air/at the gate, or the first
flight of a future date once every flight for today has expired. The next
flight is the first non-expired flight after that -- not just whatever's
immediately next by departure time. Those two used to always coincide under
schedule-only timing (if flight N isn't expired, flight N+1 -- departing
later -- can't be either), but a long-spanning BLOCK breaks that: it can
stay current for weeks while an ordinary flight chronologically sandwiched
inside that window has already itself expired (e.g. its actual_in landed-
and-arrived time has already passed grace) -- skip forward past those too,
or "next" gets stuck forever pointing at an already-arrived flight.

`use_live_times=True` (AeroAPI mode) bases the expiry/grace calculation on
FlightEvent.effective_arr_dt_utc() instead of the raw scheduled arrival --
actual_in once landed-and-at-the-gate; otherwise the delay-adjusted
estimated_in once AeroAPI has reported a delay; otherwise the scheduled
time. This matters well before a flight lands, not just after: without it,
a flight running late enough that (scheduled_arrival + grace) has already
passed -- while still genuinely airborne, with no actual_in yet -- would
silently drop out of "current"/"next" and stop being polled altogether
(nothing left in scope to ever fetch actual_on/actual_in for it). Falls
back to the scheduled time when no live data exists yet, or in Local
Timing mode where live data is ignored entirely.

A BLOCK ("No Flights Today: X") normally stays current through its own
declared date span. But that span is set by whatever the source calendar
says (or a manual entry), and can run right up against -- or, via a
reroute/reserve callout, straight through -- the pilot's actual next
flying assignment. A block must never keep claiming "no flights today" on
a day (or past a point) that's actually a flying day, so its effective end
is clamped to whichever comes later of two things: one hour before that
next assignment departs, or midnight (departure-station-local) commencing
the calendar date it departs on. Midnight is a hard floor specifically for
early report times: a flight departing 00:05 would otherwise get a full
hour of lead time that bleeds backward into the *previous* calendar date's
block, incorrectly telling the pilot "no flights today" on a day that
starts with a flight 5 minutes in. See _next_flight_cutoff().
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from models import FlightEvent


@dataclass
class BoardState:
    current: Optional[FlightEvent]
    next: Optional[FlightEvent]
    generated_at: datetime


def _expiry_basis(flight: FlightEvent, use_live_times: bool) -> datetime:
    if use_live_times:
        return flight.effective_arr_dt_utc()
    return flight.arr_dt_utc


def _pre_report_cutoff(dep_utc: datetime, dep_tz_name: str) -> datetime:
    """The later of (departure - 1 hour) or departure-station-local
    midnight commencing departure's calendar date -- see module docstring
    for why "later" (not "earlier") is what makes the 00:05-report case
    work out to a 5-minute lead time instead of an hour bleeding into the
    prior day."""
    try:
        tz = ZoneInfo(dep_tz_name) if dep_tz_name else timezone.utc
    except Exception:
        tz = timezone.utc
    local_dep = dep_utc.astimezone(tz)
    midnight_utc = local_dep.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    one_hour_prior = dep_utc - timedelta(hours=1)
    return max(midnight_utc, one_hour_prior)


def _next_flight_cutoff(
    ordered: list[FlightEvent],
    idx: int,
    use_live_times: bool,
    now: datetime,
    grace_minutes: int,
) -> Optional[datetime]:
    """Scans forward from `ordered[idx]` (a BLOCK) for the pilot's actual
    next flying assignment -- skipping over any further BLOCKs, *and* any
    FLIGHT that's already expired -- and returns its pre-report cutoff, or
    None if nothing still-upcoming follows at all (the ordinary case: the
    block just ends on its own schedule).

    The expired-flight skip matters because this app deliberately never
    deletes history (see "Browsing flight history" in the README): a
    manually-added test flight, or any other already-completed flight,
    can easily sit chronologically *inside* a block's declared span. Such
    a flight isn't the pilot's next assignment -- it's already in the
    past -- so it must never be allowed to clamp the block's cutoff back
    into the past too, which would otherwise make an actually-still-valid
    block expire immediately the moment it's evaluated at all.
    """
    for later in ordered[idx + 1:]:
        if later.event_type != "FLIGHT":
            continue
        if now >= _expiry_basis(later, use_live_times) + timedelta(minutes=grace_minutes):
            continue  # already happened -- not a real "next assignment"
        dep = later.effective_dep_dt_utc() if use_live_times else later.dep_dt_utc
        return _pre_report_cutoff(dep, later.dep_tz)
    return None


def select_current_and_next(
    flights: list[FlightEvent],
    now: datetime,
    grace_minutes: int = 15,
    use_live_times: bool = False,
) -> BoardState:
    ordered = sorted(flights, key=lambda f: f.dep_dt_utc)

    def is_expired(flight: FlightEvent, idx: int) -> bool:
        natural_expiry = _expiry_basis(flight, use_live_times) + timedelta(minutes=grace_minutes)
        if flight.event_type == "BLOCK":
            cutoff = _next_flight_cutoff(ordered, idx, use_live_times, now, grace_minutes)
            if cutoff is not None:
                # Whichever comes first actually ends the block's display --
                # its own natural end (with the usual grace), or the next
                # assignment's pre-report cutoff clamping it short.
                return now >= min(natural_expiry, cutoff)
        return now >= natural_expiry

    current: Optional[FlightEvent] = None
    current_idx: Optional[int] = None

    for i, flight in enumerate(ordered):
        if not is_expired(flight, i):
            current = flight
            current_idx = i
            break

    next_flight = None
    if current_idx is not None:
        for i in range(current_idx + 1, len(ordered)):
            if not is_expired(ordered[i], i):
                next_flight = ordered[i]
                break

    return BoardState(current=current, next=next_flight, generated_at=now)
