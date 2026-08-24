"""
Thin client for FlightAware AeroAPI (https://www.flightaware.com/commercial/aeroapi/).

Two lookups this app needs, both against /flights/{ident} (which returns a
list of recent/upcoming flights sharing that flight number -- airlines
reuse numbers across different days, and sometimes more than once on the
same day):

- fetch_flight_status(): given a flight we already have on file (with a
  known scheduled departure), find the matching AeroAPI record and pull its
  OOOI (Out/Off/On/In) times, delays, and status.
- find_flight_for_new_record(): given just a flight number, departure
  station, and departure date (the "add via AeroAPI" manual-entry form),
  find the one matching flight and pull enough to build a whole new record
  from scratch -- route, scheduled times, gates if AeroAPI has them, plus
  whatever live OOOI data already exists.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

AEROAPI_BASE_URL = "https://aeroapi.flightaware.com/aeroapi"


class AeroApiError(RuntimeError):
    pass


def _mask_key(key: str) -> str:
    if len(key) <= 8:
        return "*** (masked)"
    return f"{key[:4]}...{key[-4:]} (masked, {len(key)} chars)"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    # AeroAPI returns ISO8601 UTC timestamps like "2026-08-21T14:05:00Z".
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fetch_candidates(ident: str, api_key: str, timeout: int, debug: bool) -> list[dict]:
    """GET /flights/{ident} and return the raw `flights` list. Raises
    AeroApiError for real failures; an ident with no candidates at all just
    yields an empty list (not an error)."""
    if not api_key:
        raise AeroApiError("No AeroAPI key configured")
    if not ident:
        raise AeroApiError("No flight number to look up")

    url = f"{AEROAPI_BASE_URL}/flights/{ident}"
    headers = {"x-apikey": api_key, "Accept": "application/json"}

    if debug:
        logger.info("[AEROAPI_DEBUG] GET %s", url)
        logger.info("[AEROAPI_DEBUG]   x-apikey: %s", _mask_key(api_key))

    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise AeroApiError(f"AeroAPI request failed: {exc}") from exc

    if debug:
        logger.info("[AEROAPI_DEBUG] status: %s %s", response.status_code, response.reason)

    if response.status_code == 401:
        raise AeroApiError("AeroAPI rejected the key (401 Unauthorized) -- check the saved key")
    if response.status_code in (402, 403):
        raise AeroApiError(f"AeroAPI denied this request ({response.status_code}) -- "
                            "may be outside your plan's tier or you're out of credit")
    if response.status_code == 429:
        raise AeroApiError("AeroAPI rate limit hit (429) -- back off before retrying")
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise AeroApiError(f"AeroAPI request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise AeroApiError(f"AeroAPI returned non-JSON response: {exc}") from exc

    flights = payload.get("flights") or []
    if debug:
        logger.info("[AEROAPI_DEBUG] %d candidate flight(s) returned for %s", len(flights), ident)
    return flights


def _extract_ooi_fields(flight: dict) -> dict:
    """The OOOI/delay/status fields both lookups return."""
    return {
        "actual_out": _parse_dt(flight.get("actual_out")),
        "actual_off": _parse_dt(flight.get("actual_off")),
        "actual_on": _parse_dt(flight.get("actual_on")),
        "actual_in": _parse_dt(flight.get("actual_in")),
        "estimated_out": _parse_dt(flight.get("estimated_out")),
        "estimated_in": _parse_dt(flight.get("estimated_in")),
        "departure_delay_sec": flight.get("departure_delay"),
        "arrival_delay_sec": flight.get("arrival_delay"),
        "status": flight.get("status"),
        "cancelled": bool(flight.get("cancelled")),
        "diverted": bool(flight.get("diverted")),
    }


def _airport_code(airport: dict) -> str:
    return str(airport.get("code_iata") or airport.get("code") or airport.get("code_icao") or "")


def _airport_matches(airport: dict, wanted: str) -> bool:
    wanted = (wanted or "").strip().upper()
    if not wanted:
        return False
    candidates = {
        str(airport.get("code") or "").upper(),
        str(airport.get("code_iata") or "").upper(),
        str(airport.get("code_icao") or "").upper(),
    }
    return wanted in candidates


def _local_date(dt_utc: datetime, tzname: Optional[str]) -> date:
    if tzname:
        try:
            return dt_utc.astimezone(ZoneInfo(tzname)).date()
        except Exception:
            pass
    return dt_utc.date()


def fetch_flight_status(
    ident: str,
    api_key: str,
    scheduled_dep_utc: datetime,
    timeout: int = 15,
    debug: bool = False,
) -> Optional[dict]:
    """
    Look up `ident` on AeroAPI and return the flight record whose
    scheduled_out is closest to `scheduled_dep_utc`, as a dict with keys:
    actual_out/off/on/in, estimated_out/in (datetimes or None),
    departure_delay_sec/arrival_delay_sec (ints or None), status (str),
    cancelled/diverted (bool).

    Returns None if AeroAPI has no matching flight (not an error -- just
    nothing to report yet, e.g. too far out). Raises AeroApiError for
    actual failures (bad key, network error, rate limit, etc).
    """
    flights = _fetch_candidates(ident, api_key, timeout, debug)
    if not flights:
        return None

    def _distance(flight: dict) -> float:
        sched = _parse_dt(flight.get("scheduled_out"))
        if sched is None:
            return float("inf")
        return abs((sched - scheduled_dep_utc).total_seconds())

    best = min(flights, key=_distance)
    if _distance(best) == float("inf"):
        return None

    return _extract_ooi_fields(best)


def find_flight_for_new_record(
    ident: str,
    dep_station: str,
    dep_date: date,
    api_key: str,
    timeout: int = 15,
    debug: bool = False,
) -> Optional[dict]:
    """
    Look up `ident` on AeroAPI and find the one flight departing
    `dep_station` on `dep_date` (the departing airport's own local date --
    matches how the rest of this app treats dates). A flight number can
    recur across different days, and occasionally more than once on the
    same day (different routes/legs), so both the station and the date are
    needed to pin down the right one.

    Returns a dict with the OOOI/delay/status fields (see
    _extract_ooi_fields) plus route info needed to build a whole new
    record: dep_code, arr_code, dep_dt_utc, arr_dt_utc, ident, dep_gate,
    arr_gate (gates are opportunistic -- None if AeroAPI doesn't have them
    for this flight/endpoint). Returns None if nothing matches (not an
    error). Raises AeroApiError for actual failures.
    """
    flights = _fetch_candidates(ident, api_key, timeout, debug)

    for flight in flights:
        origin = flight.get("origin") or {}
        if not _airport_matches(origin, dep_station):
            continue
        sched_out = _parse_dt(flight.get("scheduled_out"))
        if sched_out is None:
            continue
        if _local_date(sched_out, origin.get("timezone")) != dep_date:
            continue

        destination = flight.get("destination") or {}
        sched_in = _parse_dt(flight.get("scheduled_in"))
        if sched_in is None:
            continue

        result = _extract_ooi_fields(flight)
        result.update({
            "ident": flight.get("ident_iata") or flight.get("ident") or ident,
            "dep_code": _airport_code(origin),
            "arr_code": _airport_code(destination),
            "dep_dt_utc": sched_out,
            "arr_dt_utc": sched_in,
            # Speculative field names -- AeroAPI's gate/terminal availability
            # varies by airport and hasn't been confirmed against a live key.
            # None here just means the manual-entry form's own gate fields
            # (or "No Gate") apply instead.
            "dep_gate": flight.get("gate_origin") or origin.get("gate"),
            "arr_gate": flight.get("gate_destination") or destination.get("gate"),
        })
        return result

    return None
