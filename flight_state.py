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
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

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


def select_current_and_next(
    flights: list[FlightEvent],
    now: datetime,
    grace_minutes: int = 15,
    use_live_times: bool = False,
) -> BoardState:
    ordered = sorted(flights, key=lambda f: f.dep_dt_utc)

    def is_expired(flight: FlightEvent) -> bool:
        return now >= _expiry_basis(flight, use_live_times) + timedelta(minutes=grace_minutes)

    current: Optional[FlightEvent] = None
    current_idx: Optional[int] = None

    for i, flight in enumerate(ordered):
        if not is_expired(flight):
            current = flight
            current_idx = i
            break

    next_flight = None
    if current_idx is not None:
        for flight in ordered[current_idx + 1:]:
            if not is_expired(flight):
                next_flight = flight
                break

    return BoardState(current=current, next=next_flight, generated_at=now)
