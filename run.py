#!/usr/bin/env python3
"""Entry point: starts the background calendar-refresh scheduler, the
AeroAPI polling scheduler, and the Flask board server."""
from __future__ import annotations

import config
from app import aeroapi_scheduler, app, scheduler

if __name__ == "__main__":
    scheduler.start()
    aeroapi_scheduler.start()
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False, use_reloader=False)
