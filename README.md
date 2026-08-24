# flifo_board

Ingests a pilot's subscribed ICS calendar and displays it as a traditional
airport-style Flight Information (FLIFO) board — a five-row table, current
flight pinned to the top row with the next four below it — meant for a wall
display family can glance at.

## How it fits together

```
ics_client.py    -> HTTP GET the .ics feed (Basic Auth + configurable headers)
parser.py        -> parse SUMMARY titles into FlightEvent objects (local times per airport)
storage.py       -> persist every parsed event + refresh attempt to SQLite (debug trail)
flight_state.py  -> pick "current" / "next" flight out of the stored schedule
scheduler.py     -> generic background-thread scheduler (used for both of the below)
simulate.py      -> generates a fake schedule through the real parser, for previewing
data_source.py   -> switches app.py between real (stored) and simulated data
aeroapi_client.py -> raw FlightAware AeroAPI HTTP client (OOOI times, delays, status)
aeroapi_sync.py  -> AeroAPI polling orchestration + the dynamic per-flight cadence
app.py           -> Flask: "/" board, "/calendar" debug view, JSON API
```

## Expected event title format

Each VEVENT's `SUMMARY` is expected to look like:

```
UA123 SFO 19Aug 0830 - ORD 19Aug 1420
```

i.e. `FLIGHT_NUMBER DEP_CODE DDMmm HHMM - ARR_CODE DDMmm HHMM`. Both times
are **local to their respective airport** — the parser looks up each
airport's IANA timezone (via `airportsdata`, IATA first, then ICAO for
4-letter codes) and attaches it, then converts to UTC internally for all
scheduling logic. If your feed's titles differ, adjust `TITLE_PATTERN` in
[parser.py](parser.py).

## Current / Next flight logic

