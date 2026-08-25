"""
Central configuration, loaded from environment variables / .env.

Nothing here should need to change to run the app -- edit .env instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_extra_headers() -> dict:
    """
    ICS_EXTRA_HEADERS holds whatever bespoke headers the calendar host ends up
    requiring (auth tokens, API keys, custom accept headers, etc). Their
    documentation isn't published yet, so this is left as an empty JSON object
    until someone fills it in -- see .env.
    """
    raw = os.environ.get("ICS_EXTRA_HEADERS", "").strip()
    if not raw:
        return {}
    try:
        headers = json.loads(raw)
        if not isinstance(headers, dict):
            raise ValueError("ICS_EXTRA_HEADERS must be a JSON object")
        return {str(k): str(v) for k, v in headers.items()}
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"ICS_EXTRA_HEADERS in .env is not valid JSON: {exc}\n"
            f"Raw value dotenv read: {raw!r}\n"
            "Note: dotenv reads one line per KEY=VALUE, so the JSON object "
            "must be on a single line, e.g.:\n"
            'ICS_EXTRA_HEADERS={"User-Agent": "abc", "Connection": "Keep-Alive"}'
        ) from exc


# --- Subscribed calendar ---------------------------------------------------
ICS_URL = os.environ.get("ICS_URL", "")
ICS_USERNAME = os.environ.get("ICS_USERNAME", "")
ICS_PASSWORD = os.environ.get("ICS_PASSWORD", "")
ICS_EXTRA_HEADERS = _get_extra_headers()
ICS_REQUEST_TIMEOUT_SECONDS = _get_int("ICS_REQUEST_TIMEOUT_SECONDS", 30)

# When true, logs the exact outgoing request (method/URL/headers) and
# whatever response comes back (status/headers/body preview), or the full
# exception chain if the connection drops before any response -- useful for
# diffing against what a real browser/client sends. Credentials and other
# sensitive header values are redacted before logging. See ics_client.py.
ICS_DEBUG = _get_bool("ICS_DEBUG", False)

# --- Refresh / board behavior -----------------------------------------------
REFRESH_INTERVAL_MINUTES = _get_int("REFRESH_INTERVAL_MINUTES", 15)
# How soon to retry after a *failed* ICS fetch, instead of waiting the full
# REFRESH_INTERVAL_MINUTES. Matters most right after boot (auto-start via
# systemd) if the network/DNS isn't ready yet for the very first attempt --
# see scheduler.RefreshScheduler.
REFRESH_RETRY_SECONDS = _get_int("REFRESH_RETRY_SECONDS", 60)
ARRIVAL_GRACE_MINUTES = _get_int("ARRIVAL_GRACE_MINUTES", 15)

# The header text shown at the top of the board (e.g. "Flight Status", or
# something personalized like "The Smiths' Flight Board"). Lets the app be
# personalized per-install without touching code.
BOARD_TITLE = os.environ.get("BOARD_TITLE", "").strip() or "Flight Status"

# How far around "now" to expand the calendar window when resolving
# recurring events. Generous on both sides so late-posted schedules and
# still-relevant recent flights both show up in the debug view.
LOOKBACK_DAYS = _get_int("LOOKBACK_DAYS", 14)
LOOKAHEAD_DAYS = _get_int("LOOKAHEAD_DAYS", 120)

# --- Storage -----------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "flifo.db"))

# --- Web server ----------------------------------------------------------------
FLASK_HOST = os.environ.get("FLASK_HOST", "0.0.0.0")
FLASK_PORT = _get_int("FLASK_PORT", 5000)
