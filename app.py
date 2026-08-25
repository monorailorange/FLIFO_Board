"""Flask app: the flight board (/) and the debug calendar view (/calendar)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request, url_for

import aeroapi_sync
import config
import data_source
import storage
from aeroapi_client import AeroApiError
from flight_state import select_current_and_next
from ingest import refresh_calendar
from manual_entry import add_manual_block, add_manual_flight, add_manual_flight_via_aeroapi
from parser import FlightParseError
from scheduler import RefreshScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

storage.init_db(config.DB_PATH)
# Recovers the AeroAPI key / status source from .env if flifo.db came up
# empty (fresh install, restored backup, lost across a power outage, etc)
# -- see aeroapi_sync.py's module docstring.
aeroapi_sync.ensure_settings_seeded()
scheduler = RefreshScheduler(
    refresh_calendar, config.REFRESH_INTERVAL_MINUTES * 60,
    retry_seconds=config.REFRESH_RETRY_SECONDS,
)
# Separate, much faster scheduler for live AeroAPI polling -- see
# aeroapi_sync.py for the per-flight dynamic cadence. sync_now() is a
# cheap no-op whenever status source isn't set to AeroAPI or no key is
# configured, so ticking every minute costs nothing in Local Timing mode.
aeroapi_scheduler = RefreshScheduler(aeroapi_sync.sync_now, 60)


def _is_live_mode() -> bool:
    """AeroAPI-driven status/times only ever apply against real stored
    data -- Simulated mode always shows locally-derived timing regardless
    of this toggle, since there's nothing real to poll AeroAPI about."""
    return aeroapi_sync.get_status_source() == "AEROAPI" and data_source.get_mode() == "real"


# --- Board row count -----------------------------------------------------
# How many flight/block rows the board shows at once -- user-configurable
# from /calendar, mainly so a bigger physical display can show more than
# the original 5 at a time. Lives in app_settings like data_mode (not
# mirrored to .env like the AeroAPI key/status source) -- losing this on
# an empty database just resets it to the default, not a broken app.
_BOARD_ROWS_KEY = "board_rows"
DEFAULT_BOARD_ROWS = 5
MIN_BOARD_ROWS = 1
MAX_BOARD_ROWS = 20


def _get_board_rows() -> int:
    raw = storage.get_setting(config.DB_PATH, _BOARD_ROWS_KEY, str(DEFAULT_BOARD_ROWS))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BOARD_ROWS
    return max(MIN_BOARD_ROWS, min(MAX_BOARD_ROWS, value))


def _set_board_rows(value: int) -> int:
    value = max(MIN_BOARD_ROWS, min(MAX_BOARD_ROWS, value))
    storage.set_setting(config.DB_PATH, _BOARD_ROWS_KEY, str(value))
    return value


def _flight_to_view(flight, live: Optional[bool] = None) -> dict | None:
    if flight is None:
        return None
    now = datetime.now(timezone.utc)
    d = flight.to_dict()
    if flight.event_type == "BLOCK":
        d["status"] = "BLOCK"
        d["display_text"] = f"No Flights Today: {flight.block_code}"
        return d

    if live is None:
        live = _is_live_mode()

    if live:
        d["status"] = flight.live_status(now)
        try:
            dep_tz = ZoneInfo(flight.dep_tz) if flight.dep_tz else timezone.utc
            arr_tz = ZoneInfo(flight.arr_tz) if flight.arr_tz else timezone.utc
        except Exception:
            dep_tz, arr_tz = timezone.utc, timezone.utc
        eff_dep_utc = flight.effective_dep_dt_utc()
        eff_arr_utc = flight.effective_arr_dt_utc()
        d["dep_dt_utc"] = eff_dep_utc.isoformat()
        d["dep_dt_local"] = eff_dep_utc.astimezone(dep_tz).isoformat()
        d["arr_dt_utc"] = eff_arr_utc.isoformat()
        d["arr_dt_local"] = eff_arr_utc.astimezone(arr_tz).isoformat()
    else:
        d["status"] = flight.status(now)
    return d


@app.route("/")
def board():
    return render_template(
        "board.html",
        refresh_interval_minutes=config.REFRESH_INTERVAL_MINUTES,
        grace_minutes=config.ARRIVAL_GRACE_MINUTES,
        board_title=config.BOARD_TITLE,
        board_rows=_get_board_rows(),
    )