A flight is "expired" once `ARRIVAL_GRACE_MINUTES` (default 15) have passed
since its scheduled arrival. **Current** is the earliest non-expired flight
in the schedule — which naturally covers both stated cases (first flight of
today before it's departed, or a flight actively in progress) and stays
"current" through the grace period after landing. **Next** is whatever
immediately follows current in the schedule, whether later the same day or
the first flight of the next day. See [flight_state.py](flight_state.py).

Status (`ON TIME` / `EN ROUTE` / `ARRIVED`) is currently inferred purely
from scheduled times, since the feed is a static schedule with no live ops
signal. The full pill vocabulary the board understands also includes
`DELAYED`, `CANCELLED`, `TAXIING/LEFT GATE`, and `DIVERTED` — those aren't
derivable from schedule times alone and are reserved for a future live
flight-status API. See `models.FlightEvent.STATUSES` / `.status()`.

Gate assignments (`dep_gate` / `arr_gate` on `FlightEvent`) are the same
story: the ICS feed has no gate data today, so the board always shows
"No Gate" until a future gate-assignment API populates those fields. See
the note in `storage.save_events()` if wiring that up — the ICS refresh
upsert will clobber a gate value back to `None` unless whatever sets it
writes onto the `FlightEvent` before `save_events` is called.

## Setup

```bash
cd /Users/andrewbarton/Development/flifo_board
source flifo_venv/bin/activate   # already has all deps installed
cp .env.example .env             # if you don't already have one
```

Edit `.env`:

- `ICS_URL`, `ICS_USERNAME`, `ICS_PASSWORD` — your subscribed calendar's
  address and Basic Auth credentials.
- `ICS_EXTRA_HEADERS` — left as `{}` on purpose. The calendar host's header
  requirements aren't documented yet; once they are, drop them in here as a
  JSON object, e.g. `{"X-Api-Key": "abc123"}`. No code changes needed —
  [ics_client.py](ics_client.py) merges whatever's here into the request.

## Run

```bash
python run.py
```

- Board: http://localhost:5000/
- Debug calendar view (every parsed event, successes and failures, plus the
  refresh log): http://localhost:5000/calendar
- JSON: `GET /api/status`, `GET /api/calendar`, `POST /api/refresh` (manual
  refresh without waiting for the 15-minute cycle)

The first fetch happens immediately on startup; after that it refreshes
every `REFRESH_INTERVAL_MINUTES` (default 15) in a background thread.

## Debugging fetch failures (`ICS_DEBUG`)

If the calendar host rejects requests (auth errors, unexpected header
requirements, connection drops like `Remote end closed connection without
response`), set `ICS_DEBUG=true` in `.env` and restart. Every refresh then
logs, to the console:

- the exact outgoing request: method, URL, and every header (credentials
  and anything that looks like a token/cookie/secret are redacted)
- if a response came back: status code, all response headers, and a preview
  of the body
- if the connection failed before any response: the full exception chain
  (e.g. `ConnectionError` → `ProtocolError` → `RemoteDisconnected`), so you
  can tell a WAF/bot-detection drop apart from a timeout, DNS failure, or
  TLS error

Trigger a fetch on demand instead of waiting for the 15-minute cycle:

```bash
curl -X POST http://localhost:5000/api/refresh
```

Then compare the logged request headers line-by-line against what a real
browser/client sends for the same endpoint (e.g. from browser dev tools'
Network tab). Turn `ICS_DEBUG` back off once you're done — it's verbose and
logs full header values that aren't sensitive alongside the redacted ones.

## Previewing with simulated data

On `/calendar` there's a **Data source: Real World Data / Simulated Data**
toggle. Flip it to see the board fully populated (a current en-route flight,
a next flight, a multi-day "No Flights Today: OFF" block, and a parse-error
row) without touching your real credentials, `.env`, or stored data:

- The simulated schedule ([simulate.py](simulate.py)) is built fresh relative
  to "now" on every request and run through the exact same
  `parser.parse_calendar()` pipeline the real feed uses — it's a preview of
  the real rendering path, not a separate mock.
- The toggle is stored server-side (in `flifo.db`'s `app_settings` table), so
  it affects the actual board (`/`) and JSON API too, not just the debug
  page — useful if you want to see the wall display itself in a populated
  state. The board shows an amber "SIMULATED DATA" banner whenever it's on,
  so it's never mistaken for the real schedule.
- Switching to simulated mode never reads or writes the real `flight_events`
  / `refresh_log` tables — your actual ingested data is untouched and
  reappears as soon as you switch back to "Real World Data".
- The background scheduler keeps refreshing the real feed on its normal
  interval the whole time, regardless of which mode the toggle is in.

## Browsing flight history

The board's `←` / `Current Flight` / `→` buttons (bottom of the main
display, deliberately understated) step through every stored flight/block
record chronologically, not just today's current/next:

- `flight_events` rows are never deleted as they age out of the ingest's
  lookback/lookahead window, so the database accumulates real history over
  time. `GET /api/timeline` returns that full sorted set.
- `←`/`→` slide the five-row window over that list one record at a time.
  Whichever row is the true live current flight gets the amber highlight,
  wherever it lands in the visible five — including scrolling out of view
  entirely while browsing further away.
- Browsing is purely client-side (`templates/board.html`) and per-tab — it
  suspends following live `/api/status` updates until "Current Flight" is
  pressed again, so it won't fight another device's view of the same board
  or silently snap back mid-browse on the 30s poll.
- "Current Flight" resets to live mode; its green highlight indicates
  whether you're currently in live mode or browsing history.

## Manually adding / deleting records

The `/calendar` debug view has forms under "Manually Add a Record":

- **Add Flight (Manual Title)** takes a title in the exact same format the
  ICS feed uses (`UA123 SFO 19Aug 0830 - ORD 19Aug 1420`) plus optional
  gates, and runs it through the identical parser the ICS feed does
  (`manual_entry.py` -> `parser.build_flight_event()`) — same validation,
  same airport-timezone handling, no separate looser path to keep in sync.
  Always available.
- **Add Flight (via AeroAPI)** — only shown when Flight Info Source is set
  to AeroAPI (see below) — takes just a flight number, departure station,
  and departure date. `manual_entry.add_manual_flight_via_aeroapi()` looks
  it up on AeroAPI (`aeroapi_client.find_flight_for_new_record()`) and
  builds the whole record from the result: route, scheduled times, and any
  live OOOI/delay/status data already available, seeded immediately rather
  than waiting for the next scheduled poll. The departure station matters
  because a flight number can operate more than once on the same day
  (different routes) — station + date together is what pins down the right
  one; matching by date alone isn't enough. Optional gate fields here
  override whatever AeroAPI itself reports for gates, if anything.
- **Add Day Off / Block** takes a code (e.g. `OFF`) and a start/end date,
  building a `BLOCK`-type record the same way a multi-day ICS entry would
  (see "No Flights Today" above) — but without the ICS parser's 2-day
  minimum, since this is a deliberate action, not a guess.

Both write straight to `flight_events` tagged `source="MANUAL"`, alongside
whatever the ICS feed has already stored (`source="ICS"`) — they show up on
the board, in `/api/timeline`, everywhere, exactly like a real synced
record. The only difference: a `Delete` button appears in the debug table's
`Actions` column **only** for `MANUAL` rows. `storage.delete_manual_event()`
enforces `source = 'MANUAL'` in the SQL itself, not just by hiding the
button — so even a hand-crafted request naming an ICS record's key deletes
nothing. Manual add/delete always act on real stored data regardless of
whichever "Data source" toggle you're currently viewing.

## Live status via FlightAware AeroAPI

`/calendar` has a "Flight Info Source" toggle: **Local Timing** (the
original behavior — status/times derived purely from the published
schedule) or **AeroAPI**, which polls
[FlightAware AeroAPI](https://www.flightaware.com/commercial/aeroapi/) for
the current and next flight and shows live data instead. This only ever
applies to Real World Data; Simulated mode always shows locally-derived
timing regardless of the toggle, since there's nothing real to poll AeroAPI
about.

**Setting it up:** paste an AeroAPI key into the "AeroAPI Key" box on
`/calendar` (no manual `.env` editing, no restart needed to take effect)
— masked everywhere it's displayed back (`aeroapi_sync.masked_api_key()`),
and only ever sent as the `x-apikey` header on outbound AeroAPI requests.
A "Poll AeroAPI Now" button triggers an immediate check outside the normal
schedule (useful for testing — it's still subject to the cadence rules
below, so it only actually calls out if something's due); each debug-table
row for a flight also has its own **Poll** button that force-polls that
specific record regardless of current/next scope.

**Durability:** the key and the Local Timing/AeroAPI toggle are read from
(and written back to) `app_settings` in `flifo.db` for normal use, but also
mirrored into `.env` (`AEROAPI_API_KEY` / `AEROAPI_STATUS_SOURCE`) on every
save. `flifo.db` is treated as disposable everywhere else in this app (it's
`.gitignore`'d; the ICS side just re-syncs fine if it's lost), so it can't
be the only place real configuration lives. At startup,
`aeroapi_sync.ensure_settings_seeded()` restores these settings from `.env`
into a freshly-created `app_settings` if the database ever comes up empty
— so a power outage or service restart that wipes/loses `flifo.db` doesn't
also mean re-entering the key by hand. Verified directly: save a key, kill
the process, delete `flifo.db`, restart — the key and toggle come back
automatically, logged at startup.

**Display rule** (`models.FlightEvent.effective_dep_dt_utc()` /
`effective_arr_dt_utc()`): the published/scheduled time shows until AeroAPI
reports something newer —

1. Once AeroAPI reports a delay (`departure_delay`/`arrival_delay` non-null),
   the delay-adjusted estimate (`estimated_out`/`estimated_in`) replaces the
   published time.
2. Once the flight actually happens (`actual_out`/`actual_in` non-null),
   that replaces the estimate — the real thing that happened always wins.

**Status ladder** (`FlightEvent.live_status()`), most specific first:
`CANCELLED`/`DIVERTED` (from AeroAPI's own flags) → `ARRIVED` (`actual_in`)
→ `LANDED` (`actual_on`, wheels down but not yet at the gate — new status,
distinct from `ARRIVED`) → `EN ROUTE` (`actual_off`) → `TAXIING/LEFT GATE`
(`actual_out`) → `DELAYED` (a delay is known but nothing's happened yet) →
`ON TIME`.

**Polling cadence** (`aeroapi_sync.py`) is dynamic per flight, on its own
60-second scheduler separate from the 15-minute ICS refresh:

- **Current flight**: starts polling once within 15 minutes of the
  delay-adjusted estimated departure (or scheduled, if no delay is known
  yet). Every **1 minute** until `actual_out` (→ gate-out happened); every
  **1 minute** until `actual_off` (→ wheels up); every **5 minutes** while
  airborne until `actual_on` (→ landed); every **1 minute** until
  `actual_in` (→ at the gate). Once `actual_in` is set, that flight is done
  polling — and the board's 15-minute "still current" grace countdown
  (`ARRIVAL_GRACE_MINUTES`) counts from that *real* arrival moment, not the
  originally published one (`flight_state.select_current_and_next(...,
  use_live_times=True)`), so a delayed flight keeps its full grace window
  measured from when it actually landed.
- **Next flight**: fixed **15 minutes**, only to keep `estimated_out`/
  `estimated_in` current — never the fast phase-based cadence.

AeroAPI's own results (`aeroapi_client.fetch_flight_status()`) are written
via `storage.update_aeroapi_fields()`, a plain `UPDATE` that's deliberately
a *separate write path* from the ICS upsert (`storage.save_events()`) — so
a routine 15-minute ICS refresh can never wipe out live data that's already
been polled for that flight.

**Important:** both the scheduled polling and the "Poll AeroAPI Now"
button only ever act on whoever `select_current_and_next()` currently
considers current/next — so the expiry basis it uses matters a lot. It's
`FlightEvent.effective_arr_dt_utc()` (actual → delay-adjusted estimate →
scheduled), not just the raw scheduled arrival — a flight running late
enough that `scheduled_arrival + grace` has already passed, while still
genuinely airborne with no `actual_in` yet, must *not* drop out of that
scope, or nothing would ever be left to fetch `actual_on`/`actual_in` for
it, automatically or via "Poll Now". Each debug-table row for a `FLIGHT`
record also has its own **Poll** button (`/calendar/aeroapi-poll-record`,
`aeroapi_sync.poll_flight_now()`) that force-polls that specific record
immediately, bypassing current/next scoping and the cadence entirely —
useful for testing, or for pulling a record back in if it ever falls out
of scope some other way.

**"Next" also has to skip already-expired flights, not just find whoever's
immediately next by departure time.** Those two coincide under schedule-only
timing (if flight N isn't expired, flight N+1 -- departing later -- can't
be either), but a long-spanning `BLOCK` breaks that assumption: it can stay
`current` for weeks (see "No Flights Today" above) while an ordinary flight
chronologically sandwiched inside that window has already itself arrived
and expired via live `actual_in` data. Naively taking "whatever's next by
departure time" would leave that already-arrived flight stuck in the "Next
Flight" slot indefinitely, since nothing ever advances past a `current`
that hasn't itself expired. `select_current_and_next()` instead walks
forward from `current` to the first flight that also isn't expired.

## Airline column

Every row's Airline column always shows [static/ua.png](static/ua.png) —
this board is built for a single-carrier (United) schedule, so the logo
isn't looked up per-flight. If that ever needs to vary by carrier, that's a
`flight.airline_code -> logo` lookup in `renderRow()` in
[board.html](templates/board.html) plus per-carrier assets in `static/`.

## Fonts

The board's default typeface is set to "Zurich Black Extended BT" in
[static/style.css](static/style.css)'s `:root` font-family. That's a
commercial Bitstream/Monotype font, not something bundled with the app —
it only renders where it's actually installed:

- **If it's installed as a system font** on whatever device displays the
  board, it's picked up automatically, no further changes needed.
- **If not**, the browser silently falls back through the rest of the
  stack (Bahnschrift → DIN Alternate → Helvetica Neue → Arial → sans-serif).
- **To make it render on any device** regardless of local installs (e.g. a
  kiosk/tablet you haven't installed fonts on), add a licensed font file
  (`.woff2`/`.woff`/`.ttf`/`.otf`) to `static/` and an `@font-face` rule
  pointing at it, above the `:root` block in `style.css`:
  ```css
  @font-face {
    font-family: "Zurich Black Extended BT";
    src: url("zurich-blkex-bt.woff2") format("woff2");
    font-weight: 900;
  }
  ```

## Notes / known gaps

- No CalDAV protocol support (e.g. discovery, ETags) — this is a plain
  authenticated GET of a `.ics` URL, per the "ics feed client" ask.
- `parse_calendar` expands the feed through `recurring_ical_events`, so
  recurring `VEVENT`s (RRULE) resolve into individual occurrences too, not
  just one-off events.
- Unparseable events aren't dropped — they're stored with `parse_ok=0` and
  a `parse_error`, and show up (in red) on `/calendar` so a bad title format
  is easy to spot instead of just silently missing from the board.
