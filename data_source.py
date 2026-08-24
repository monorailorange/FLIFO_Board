"""
Picks between real (stored) calendar data and the generated simulated
dataset, per a mode flag persisted in storage. This is the single place
app.py should read calendar data through, so the toggle affects the board,
the JSON API, and the debug view uniformly.

Switching to "simulated" never reads or writes the real flight_events /
refresh_log tables -- it's a purely additive preview, safe to flip at any
time without disturbing whatever the real ingest has stored.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import simulate
import storage

_MODE_KEY = "data_mode"
VALID_MODES = ("real", "simulated")
DEFAULT_MODE = "real"


def get_mode() -> str:
    mode = storage.get_setting(config.DB_PATH, _MODE_KEY, DEFAULT_MODE)
    return mode if mode in VALID_MODES else DEFAULT_MODE


def set_mode(mode: str) -> str:
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid data mode {mode!r}; must be one of {VALID_MODES}")
    storage.set_setting(config.DB_PATH, _MODE_KEY, mode)
    return mode


def get_valid_flight_events() -> list:
    if get_mode() == "simulated":
        return [e for e in simulate.generate_events() if e.parse_ok]
    return storage.get_valid_flight_events(config.DB_PATH)


def get_all_event_rows() -> list[dict]:
    if get_mode() == "simulated":
        return [e.to_dict() for e in simulate.generate_events()]
    return storage.get_all_event_rows(config.DB_PATH)


def get_refresh_log(limit: int = 20) -> list[dict]:
    if get_mode() == "simulated":
        now = datetime.now(timezone.utc)
        return [{
            "id": None,
            "fetched_at": now.isoformat(),
            "success": True,
            "event_count": len(simulate.generate_events(now)),
            "error": "(SIMULATED MODE -- generated sample data, not a real refresh)",
        }]
    return storage.get_refresh_log(config.DB_PATH, limit=limit)
