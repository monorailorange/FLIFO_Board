"""Minimal background-thread scheduler: runs refresh_fn immediately, then
every interval_seconds, until stopped. No external scheduling dependency."""
from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class RefreshScheduler:
    def __init__(self, refresh_fn, interval_seconds: int, retry_seconds: Optional[int] = None):
        self.refresh_fn = refresh_fn
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

    def start(self) -> None:
        try:
            self.refresh_fn()
            self._next_wait = self.interval_seconds
        except Exception:
            # Already logged inside refresh_fn; startup shouldn't crash
            # just because the very first fetch failed (e.g. creds not
            # filled in yet, or DNS not up yet). Retry soon rather than
            # waiting a full cycle.
            logger.warning(
                "Initial calendar refresh failed; retrying in %ss", self.retry_seconds
            )
            self._next_wait = self.retry_seconds
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self._next_wait):
            try:
                self.refresh_fn()
                self._next_wait = self.interval_seconds
            except Exception:
                logger.warning(
                    "Scheduled calendar refresh failed; retrying in %ss instead of the normal %ss",
                    self.retry_seconds, self.interval_seconds,
                )
                self._next_wait = self.retry_seconds

    def stop(self) -> None:
        self._stop_event.set()
