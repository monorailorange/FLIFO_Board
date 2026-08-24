"""Minimal background-thread scheduler: runs refresh_fn immediately, then
every interval_seconds, until stopped. No external scheduling dependency."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class RefreshScheduler:
    def __init__(self, refresh_fn, interval_seconds: int):
        self.refresh_fn = refresh_fn
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        try:
            self.refresh_fn()
        except Exception:
            # Already logged inside refresh_fn; startup shouldn't crash
            # just because the very first fetch failed (e.g. creds not
            # filled in yet). The loop will keep retrying.
            logger.warning("Initial calendar refresh failed; will retry on schedule")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.refresh_fn()
            except Exception:
                logger.warning("Scheduled calendar refresh failed; will retry next cycle")

    def stop(self) -> None:
        self._stop_event.set()