@app.route("/calendar")
def calendar_view():
    events = data_source.get_all_event_rows()
    log = data_source.get_refresh_log()
    return render_template(
        "calendar.html", events=events, log=log,
        mode=data_source.get_mode(), valid_modes=data_source.VALID_MODES,
        status_source=aeroapi_sync.get_status_source(),
        valid_status_sources=aeroapi_sync.VALID_STATUS_SOURCES,
        aeroapi_key_masked=aeroapi_sync.masked_api_key(),
        board_rows=_get_board_rows(),
        min_board_rows=MIN_BOARD_ROWS,
        max_board_rows=MAX_BOARD_ROWS,
        added=request.args.get("added"),
        deleted=request.args.get("deleted"),
        error=request.args.get("error"),
        polled=request.args.get("polled"),
    )


@app.route("/calendar/mode", methods=["POST"])
def set_calendar_mode():
    mode = request.form.get("mode", data_source.DEFAULT_MODE)
    try:
        data_source.set_mode(mode)
    except ValueError as exc:
        logger.warning("Rejected calendar mode change: %s", exc)
    return redirect(url_for("calendar_view"))


@app.route("/calendar/board-rows", methods=["POST"])
def set_board_rows():
    raw = request.form.get("rows", "")
    try:
        requested = int(raw)
    except ValueError:
        return redirect(url_for("calendar_view", error=f"Invalid row count: {raw!r}"))
    actual = _set_board_rows(requested)
    if actual != requested:
        return redirect(url_for(
            "calendar_view",
            added=f"Rows shown set to {actual} (clamped to {MIN_BOARD_ROWS}-{MAX_BOARD_ROWS})",
        ))
    return redirect(url_for("calendar_view", added=f"Rows shown set to {actual}"))


@app.route("/calendar/status-source", methods=["POST"])
def set_status_source():
    source = request.form.get("source", aeroapi_sync.DEFAULT_STATUS_SOURCE)
    try:
        aeroapi_sync.set_status_source(source)
    except ValueError as exc:
        logger.warning("Rejected status source change: %s", exc)
        return redirect(url_for("calendar_view", error=str(exc)))
    return redirect(url_for("calendar_view"))


@app.route("/calendar/aeroapi-key", methods=["POST"])
def set_aeroapi_key():
    if request.form.get("action") == "clear":
        aeroapi_sync.clear_api_key()
        return redirect(url_for("calendar_view", added="AeroAPI key cleared"))
    key = request.form.get("api_key", "")
    if not key.strip():
        return redirect(url_for("calendar_view", error="No key entered."))
    aeroapi_sync.set_api_key(key)
    return redirect(url_for("calendar_view", added="AeroAPI key saved"))


@app.route("/calendar/aeroapi-poll-now", methods=["POST"])
def aeroapi_poll_now():
    if aeroapi_sync.get_status_source() != "AEROAPI":
        return redirect(url_for("calendar_view", error="Switch Flight Info Source to AeroAPI first."))
    summary = aeroapi_sync.sync_now()
    if summary["errors"]:
        return redirect(url_for("calendar_view", error="; ".join(summary["errors"])))
    if summary["polled"]:
        return redirect(url_for("calendar_view", polled=", ".join(summary["polled"])))
    return redirect(url_for("calendar_view", polled="(nothing due yet)"))


@app.route("/calendar/aeroapi-poll-record", methods=["POST"])
def aeroapi_poll_record():
    """Force-poll one specific record right now, regardless of whether
    it's currently scoped as "current"/"next" or due under the phase
    cadence -- see aeroapi_sync.poll_flight_now()."""
    occurrence_key = request.form.get("occurrence_key", "")
    ok, message = aeroapi_sync.poll_flight_now(occurrence_key)
    if ok:
        return redirect(url_for("calendar_view", polled=message))
    return redirect(url_for("calendar_view", error=message))


@app.route("/calendar/add-flight", methods=["POST"])
def add_flight_record():
    title = request.form.get("title", "")
    dep_gate = request.form.get("dep_gate", "")
    arr_gate = request.form.get("arr_gate", "")
    try:
        event = add_manual_flight(title, dep_gate, arr_gate)
        storage.save_events(config.DB_PATH, [event])
        return redirect(url_for("calendar_view", added=event.flight_number))
    except FlightParseError as exc:
        return redirect(url_for("calendar_view", error=str(exc)))


