"""Orchestrates a single fetch -> parse -> store cycle."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
import storage
from ics_client import IcsFetchError, fetch_ics
from parser import parse_calendar

logger = logging.getLogger(__name__)


def refresh_calendar() -> int:
    """Fetch the subscribed calendar, parse it, and persist the results.

    Returns the number of events stored. Errors are logged to refresh_log
    and re-raised so the caller (scheduler / manual trigger) knows it failed;
    they do not crash the process.
    """
    fetched_at = datetime.now(timezone.utc)
    try:
        raw = fetch_ics(
            url=config.ICS_URL,
            username=config.ICS_USERNAME,
            password=config.ICS_PASSWORD,
            extra_headers=config.ICS_EXTRA_HEADERS,
            timeout=config.ICS_REQUEST_TIMEOUT_SECONDS,
            debug=config.ICS_DEBUG,
        )
        events = parse_calendar(
            raw,
            fetched_at=fetched_at,
            lookback_days=config.LOOKBACK_DAYS,
            lookahead_days=config.LOOKAHEAD_DAYS,
        )
        storage.save_events(config.DB_PATH, events)
        storage.log_refresh(config.DB_PATH, fetched_at, success=True, event_count=len(events))
        ok_count = sum(1 for e in events if e.parse_ok)
        logger.info(
            "Refresh OK: %d events parsed, %d failed to parse", ok_count, len(events) - ok_count
        )
        return len(events)
    except IcsFetchError as exc:
        logger.error("Refresh failed (fetch): %s", exc)
        storage.log_refresh(config.DB_PATH, fetched_at, success=False, error=str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - want every failure logged
        logger.exception("Refresh failed (unexpected)")
        storage.log_refresh(config.DB_PATH, fetched_at, success=False, error=str(exc))
        raise
