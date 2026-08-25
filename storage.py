"""
SQLite storage layer.

This exists mainly for reference/debugging: every ingest run persists what
it parsed (successes and failures alike) plus a log of each refresh attempt,
so the /calendar view can show what the client actually received.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from models import FlightEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS flight_events (
    occurrence_key   TEXT PRIMARY KEY,
    uid              TEXT NOT NULL,
    raw_summary      TEXT,
    flight_number    TEXT,
    dep_code         TEXT,
    arr_code         TEXT,
    dep_tz           TEXT,
    arr_tz           TEXT,
    dep_dt_local     TEXT,
    arr_dt_local     TEXT,
    dep_dt_utc       TEXT,
    arr_dt_utc       TEXT,
    ics_dtstart      TEXT,
    ics_dtend        TEXT,
    ics_last_modified TEXT,
    fetched_at       TEXT,
    parse_ok         INTEGER,
    parse_error      TEXT,
    event_type       TEXT DEFAULT 'FLIGHT',
    block_code       TEXT,
    dep_gate         TEXT,
    arr_gate         TEXT,
    source           TEXT DEFAULT 'ICS',
    actual_out       TEXT,
    actual_off       TEXT,
    actual_on        TEXT,
    actual_in        TEXT,
    estimated_out    TEXT,
    estimated_in     TEXT,
    departure_delay_sec INTEGER,
    arrival_delay_sec   INTEGER,
    aeroapi_status   TEXT,
    aeroapi_updated_at TEXT
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at   TEXT NOT NULL,
    success      INTEGER NOT NULL,
    event_count  INTEGER,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database may already have been
    created, so an existing flifo.db from before this doesn't break."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(flight_events)")}
    if "event_type" not in existing:
        conn.execute("ALTER TABLE flight_events ADD COLUMN event_type TEXT DEFAULT 'FLIGHT'")
    if "block_code" not in existing:
        conn.execute("ALTER TABLE flight_events ADD COLUMN block_code TEXT")
    if "dep_gate" not in existing:
        conn.execute("ALTER TABLE flight_events ADD COLUMN dep_gate TEXT")
    if "arr_gate" not in existing:
        conn.execute("ALTER TABLE flight_events ADD COLUMN arr_gate TEXT")
    if "source" not in existing:
        # Every pre-existing row predates this feature, so it can only have
        # come from the ICS feed -- SQLite backfills this default onto them.
        conn.execute("ALTER TABLE flight_events ADD COLUMN source TEXT DEFAULT 'ICS'")
    for col in (
        "actual_out", "actual_off", "actual_on", "actual_in",
        "estimated_out", "estimated_in", "aeroapi_status", "aeroapi_updated_at",
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE flight_events ADD COLUMN {col} TEXT")
    for col in ("departure_delay_sec", "arrival_delay_sec"):
        if col not in existing:
            conn.execute(f"ALTER TABLE flight_events ADD COLUMN {col} INTEGER")


@contextmanager
def _connect(db_path: str):
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _dt_str(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def save_events(db_path: str, events: Iterable[FlightEvent]) -> None:
    """
    dep_gate/arr_gate are written on INSERT only (a brand-new record's
    initial value -- typed in manually, or None for an ICS-fed row, which
    has no gate data of its own) but deliberately left out of the
    ON CONFLICT ... DO UPDATE clause below. Gates get populated/refreshed
    afterward through a separate path -- see update_aeroapi_fields() --
    driven by AeroAPI's recurring poll (see aeroapi_sync.py). Excluding
    them here is what stops a routine ICS refresh (which re-upserts the
    same occurrence_key with no gate data of its own every cycle) from
    silently wiping out whatever AeroAPI already found, the same
    protection actual_out/actual_in/etc already have.
    """
    rows = [
        (
            e.occurrence_key, e.uid, e.raw_summary, e.flight_number,
            e.dep_code, e.arr_code, e.dep_tz, e.arr_tz,
            _dt_str(e.dep_dt_local), _dt_str(e.arr_dt_local),
            _dt_str(e.dep_dt_utc), _dt_str(e.arr_dt_utc),
            _dt_str(e.ics_dtstart), _dt_str(e.ics_dtend), _dt_str(e.ics_last_modified),
            _dt_str(e.fetched_at), int(e.parse_ok), e.parse_error,
            e.event_type, e.block_code, e.dep_gate, e.arr_gate, e.source,
        )
        for e in events
    ]
    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO flight_events (
                occurrence_key, uid, raw_summary, flight_number,
                dep_code, arr_code, dep_tz, arr_tz,
                dep_dt_local, arr_dt_local, dep_dt_utc, arr_dt_utc,
                ics_dtstart, ics_dtend, ics_last_modified,
                fetched_at, parse_ok, parse_error, event_type, block_code,
                dep_gate, arr_gate, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(occurrence_key) DO UPDATE SET
                raw_summary=excluded.raw_summary,
                flight_number=excluded.flight_number,
                dep_code=excluded.dep_code,
                arr_code=excluded.arr_code,
                dep_tz=excluded.dep_tz,
                arr_tz=excluded.arr_tz,
                dep_dt_local=excluded.dep_dt_local,
                arr_dt_local=excluded.arr_dt_local,
                dep_dt_utc=excluded.dep_dt_utc,
                arr_dt_utc=excluded.arr_dt_utc,
                ics_dtstart=excluded.ics_dtstart,
                ics_dtend=excluded.ics_dtend,
                ics_last_modified=excluded.ics_last_modified,
                fetched_at=excluded.fetched_at,
                parse_ok=excluded.parse_ok,
                parse_error=excluded.parse_error,
                event_type=excluded.event_type,
                block_code=excluded.block_code,
                source=excluded.source
            """,
            rows,
        )


