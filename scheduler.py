"""Minimal background-thread scheduler: runs refresh_fn immediately, then
every interval_seconds, until stopped. No external scheduling dependency."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class RefreshScheduler:
    def __init__(
        self,
        refresh_fn,
        interval_seconds: int,
        retry_seconds: Optional[int] = None,
        name: str = "refresh",
    ):
        self.refresh_fn = refresh_fn
        self.name = name
        self.interval_seconds = interval_seconds
        # After a *failed* attempt, retry sooner than the normal interval
        # instead of waiting a full cycle. This matters most right after
        # boot: a service that auto-starts (e.g. via systemd on a
        # Raspberry Pi with no hardware RTC and Wi-Fi still associating)
        # can easily have its very first fetch fail on DNS/network not
        # being ready yet, even though everything's fine moments later --
        # without this, that one bad attempt would leave the board looking
        # disconnected for up to a full interval_seconds regardless.
        self.retry_seconds = interval_seconds if retry_seconds is None else retry_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._next_wait = interval_seconds

        # --- status, for surfacing to the UI -------------------------------
        # board.html's header error banner reads this (via app.py's
        # /api/status -> RefreshScheduler.status()) to show *why* the board
        # looks stale and a live countdown to the next retry, instead of
        # leaving the viewer to guess from a greyed-out heartbeat icon alone.
        # Guarded by a lock since _run() writes from the background thread
        # while Flask request threads read it via status().
        self._lock = threading.Lock()
        self.last_attempt_at: Optional[datetime] = None
        self.last_success: Optional[bool] = None
        self.last_error: Optional[str] = None
        self.next_attempt_at: Optional[datetime] = None

    def status(self) -> dict:
        with self._lock:
            return {
                "last_attempt_at": self.last_attempt_at.isoformat() if self.last_attempt_at else None,
                "last_success": self.last_success,
                "last_error": self.last_error,
                "next_attempt_at": self.next_attempt_at.isoformat() if self.next_attempt_at else None,
            }

    def _record(self, success: bool, error: Optional[str]) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self.last_attempt_at = now
            self.last_success = success
            self.last_error = error
            self.next_attempt_at = now + timedelta(seconds=self._next_wait)

    def _attempt(self) -> None:
        try:
            self.refresh_fn()
            self._next_wait = self.interval_seconds
            self._record(True, None)
        except Exception as exc:
            # Already logged in detail inside refresh_fn in most cases; this
            # warning is what actually shows up as "why" in the log, since
            # refresh_fn's own logging doesn't know it's running under a
            # scheduler with a shortened retry cadence.
            logger.warning(
                "%s failed; retrying in %ss instead of the normal %ss (%s)",
                self.name, self.retry_seconds, self.interval_seconds, exc,
            )
            self._next_wait = self.retry_seconds
            self._record(False, str(exc))

    def start(self) -> None:
        self._attempt()
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self._next_wait):
            self._attempt()

    def stop(self) -> None:
        self._stop_event.set()
