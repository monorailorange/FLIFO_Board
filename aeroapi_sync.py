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

Cadence rules (current flight, phase-based on which OOOI fields AeroAPI has
already reported):
  1. Not yet departed: starts polling once within 15 minutes of the
     delay-adjusted estimated departure (or scheduled, if no delay is known
     yet) -- every 1 minute -- until actual_out appears.
  2. actual_out known, actual_off not yet: every 1 minute until actual_off.
  3. actual_off known, actual_on not yet: every 5 minutes until actual_on.
  4. actual_on known, actual_in not yet: every 1 minute until actual_in.
  5. actual_in known: fully resolved, stop polling this record.

The next flight ignores all of that and just polls every 15 minutes
(picking up estimated_out/estimated_in as they become available), regardless
of phase.
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
_CURRENT_SLOW_SECONDS = 5 * 60      # phase 3 (airborne)
_NEXT_FLIGHT_SECONDS = 15 * 60
_PRE_DEPARTURE_WINDOW = timedelta(minutes=15)


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


def _current_flight_cadence(flight: FlightEvent, now: datetime) -> Optional[int]:
    """Seconds between polls for `flight` as the CURRENT flight right now,
    or None if it isn't due to start yet (or is fully resolved)."""
    if flight.actual_in:
        return None  # phase 5: fully resolved
    if flight.actual_on:
        return _CURRENT_FAST_SECONDS   # phase 4
    if flight.actual_off:
        return _CURRENT_SLOW_SECONDS   # phase 3
    if flight.actual_out:
        return _CURRENT_FAST_SECONDS   # phase 2
    # phase 1: only once within 15 min of the best departure estimate we have
    trigger = flight.estimated_out or flight.dep_dt_utc
    if now >= trigger - _PRE_DEPARTURE_WINDOW:
        return _CURRENT_FAST_SECONDS
    return None


def _next_flight_cadence(flight: FlightEvent) -> Optional[int]:
    if flight.actual_in:
        return None  # already fully resolved -- shouldn't normally happen while still "next"
    return _NEXT_FLIGHT_SECONDS


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

    storage.update_aeroapi_fields(
        db_path,
        flight.occurrence_key,
        actual_out=result["actual_out"],
        actual_off=result["actual_off"],
        actual_on=result["actual_on"],
        actual_in=result["actual_in"],
        estimated_out=result["estimated_out"],
        estimated_in=result["estimated_in"],
        departure_delay_sec=result["departure_delay_sec"],
        arrival_delay_sec=result["arrival_delay_sec"],
        aeroapi_status=status_str,
        aeroapi_updated_at=now,
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
        targets.append((state.current, _current_flight_cadence(state.current, now)))
    if state.next is not None and state.next.event_type == "FLIGHT":
        targets.append((state.next, _next_flight_cadence(state.next)))

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