def update_aeroapi_fields(
    db_path: str,
    occurrence_key: str,
    *,
    actual_out: Optional[datetime] = None,
    actual_off: Optional[datetime] = None,
    actual_on: Optional[datetime] = None,
    actual_in: Optional[datetime] = None,
    estimated_out: Optional[datetime] = None,
    estimated_in: Optional[datetime] = None,
    departure_delay_sec: Optional[int] = None,
    arrival_delay_sec: Optional[int] = None,
    aeroapi_status: Optional[str] = None,
    aeroapi_updated_at: Optional[datetime] = None,
    dep_gate: Optional[str] = None,
    arr_gate: Optional[str] = None,
) -> bool:
    """
    Writes AeroAPI-sourced fields onto an existing row via UPDATE only (no
    upsert/insert) -- deliberately a separate write path from save_events(),
    which never touches these columns. That's what keeps a live poll's
    results from being wiped out by the next routine ICS refresh, without
    any special-casing needed in save_events() itself.

    dep_gate/arr_gate are written unconditionally too (same as every other
    field here), so callers that don't have a fresher gate value to report
    must pass the existing one through explicitly rather than omit it --
    aeroapi_sync._poll_one() does this (falls back to the flight's current
    dep_gate/arr_gate when a poll doesn't report one), since a transient
    gap in AeroAPI's response must never blank out a gate that was already
    known.

    Returns True if a row was found and updated.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE flight_events SET
                actual_out=?, actual_off=?, actual_on=?, actual_in=?,
                estimated_out=?, estimated_in=?,
                departure_delay_sec=?, arrival_delay_sec=?,
                aeroapi_status=?, aeroapi_updated_at=?,
                dep_gate=?, arr_gate=?
            WHERE occurrence_key = ?
            """,
            (
                _dt_str(actual_out), _dt_str(actual_off), _dt_str(actual_on), _dt_str(actual_in),
                _dt_str(estimated_out), _dt_str(estimated_in),
                departure_delay_sec, arrival_delay_sec,
                aeroapi_status, _dt_str(aeroapi_updated_at),
                dep_gate, arr_gate,
                occurrence_key,
            ),
        )
        return cur.rowcount > 0


def prune_stale_ics_events(
    db_path: str,
    seen_occurrence_keys: set,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    grace_minutes: int,
) -> list[str]:
    """
    Deletes ICS-sourced rows that fall inside the just-fetched window but
    weren't among the occurrence_keys this fetch actually returned -- i.e.
    the source calendar no longer has them. This is what covers a reroute
    or a cancelled pairing: Crew Scheduling editing or removing a VEVENT on
    the subscribed calendar changes or drops its occurrence_key here (built
    from uid + departure time -- see parser.build_flight_event()), and
    save_events() is a pure upsert that would otherwise leave the orphaned
    old row sitting in flifo.db forever, potentially still winning
    current/next selection over the flight the pilot is actually now on.

    Only rows that haven't expired yet are eligible (see flight_state's
    grace-window definition) -- anything already flown is left alone
    unconditionally, even though it's technically inside the lookback
    window too, since /api/timeline deliberately keeps full history around
    for browsing. Never touches source='MANUAL' rows.

    Returns the occurrence_keys actually deleted, for logging.
    """
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        candidates = conn.execute(
            """
            SELECT occurrence_key, arr_dt_utc FROM flight_events
            WHERE source = 'ICS' AND parse_ok = 1
              AND dep_dt_utc >= ? AND dep_dt_utc <= ?
            """,
            (window_start.isoformat(), window_end.isoformat()),
        ).fetchall()

        grace = timedelta(minutes=grace_minutes)
        stale_keys = []
        for row in candidates:
            key = row["occurrence_key"]
            if key in seen_occurrence_keys:
                continue
            arr = datetime.fromisoformat(row["arr_dt_utc"]) if row["arr_dt_utc"] else None
            if arr is not None and now >= arr + grace:
                continue  # already flown -- leave historical data alone
            stale_keys.append(key)

        if stale_keys:
            conn.executemany(
                "DELETE FROM flight_events WHERE occurrence_key = ? AND source = 'ICS'",
                [(k,) for k in stale_keys],
            )
        return stale_keys