@app.route("/calendar/add-flight-aeroapi", methods=["POST"])
def add_flight_record_aeroapi():
    ident = request.form.get("ident", "")
    dep_station = request.form.get("dep_station", "")
    dep_date_raw = request.form.get("dep_date", "")
    dep_gate = request.form.get("dep_gate", "")
    arr_gate = request.form.get("arr_gate", "")

    try:
        dep_date = datetime.strptime(dep_date_raw, "%Y-%m-%d").date()
    except ValueError:
        return redirect(url_for("calendar_view", error="Invalid or missing departure date."))

    api_key = aeroapi_sync.get_api_key()
    if not api_key:
        return redirect(url_for("calendar_view", error="No AeroAPI key saved -- add one above first."))

    try:
        event = add_manual_flight_via_aeroapi(
            ident, dep_station, dep_date, api_key, dep_gate=dep_gate, arr_gate=arr_gate,
        )
        storage.save_events(config.DB_PATH, [event])
        # save_events() deliberately never writes the AeroAPI columns (see
        # its docstring -- that's what stops a routine ICS refresh from
        # wiping out already-polled live data). But this event was *built
        # from* an AeroAPI lookup and carries seeded OOOI/delay/status
        # values right now, so a follow-up write through the same path a
        # scheduled poll uses is needed to actually persist them on insert.
        if event.aeroapi_updated_at is not None:
            # dep_gate/arr_gate are passed through too -- update_aeroapi_fields()
            # writes them unconditionally, and the INSERT save_events() just did
            # already carries whatever AeroAPI (or this form's override fields)
            # set on `event`; omitting them here would immediately null them
            # back out.
            storage.update_aeroapi_fields(
                config.DB_PATH, event.occurrence_key,
                actual_out=event.actual_out, actual_off=event.actual_off,
                actual_on=event.actual_on, actual_in=event.actual_in,
                estimated_out=event.estimated_out, estimated_in=event.estimated_in,
                departure_delay_sec=event.departure_delay_sec, arrival_delay_sec=event.arrival_delay_sec,
                aeroapi_status=event.aeroapi_status, aeroapi_updated_at=event.aeroapi_updated_at,
                dep_gate=event.dep_gate, arr_gate=event.arr_gate,
            )
        return redirect(url_for("calendar_view", added=f"{event.flight_number} (via AeroAPI)"))
    except (ValueError, AeroApiError) as exc:
        return redirect(url_for("calendar_view", error=str(exc)))


@app.route("/calendar/add-block", methods=["POST"])
def add_block_record():
    code = request.form.get("code", "")
    start_raw = request.form.get("start_date", "")
    end_raw = request.form.get("end_date", "")
    try:
        start_date = datetime.strptime(start_raw, "%Y-%m-%d").date()
        end_date = datetime.strptime(end_raw, "%Y-%m-%d").date()
        event = add_manual_block(code, start_date, end_date)
        storage.save_events(config.DB_PATH, [event])
        return redirect(url_for("calendar_view", added=event.block_code))
    except ValueError as exc:
        return redirect(url_for("calendar_view", error=str(exc)))


@app.route("/calendar/delete", methods=["POST"])
def delete_record():
    occurrence_key = request.form.get("occurrence_key", "")
    if storage.delete_manual_event(config.DB_PATH, occurrence_key):
        return redirect(url_for("calendar_view", deleted="1"))
    return redirect(url_for(
        "calendar_view",
        error="Could not delete that record -- it either doesn't exist or came from the ICS feed (only manually-added records can be deleted).",
    ))


@app.route("/api/status")
def api_status():
    flights = data_source.get_valid_flight_events()
    now = datetime.now(timezone.utc)
    live = _is_live_mode()
    state = select_current_and_next(flights, now, config.ARRIVAL_GRACE_MINUTES, use_live_times=live)

    last_refresh_rows = data_source.get_refresh_log(limit=1)
    last_refresh = last_refresh_rows[0] if last_refresh_rows else None

    return jsonify(
        {
            "generated_at": state.generated_at.isoformat(),
            "current": _flight_to_view(state.current, live=live),
            "next": _flight_to_view(state.next, live=live),
            "last_refresh": last_refresh,
            "data_mode": data_source.get_mode(),
            "status_source": aeroapi_sync.get_status_source(),
        }
    )


@app.route("/api/calendar")
def api_calendar():
    return jsonify(data_source.get_all_event_rows())


@app.route("/api/timeline")
def api_timeline():
    """The full sorted set of successfully-parsed flights/blocks (past and
    future), for the board's forward/backward history navigation. Unlike
    /api/status, this isn't limited to current/next -- it's everything the
    database has accumulated, since flight_events rows are never deleted
    as they age out of the ingest's lookback/lookahead window."""
    live = _is_live_mode()
    flights = sorted(data_source.get_valid_flight_events(), key=lambda f: f.dep_dt_utc)
    return jsonify([_flight_to_view(f, live=live) for f in flights])


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    try:
        count = refresh_calendar()
        return jsonify(ok=True, event_count=count)
    except Exception as exc:  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)), 502


if __name__ == "__main__":
    scheduler.start()
    aeroapi_scheduler.start()
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False, use_reloader=False)
