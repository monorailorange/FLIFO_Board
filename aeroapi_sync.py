"""
Orchestrates AeroAPI polling: settings (status source + API key, editable
from the debug page without ever touching .env by hand) and the dynamic
per-flight polling cadence itself.

Settings persistence: the debug-page forms write straight to
storage.app_settings (SQLite) so reads stay fast and simple everywhere
else in the app. But that table is treated as disposable cache elsewhere
in this codebase (flifo.db is .gitignore'd; the whole ICS side is designed
to shrug off losing it, since a refresh just repopulates it) -- so it's
the wrong place for the *only* copy of real configuration. Every write
here also lands in .env (AEROAPI_API_KEY / AEROAPI_STATUS_SOURCE), and
ensure_settings_seeded() -- called once at app startup -- pulls from .env
back into app_settings if the database ever comes up empty (fresh install,
restored from a backup that predates the key, or the file was flat-out
lost). That's what survives a power outage / service restart even if
flifo.db doesn't.

Cadence rules are keyed off each flight's own OOOI progress, not which
board slot (current/next) it happens to occupy, except for the final
pre-departure tightening below -- see _cadence_for():
  1. Not yet departed, either slot: nothing at all until within
     _PRE_DEPARTURE_TRACKING_WINDOW (24h) of the delay-adjusted estimated
     departure (or scheduled, if no delay is known yet) -- gates/delays/
     estimates essentially never exist any earlier than that, and this
     caps the pre-departure query cost at a fixed, predictable per-flight
     amount instead of it scaling with however long a flight sits in
     scope beforehand (behind a long block, or simply as a "next" flight
     days out). Within that 24h window: every 60 minutes
     (_PRE_DEPARTURE_FAR_SECONDS) until within _PRE_DEPARTURE_MEDIUM_WINDOW
     (6h) of departure -- gates/delays essentially never appear that early
     either, so the first ~18 hours of the window would otherwise just be
     wasted queries -- then every 15 minutes, tightening to every 1 minute
     once within _PRE_DEPARTURE_WINDOW (15 min) of departure. Only that
     final tightening is current-slot-only, since imminent departure only
     matters for whichever flight is actually pinned at the top of the
     board right now; the 60min/15min tiers apply to both slots equally.
     Every flight passes through this same check regardless of slot as it
     approaches its own departure, so nothing is ever permanently
     skipped, just deferred until it's actually worth a query.
  2. actual_out known, actual_off not yet: every 1 minute until actual_off.
  3. actual_off known, actual_on not yet (airborne): every 15 minutes
     (_NEXT_FLIGHT_SECONDS, same constant the pre-departure medium tier
     reuses) until within _AIRBORNE_FAR_WINDOW (2h) of the delay-adjusted
     estimated touchdown (estimated_on -- not the later estimated_in,
     estimated *gate* arrival), then every 5 minutes, tightening to every
     1 minute once within _PRE_TOUCHDOWN_WINDOW (10 min) of touchdown --
     same staged-tightening idea as rule 1, applied to touchdown instead
     of departure. Without the final tightening, actual_on landing
     anywhere in the middle of the 5-minute gap between polls (which is
     most of the time) would sit undetected for however much of it was
     left; without the 2h-out coarsening, a long flight would poll every
     5 minutes for hours before there's anything to find. Only matters
     for longer flights -- a short hop is already inside the 2h window
     well before it's even airborne.
  4. actual_on known, actual_in not yet: every 1 minute until actual_in.
  5. actual_in known: fully resolved, stop polling this record.

Every poll (any phase above) also refreshes dep_gate/arr_gate whenever
AeroAPI reports one -- see aeroapi_client._extract_ooi_fields() and
_poll_one() below. A poll that doesn't report a gate never blanks out one
already on file (see _poll_one()'s dep_gate/arr_gate fallback), and
storage.save_events() deliberately never touches these columns on an
UPDATE, so a routine ICS refresh can't wipe one out either -- only a fresh
AeroAPI-reported value (or a brand-new manual entry) ever changes a gate
once it's set.

Rules 2-5 apply regardless of slot: a flight that's already departed keeps
its fast/phase-based cadence even if it ends up sitting in "next" -- which
happens whenever a long-spanning BLOCK is occupying "current" while an
ordinary flight, in progress, chronologically follows it. Without this, an
already-airborne flight parked in the "next" slot would get throttled to
the slow 15-minute "next" cadence meant for flights that haven't happened
yet, and something like actual_on could sit unpolled for a long stretch.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import set_key

import aeroapi_client
import config
import storage
from flight_state import select_current_and_next
from models import FlightEvent

logger = logging.getLogger(__name__)

_STATUS_SOURCE_KEY = "status_source"
_API_KEY_KEY = "aeroapi_api_key"
VALID_STATUS_SOURCES = ("LOCAL", "AEROAPI")
DEFAULT_STATUS_SOURCE = "LOCAL"

# The .env names these settings are mirrored under -- see module docstring.
_ENV_API_KEY_VAR = "AEROAPI_API_KEY"
_ENV_STATUS_SOURCE_VAR = "AEROAPI_STATUS_SOURCE"
_ENV_PATH = config.BASE_DIR / ".env"

_CURRENT_FAST_SECONDS = 60          # phases 1, 2, 4
_CURRENT_SLOW_SECONDS = 5 * 60      # phase 3 (airborne), within _AIRBORNE_FAR_WINDOW of touchdown
_NEXT_FLIGHT_SECONDS = 15 * 60      # the "medium" tier -- reused for both pre-departure and airborne
_PRE_DEPARTURE_WINDOW = timedelta(minutes=15)
# 10, not 15: this is keyed off estimated_on (estimated touchdown) now,
# not the later estimated_in (estimated gate arrival) -- a flight is very
# unlikely to touch down earlier than 10 minutes ahead of AeroAPI's own
# touchdown estimate, so there's little benefit to starting fast polling
# any earlier than that against this tighter, more accurate reference
# point. See _cadence_for()'s phase 3.
_PRE_TOUCHDOWN_WINDOW = timedelta(minutes=10)
# No polling at all for a not-yet-departed flight, in either slot, until
# within this long of its own departure -- caps the pre-departure query
# cost at a fixed per-flight amount instead of it scaling with however
# long a flight happens to sit in scope beforehand. See _cadence_for().
_PRE_DEPARTURE_TRACKING_WINDOW = timedelta(hours=24)
# Outer "far" tiers, coarser than the existing "medium" cadence, for the
# stretches of the 24h pre-departure window and the airborne phase where
# a change is genuinely unlikely -- gates/delays essentially never appear
# more than ~6h out, and nothing relevant to actual_on can happen more
# than a couple hours before touchdown regardless of how long the flight
# actually is. Cuts the pre-departure/airborne query cost substantially
# for flights that sit in scope a long time before departure, or fly a
# long time before landing, without touching the precision of anything
# closer to an actual OOOI transition. See _cadence_for().
_PRE_DEPARTURE_MEDIUM_WINDOW = timedelta(hours=6)
_PRE_DEPARTURE_FAR_SECONDS = 60 * 60
_AIRBORNE_FAR_WINDOW = timedelta(hours=2)


def _persist_to_env(var_name: str, value: str) -> None:
    """Best-effort mirror of a setting into .env, so it survives losing
    flifo.db entirely (power outage, fresh restore, etc). Never raises --
    a write failure here (read-only filesystem, permissions) just means
    reduced durability, not a broken save; the in-memory/SQLite value the
    user just set still takes effect immediately either way."""
    try:
        if not _ENV_PATH.exists():
            _ENV_PATH.touch()
        set_key(str(_ENV_PATH), var_name, value, quote_mode="always")
    except OSError:
        logger.warning(
            "Could not write %s to .env -- this setting won't survive a full "
            "restart if the database is also lost. Check file permissions on %s.",
            var_name, _ENV_PATH,
        )


def ensure_settings_seeded() -> None:
    """Called once at app startup. If app_settings doesn't have a value yet
    (fresh database, or one that lost this table) but .env does, seed
    app_settings from .env -- this is what actually recovers the AeroAPI
    key/status source after flifo.db is wiped or lost, e.g. across a power
    outage where the disk state is otherwise reset."""
    if not storage.get_setting(config.DB_PATH, _API_KEY_KEY):
        env_key = os.environ.get(_ENV_API_KEY_VAR, "").strip()
        if env_key:
            storage.set_setting(config.DB_PATH, _API_KEY_KEY, env_key)
            logger.info("Restored AeroAPI key from .env after an empty/fresh database.")

    if not storage.get_setting(config.DB_PATH, _STATUS_SOURCE_KEY):
        env_source = os.environ.get(_ENV_STATUS_SOURCE_VAR, "").strip().upper()
        if env_source in VALID_STATUS_SOURCES:
            storage.set_setting(config.DB_PATH, _STATUS_SOURCE_KEY, env_source)
            logger.info("Restored AeroAPI status source (%s) from .env after an empty/fresh database.", env_source)


def get_status_source() -> str:
    value = storage.get_setting(config.DB_PATH, _STATUS_SOURCE_KEY, DEFAULT_STATUS_SOURCE)
    return value if value in VALID_STATUS_SOURCES else DEFAULT_STATUS_SOURCE


def set_status_source(value: str) -> str:
    if value not in VALID_STATUS_SOURCES:
        raise ValueError(f"Invalid status source {value!r}; must be one of {VALID_STATUS_SOURCES}")
    storage.set_setting(config.DB_PATH, _STATUS_SOURCE_KEY, value)
    _persist_to_env(_ENV_STATUS_SOURCE_VAR, value)
    return value


def get_api_key() -> str:
    return storage.get_setting(config.DB_PATH, _API_KEY_KEY, "") or ""


def set_api_key(value: str) -> None:
    value = (value or "").strip()
    storage.set_setting(config.DB_PATH, _API_KEY_KEY, value)
    _persist_to_env(_ENV_API_KEY_VAR, value)


def clear_api_key() -> None:
    storage.set_setting(config.DB_PATH, _API_KEY_KEY, "")
    _persist_to_env(_ENV_API_KEY_VAR, "")


def masked_api_key() -> str:
    key = get_api_key()
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


def _cadence_for(flight: FlightEvent, is_current_slot: bool, now: datetime) -> Optional[int]:
    """
    Seconds between polls for `flight` right now, or None if it isn't due
    to start yet (or is fully resolved). Based on the flight's own OOOI
    progress, not which board slot it's sitting in -- a flight that's
    already in the air doesn't stop needing close tracking just because a
    long-spanning BLOCK is occupying the "current" slot and pushed it into
    "next". Only the pre-departure case (no actual_out yet) actually cares
    about slot: a not-yet-departed "current" flight gets the fast
    pre-departure-window gating below, while a genuinely-future "next"
    flight (hasn't happened yet, in either sense) just needs its estimates
    kept fresh on the slow fixed cadence -- that's the only place
    `is_current_slot` changes the answer.
    """
    if flight.actual_in:
        return None  # phase 5: fully resolved
    if flight.actual_on:
        return _CURRENT_FAST_SECONDS   # phase 4
    if flight.actual_off:
        # phase 3 (airborne): slow cadence for most of the flight -- no
        # point polling every minute while there are hours left in the
        # air -- but switch to the fast cadence once touchdown is close,
        # same pre-window pattern as phase 1's departure gating above.
        # Without this, actual_on could sit undetected for up to a full
        # _CURRENT_SLOW_SECONDS after it actually happens, since that's
        # deliberately a long interval; this bounds that lag to roughly
        # _CURRENT_FAST_SECONDS instead, right when it matters most.
        #
        # Triggered off estimated_on (estimated *touchdown*), not
        # estimated_in (estimated *gate* arrival, which runs later by
        # however long taxi-in takes) -- estimated_on is the right
        # reference point for anticipating actual_on specifically. Falls
        # back to the scheduled gate arrival only if no live estimate
        # exists yet at all (no better reference available).
        trigger = flight.estimated_on or flight.arr_dt_utc
        if now >= trigger - _PRE_TOUCHDOWN_WINDOW:
            return _CURRENT_FAST_SECONDS
        if now >= trigger - _AIRBORNE_FAR_WINDOW:
            return _CURRENT_SLOW_SECONDS
        # More than _AIRBORNE_FAR_WINDOW from touchdown -- nothing relevant
        # to actual_on can happen yet regardless of how long the flight
        # actually is; only matters for longer flights (a short hop is
        # already inside this window well before it's even airborne).
        return _NEXT_FLIGHT_SECONDS
    if flight.actual_out:
        return _CURRENT_FAST_SECONDS   # phase 2: already departed, regardless of slot

    # phase 1 (not yet departed, either slot): nothing at all until within
    # _PRE_DEPARTURE_TRACKING_WINDOW (24h) of the best departure estimate
    # we have -- gates/delays/estimates essentially never exist any
    # earlier than that, and every flight eventually passes through this
    # check as it approaches its own turn regardless of how long it sat
    # further back in the queue (behind a long block, or simply as a
    # "next" flight days out), so nothing is ever permanently skipped,
    # just deferred until it's actually worth a query. This caps the
    # per-flight AeroAPI cost at a fixed, predictable amount instead of
    # scaling with however long a flight happens to sit in scope before
    # its own departure -- see the query-budget discussion that led here.
    #
    # Within that 24h window: 60-minute cadence until within
    # _PRE_DEPARTURE_MEDIUM_WINDOW (6h) of departure -- gates/delays
    # essentially never appear any earlier than that either, so the first
    # ~18 hours of the window would otherwise just be wasted queries --
    # then 15 minutes, tightening to 1 minute once within the final
    # _PRE_DEPARTURE_WINDOW (15 min) of departure. That final tightening
    # only applies to the current-slot flight, since imminent departure
    # only matters for whichever flight is actually pinned at the top of
    # the board right now; the 60min/15min tiers apply to both slots.
    trigger = flight.estimated_out or flight.dep_dt_utc
    if now < trigger - _PRE_DEPARTURE_TRACKING_WINDOW:
        return None
    if is_current_slot and now >= trigger - _PRE_DEPARTURE_WINDOW:
        return _CURRENT_FAST_SECONDS
    if now >= trigger - _PRE_DEPARTURE_MEDIUM_WINDOW:
        return _NEXT_FLIGHT_SECONDS
    return _PRE_DEPARTURE_FAR_SECONDS


def _is_due(flight: FlightEvent, cadence_seconds: Optional[int], now: datetime) -> bool:
    if cadence_seconds is None:
        return False
    if flight.aeroapi_updated_at is None:
        return True
    return (now - flight.aeroapi_updated_at).total_seconds() >= cadence_seconds


def _poll_one(db_path: str, flight: FlightEvent, api_key: str, now: datetime, debug: bool) -> bool:
    """Polls `flight` unconditionally (no cadence check -- caller decides
    whether it's due) and writes any result. Returns True if AeroAPI had a
    matching flight to report on, False if it had nothing yet (not an
    error -- just nothing new)."""
    result = aeroapi_client.fetch_flight_status(
        flight.flight_number, api_key, flight.dep_dt_utc, debug=debug,
    )
    if result is None:
        logger.info("AeroAPI: no matching flight found yet for %s", flight.flight_number)
        return False

    status_str = result.get("status")
    if result.get("cancelled"):
        status_str = "Cancelled"
    elif result.get("diverted"):
        status_str = "Diverted"

    # Gates are opportunistic and can come and go in AeroAPI's response
    # (typically only published within ~24h of departure) -- a poll that
    # doesn't report one right now must not blank out a gate a previous
    # poll already found, so fall back to whatever's already on file.
    dep_gate = result.get("dep_gate") or flight.dep_gate
    arr_gate = result.get("arr_gate") or flight.arr_gate

    storage.update_aeroapi_fields(
        db_path,
        flight.occurrence_key,
        actual_out=result["actual_out"],
        actual_off=result["actual_off"],
        actual_on=result["actual_on"],
        actual_in=result["actual_in"],
        estimated_out=result["estimated_out"],
        estimated_in=result["estimated_in"],
        estimated_on=result["estimated_on"],
        departure_delay_sec=result["departure_delay_sec"],
        arrival_delay_sec=result["arrival_delay_sec"],
        aeroapi_status=status_str,
        aeroapi_updated_at=now,
        dep_gate=dep_gate,
        arr_gate=arr_gate,
    )
    logger.info("AeroAPI: updated %s (status=%s)", flight.flight_number, status_str)
    return True


def poll_flight_now(occurrence_key: str) -> tuple[bool, str]:
    """
    Force-polls one specific record right now, bypassing both the
    current/next scoping and the phase cadence entirely -- unlike
    sync_now(), which only ever looks at whoever select_current_and_next()
    currently considers current/next. Useful for testing, or for pulling a
    record back in that (for whatever reason -- including bugs like a stale
    expiry basis) fell out of that scope. Returns (ok, message).
    """
    if get_status_source() != "AEROAPI":
        return False, "Flight Info Source isn't set to AeroAPI."
    api_key = get_api_key()
    if not api_key:
        return False, "No AeroAPI key configured."

    flights = storage.get_valid_flight_events(config.DB_PATH)
    flight = next((f for f in flights if f.occurrence_key == occurrence_key), None)
    if flight is None:
        return False, "Record not found."
    if flight.event_type != "FLIGHT":
        return False, "Only flight records can be polled (not day-off/block entries)."

    now = datetime.now(timezone.utc)
    try:
        found = _poll_one(config.DB_PATH, flight, api_key, now, debug=config.ICS_DEBUG)
    except aeroapi_client.AeroApiError as exc:
        return False, str(exc)

    if found:
        return True, f"Polled {flight.flight_number} -- data updated."
    return True, f"Polled {flight.flight_number} -- AeroAPI has no matching flight to report yet."


def sync_now() -> dict:
    """One polling tick. Figures out who's current/next (live-time-aware),
    decides which of those are actually due under the phase cadence, and
    polls only those. Safe to call often -- it's a no-op whenever nothing
    is due. Returns a small summary dict, mainly for the manual "poll now"
    button and logging."""
    summary = {"polled": [], "skipped": [], "errors": []}

    if get_status_source() != "AEROAPI":
        return summary
    api_key = get_api_key()
    if not api_key:
        summary["errors"].append("No AeroAPI key configured")
        return summary

    now = datetime.now(timezone.utc)
    flights = storage.get_valid_flight_events(config.DB_PATH)
    state = select_current_and_next(flights, now, config.ARRIVAL_GRACE_MINUTES, use_live_times=True)

    targets: list[tuple[FlightEvent, Optional[int]]] = []
    if state.current is not None and state.current.event_type == "FLIGHT":
        targets.append((state.current, _cadence_for(state.current, is_current_slot=True, now=now)))
    if state.next is not None and state.next.event_type == "FLIGHT":
        targets.append((state.next, _cadence_for(state.next, is_current_slot=False, now=now)))

    for flight, cadence in targets:
        if not _is_due(flight, cadence, now):
            summary["skipped"].append(flight.flight_number)
            continue
        try:
            _poll_one(config.DB_PATH, flight, api_key, now, debug=config.ICS_DEBUG)
            summary["polled"].append(flight.flight_number)
        except aeroapi_client.AeroApiError as exc:
            logger.warning("AeroAPI poll failed for %s: %s", flight.flight_number, exc)
            summary["errors"].append(f"{flight.flight_number}: {exc}")

    return summary