def log_refresh(db_path: str, fetched_at: datetime, success: bool,
                 event_count: int = 0, error: Optional[str] = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO refresh_log (fetched_at, success, event_count, error) "
            "VALUES (?, ?, ?, ?)",
            (fetched_at.isoformat(), int(success), event_count, error),
        )


def get_refresh_log(db_path: str, limit: int = 20) -> list[dict]:
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM refresh_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_event_rows(db_path: str) -> list[dict]:
    """Raw rows for the debug /calendar view (includes parse failures)."""
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM flight_events ORDER BY dep_dt_utc ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def _row_to_flight_event(row: dict) -> FlightEvent:
    def parse_dt(s):
        return datetime.fromisoformat(s) if s else None

    return FlightEvent(
        uid=row["uid"],
        occurrence_key=row["occurrence_key"],
        raw_summary=row["raw_summary"],
        flight_number=row["flight_number"],
        dep_code=row["dep_code"],
        arr_code=row["arr_code"],
        dep_tz=row["dep_tz"],
        arr_tz=row["arr_tz"],
        dep_dt_local=parse_dt(row["dep_dt_local"]),
        arr_dt_local=parse_dt(row["arr_dt_local"]),
        dep_dt_utc=parse_dt(row["dep_dt_utc"]),
        arr_dt_utc=parse_dt(row["arr_dt_utc"]),
        ics_dtstart=parse_dt(row["ics_dtstart"]),
        ics_dtend=parse_dt(row["ics_dtend"]),
        ics_last_modified=parse_dt(row["ics_last_modified"]),
        fetched_at=parse_dt(row["fetched_at"]) or datetime.now(timezone.utc),
        parse_ok=bool(row["parse_ok"]),
        parse_error=row["parse_error"],
        event_type=row["event_type"] or "FLIGHT",
        block_code=row["block_code"],
        dep_gate=row["dep_gate"],
        arr_gate=row["arr_gate"],
        source=row["source"] or "ICS",
        actual_out=parse_dt(row["actual_out"]),
        actual_off=parse_dt(row["actual_off"]),
        actual_on=parse_dt(row["actual_on"]),
        actual_in=parse_dt(row["actual_in"]),
        estimated_out=parse_dt(row["estimated_out"]),
        estimated_in=parse_dt(row["estimated_in"]),
        departure_delay_sec=row["departure_delay_sec"],
        arrival_delay_sec=row["arrival_delay_sec"],
        aeroapi_status=row["aeroapi_status"],
        aeroapi_updated_at=parse_dt(row["aeroapi_updated_at"]),
    )


def get_valid_flight_events(db_path: str) -> list[FlightEvent]:
    """Successfully-parsed flights only, for the board's current/next logic."""
    rows = get_all_event_rows(db_path)
    return [_row_to_flight_event(r) for r in rows if r["parse_ok"]]


def delete_manual_event(db_path: str, occurrence_key: str) -> bool:
    """
    Delete a single manually-added record. The `source = 'MANUAL'` guard is
    enforced here in the SQL itself, not just in the UI -- so even a
    tampered request naming an ICS-sourced occurrence_key deletes nothing.
    Returns True if a row was actually deleted.
    """
    if not occurrence_key:
        return False
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM flight_events WHERE occurrence_key = ? AND source = 'MANUAL'",
            (occurrence_key,),
        )
        return cur.rowcount > 0


def get_setting(db_path: str, key: str, default: Optional[str] = None) -> Optional[str]:
    with _connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(db_path: str, key: str, value: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
