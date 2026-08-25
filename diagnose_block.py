#!/usr/bin/env python3
"""
One-off diagnostic: prints exactly what select_current_and_next() and its
block-cutoff logic compute against your REAL, currently-stored data --
read-only, makes no changes. Run this on whichever machine/profile is
showing the wrong result:

    flifo_venv/bin/python diagnose_block.py

Delete this file once you're done with it -- it's not meant to be a
permanent part of the app, just a one-time diagnostic aid.
"""
from datetime import datetime, timezone

import aeroapi_sync
import config
import storage
from flight_state import _next_flight_cutoff, select_current_and_next

flights = storage.get_valid_flight_events(config.DB_PATH)
ordered = sorted(flights, key=lambda f: f.dep_dt_utc)
now = datetime.now(timezone.utc)
status_source = aeroapi_sync.get_status_source()
use_live = status_source == "AEROAPI"

print(f"now (UTC):        {now.isoformat()}")
print(f"status source:     {status_source}  (use_live_times={use_live})")
print(f"grace minutes:      {config.ARRIVAL_GRACE_MINUTES}")
print()
print(f"{'#':<3} {'type':<6} {'label':<10} {'dep_dt_utc':<30} {'arr_dt_utc':<30}")
for i, f in enumerate(ordered):
    label = f.flight_number or f.block_code or ""
    print(f"{i:<3} {f.event_type:<6} {label:<10} {f.dep_dt_utc.isoformat():<30} {f.arr_dt_utc.isoformat():<30}")

print()
print("--- block cutoff computation for every BLOCK entry ---")
for i, f in enumerate(ordered):
    if f.event_type != "BLOCK":
        continue
    cutoff = _next_flight_cutoff(ordered, i, use_live, now, config.ARRIVAL_GRACE_MINUTES)
    print(f"[{i}] block {f.block_code!r}: own arr_dt_utc={f.arr_dt_utc.isoformat()}")
    if cutoff is None:
        print("     -> no FLIGHT found after this block; cutoff logic does not apply (unchanged old behavior)")
    else:
        print(f"     -> next-flight cutoff = {cutoff.isoformat()}  (now >= cutoff? {now >= cutoff})")

print()
state = select_current_and_next(flights, now, config.ARRIVAL_GRACE_MINUTES, use_live_times=use_live)
cur = (state.current.block_code if state.current.event_type == "BLOCK" else state.current.flight_number) if state.current else None
nxt = (state.next.block_code if state.next.event_type == "BLOCK" else state.next.flight_number) if state.next else None
print(f"RESULT -- current: {cur} | next: {nxt}")
