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

import json
import logging
from datetime import date, datetime, timedelta, timezone
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


# Per AeroAPI's own OpenAPI spec: GET /flights/{ident} with no start/end
# defaults to roughly 11 days in the past through 2 days in the future,
# hard-capped at 10 days past / 2 days future -- and returns only a single
# page (max_pages=1) unless told otherwise. For a flight number with
# regular/daily service, that default page can easily be dominated by
# recent past occurrences, pushing the specific future occurrence we
# actually want out of the page entirely. Pass an explicit start/end
# window scoped tightly around the date we care about instead of relying
# on that default.
_LOOKUP_WINDOW_PAD = timedelta(hours=36)
_MAX_FUTURE = timedelta(days=2)
_MAX_PAST = timedelta(days=10)


def _iso_utc_seconds(dt: datetime) -> str:
    """Whole-second, Z-suffixed ISO8601 (e.g. "2026-08-21T14:05:00Z") --
    matches the exact format AeroAPI's own timestamps come back in (see
    _parse_dt()). A microsecond-precision, "+00:00"-suffixed value (Python's
    plain .isoformat()) got a 400 Bad Request in practice against the real
    API, so match its own format for outgoing start/end params too rather
    than assume any ISO8601 variant is accepted."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _lookup_window(reference_utc: datetime) -> tuple[str, str]:
    """ISO8601 (start, end) bracketing `reference_utc`, clamped to
    AeroAPI's documented hard limits so a request for a date near either
    edge doesn't get rejected outright. Clamps the reference point itself
    into the allowed range *before* padding -- padding first and clamping
    each end independently can invert the window (start after end) when
    reference_utc is far enough outside the allowed range that its padded
    window doesn't reach back into it at all (e.g. force-polling a record
    from weeks ago via the debug page's per-record Poll button)."""
    now = datetime.now(timezone.utc)
    lower_bound = now - _MAX_PAST
    upper_bound = now + _MAX_FUTURE
    anchor = max(lower_bound, min(reference_utc, upper_bound))
    start = max(anchor - _LOOKUP_WINDOW_PAD, lower_bound)
    end = min(anchor + _LOOKUP_WINDOW_PAD, upper_bound)
    return _iso_utc_seconds(start), _iso_utc_seconds(end)


def _fetch_candidates(
    ident: str, api_key: str, timeout: int, debug: bool,
    start: Optional[str] = None, end: Optional[str] = None,
) -> list[dict]:
    """GET /flights/{ident} and return the raw `flights` list. Raises
    AeroApiError for real failures; an ident with no candidates at all just
    yields an empty list (not an error). `start`/`end` (ISO8601) scope the
    query to a specific date window -- see module note above; omitting
    both falls back to AeroAPI's own default window."""
    if not api_key:
        raise AeroApiError("No AeroAPI key configured")
    if not ident:
        raise AeroApiError("No flight number to look up")

    url = f"{AEROAPI_BASE_URL}/flights/{ident}"
    headers = {"x-apikey": api_key, "Accept": "application/json"}
    params = {}
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    if debug:
        logger.info("[AEROAPI_DEBUG] GET %s params=%s", url, params)
        logger.info("[AEROAPI_DEBUG]   x-apikey: %s", _mask_key(api_key))

    try:
        response = requests.get(url, headers=headers, params=params, timeout=timeout)
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
        # requests' own str(exc) for raise_for_status() never includes the
        # response body -- a 400 in particular is AeroAPI telling us
        # exactly what's wrong with the request (bad param format, etc),
        # and swallowing that meant a bad request just had to be
        # guessed-and-retried instead of fixed from the actual reason.
        raise AeroApiError(f"AeroAPI request failed: {exc} -- body: {response.text[:500]}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise AeroApiError(f"AeroAPI returned non-JSON response: {exc}") from exc

    flights = payload.get("flights") or []
    if debug:
        logger.info("[AEROAPI_DEBUG] %d candidate flight(s) returned for %s", len(flights), ident)
    return flights


def _extract_ooi_fields(flight: dict) -> dict:
    """The OOOI/delay/status/gate fields both lookups return. Gates are
    opportunistic -- real-world gate assignments typically only get
    published within ~24h of departure, so None here just means AeroAPI
    doesn't have one *yet*, not that this airport/flight never gets gate
    data. Field names are speculative (AeroAPI's gate/terminal
    availability varies by airport and hasn't been confirmed against a
    live key for every carrier)."""
    origin = flight.get("origin") or {}
    destination = flight.get("destination") or {}
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
        "dep_gate": flight.get("gate_origin") or origin.get("gate"),
        "arr_gate": flight.get("gate_destination") or destination.get("gate"),
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
    start, end = _lookup_window(scheduled_dep_utc)
    flights = _fetch_candidates(ident, api_key, timeout, debug, start=start, end=end)
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

    if debug:
        logger.info("[AEROAPI_DEBUG] matched flight (raw): %s", json.dumps(best, default=str))

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
    # We don't know the departure station's UTC offset yet (that's what
    # we're looking up) -- anchor the window on local noon of dep_date
    # treated as UTC. _LOOKUP_WINDOW_PAD (36h) comfortably covers any real
    # timezone's actual local calendar day for that nominal date either
    # side of that anchor.
    reference_utc = datetime(dep_date.year, dep_date.month, dep_date.day, 12, 0, tzinfo=timezone.utc)
    start, end = _lookup_window(reference_utc)
    flights = _fetch_candidates(ident, api_key, timeout, debug, start=start, end=end)

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

        if debug:
            logger.info("[AEROAPI_DEBUG] matched flight (raw): %s", json.dumps(flight, default=str))

        result = _extract_ooi_fields(flight)  # includes dep_gate/arr_gate
        result.update({
            "ident": flight.get("ident_iata") or flight.get("ident") or ident,
            "dep_code": _airport_code(origin),
            "arr_code": _airport_code(destination),
            "dep_dt_utc": sched_out,
            "arr_dt_utc": sched_in,
        })
        return result

    return None
