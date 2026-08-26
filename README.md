# flifo_board

Ingests a pilot's subscribed ICS calendar and displays it as a traditional
airport-style Flight Information (FLIFO) board — a table, current flight
pinned to the top row with more below it (5 by default, adjustable from
the debug page) — meant for a wall display family can glance at.
Optionally augmented with live status (delays, gate-out/wheels-up/
wheels-down/gate-in times) via FlightAware AeroAPI.

## How it fits together

```
ics_client.py     -> HTTP GET the .ics feed (Basic Auth + configurable headers)
parser.py         -> parse SUMMARY titles into FlightEvent objects (local times per airport)
storage.py        -> persist every parsed event + refresh attempt to SQLite (debug trail)
flight_state.py   -> pick "current" / "next" flight out of the stored schedule
scheduler.py      -> generic background-thread scheduler (used for both of the below)
ingest.py         -> one ICS fetch -> parse -> store cycle
simulate.py       -> generates a fake schedule through the real parser, for previewing
data_source.py    -> switches app.py between real (stored) and simulated data
manual_entry.py   -> builds hand-added flight/block records for the debug view's forms
aeroapi_client.py -> raw FlightAware AeroAPI HTTP client (OOOI times, delays, status)
aeroapi_sync.py   -> AeroAPI polling orchestration + the dynamic per-flight cadence
app.py            -> Flask: "/" board, "/calendar" debug view, JSON API
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

`FLIGHT_NUMBER` doesn't actually need the carrier code — Crew Scheduling's
own calendar titles omit it entirely (`1206 TPA 12Aug 1738 - EWR 12Aug
2035`, not `UA1206 ...`), since it's this pilot's own airline's calendar.
`parser.normalize_flight_number()` prepends `parser.DEFAULT_CARRIER_CODE`
("UA") to a bare-digits flight number so AeroAPI lookups (which need a
carrier-qualified ident to know which airline's flight to match) work —
anything already carrying a letter prefix is left untouched. Hardcoded
rather than configurable, same as the rest of this app's United-specific
branding (the airline column always renders `ua_white.png` regardless of
actual carrier).

### Multi-day "block" entries

A non-flight-shaped title (doesn't match the pattern above) that spans at
least `parser.MULTIDAY_BLOCK_MIN_DAYS` (2) elapsed days is treated as a
schedule block — a day off, reserve, vacation, etc — rather than a parse
failure. Its raw title text becomes its "code" (e.g. `OFF`, `RSV`, `UNBS`).
A block occupying "today" renders in the Current Flight slot as
**"No Flights Today: [CODE]"** instead of a flight card. Anything that
doesn't match the flight format *and* isn't multi-day is a genuine parse
failure, shown (in red) on the debug view but otherwise ignored.

## Current / Next flight logic

A flight is "expired" once `ARRIVAL_GRACE_MINUTES` (default 15) have passed
since its arrival. **Current** is the earliest non-expired flight/block in
the schedule — which naturally covers both stated cases (first flight of
today before it's departed, or a flight actively in progress) and stays
"current" through the grace period after landing. **Next** is the first
*also non-expired* flight/block after that — not just whatever's
immediately next by departure time. Those two used to always coincide under
schedule-only timing (if flight N isn't expired, flight N+1 — departing
later — can't be either), but a long-spanning block breaks that: it can
stay current for weeks while an ordinary flight chronologically sandwiched
inside that window has already itself expired (its real, AeroAPI-reported
arrival happened and its grace period has passed). Naively taking "whatever
comes right after current" would leave that already-arrived flight stuck in
the "Next Flight" slot indefinitely, since nothing advances past a current
that hasn't itself expired — `select_current_and_next()` instead walks
forward from current to the first flight/block that also isn't expired.
See [flight_state.py](flight_state.py).

**A block's effective end is also clamped by the pilot's next flying
assignment**, on top of its own declared date span. Otherwise a block could
keep displaying "No Flights Today: X" on a day that's actually a flying
day (a reserve callout, a reroute) or right past the point a pilot needs to
have already reported. The clamp is whichever comes *later* of: one hour
before that next assignment departs, or midnight (departure-station-local)
commencing the calendar date it departs on. Midnight is a hard floor, not
just a fallback — for a flight departing shortly after midnight (say,
00:05), "one hour before" would land the evening *before*, incorrectly
telling the pilot "no flights today" on a date that starts with a flight 5
minutes in; midnight instead gives that flight exactly a 5-minute lead
before departure, matching how little runway a report time that early
actually has. In AeroAPI mode this cutoff tracks the delay-adjusted
estimated departure, not the stale original schedule, so a block correctly
stays up if the next flight ends up pushed back. Only a *still-upcoming*
flight counts for this clamp — an already-flown flight (this app never
deletes history, so one can easily sit chronologically inside a block's
declared span) is skipped over, the same way the "next" slot's own
selection already skips expired records, so old history can never
incorrectly cut a currently-valid block short. See
`flight_state._next_flight_cutoff()`.

**Status** (`ON TIME` / `EN ROUTE` / `ARRIVED`) is inferred purely from
scheduled times in Local Timing mode (`FlightEvent.status()`), since a bare
ICS feed has no live ops signal. The full pill vocabulary the board
understands also includes `DELAYED`, `CANCELLED`, `TAXIING/LEFT GATE`,
`DIVERTED`, and `LANDED` (see `FlightEvent.STATUSES`) — those come from
AeroAPI's `live_status()` when that's the active Flight Info Source (see
below).

**Gate assignments** (`dep_gate` / `arr_gate` on `FlightEvent`) show "No
Gate" until populated — either manually (see below) or, opportunistically,
from AeroAPI, which is only ever the *initial* value: in AeroAPI mode,
every recurring poll (not just the one at manual-entry time) re-checks and
refreshes the gate too, since real-world gate assignments are typically
only published within ~24h of departure — long after most flights are
already on the board. A poll that doesn't report a gate never blanks out
one already known (see `aeroapi_sync._poll_one()`), and a routine ICS
refresh can't touch a gate at all once set (`storage.save_events()`
deliberately excludes `dep_gate`/`arr_gate` from its update, the same
protection `actual_out`/`actual_in`/etc already have) — so once a gate
shows up, only a fresher AeroAPI value (or a brand-new manual entry) can
ever change it.

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
  **Must be on a single line** — `python-dotenv` reads one `KEY=VALUE` per
  line, so a JSON object split across lines gets silently truncated.
- `BOARD_TITLE` — header text shown at the top of the board and in the
  browser tab (rendered in all-caps by the board's styling regardless of
  how it's typed here). Lets an install be personalized without touching
  code.
- `AEROAPI_API_KEY` / `AEROAPI_STATUS_SOURCE` — durability mirrors of
  settings normally managed from `/calendar` (see "Live status via
  FlightAware AeroAPI" below). You shouldn't need to edit these by hand.

## Run

```bash
python run.py
```

- Board: http://localhost:5000/
- Debug calendar view (every parsed event, successes and failures, plus the
  refresh log): http://localhost:5000/calendar
- JSON: `GET /api/status`, `GET /api/calendar`, `GET /api/timeline`,
  `POST /api/refresh` (manual ICS refresh without waiting for the
  15-minute cycle)

Two background schedulers start alongside the Flask server: the ICS refresh
(`REFRESH_INTERVAL_MINUTES`, default 15) and the AeroAPI poller (fixed
60-second tick; see below for what it actually does on each tick). Port
5000 collides with macOS AirPlay Receiver on some Macs — set `FLASK_PORT`
in `.env` if you hit that.

A *failed* ICS fetch retries after `REFRESH_RETRY_SECONDS` (default 60)
instead of waiting the full `REFRESH_INTERVAL_MINUTES` — settling back to
the normal cadence the moment a fetch succeeds. This matters most on an
auto-starting install (see "Running unattended" below): the very first
fetch runs synchronously the instant the service starts, and on a
Raspberry Pi with no hardware RTC and Wi-Fi still associating, DNS/network
genuinely might not be ready yet at that exact moment. Without a short
retry, one bad attempt at boot would leave the board's heartbeat looking
disconnected for a full 15 minutes even once the network's fine again.

## Connectivity errors: header banner and auto-retry

The calendar fetch and the AeroAPI poll are two independent background
schedulers (`scheduler.RefreshScheduler`), and each tracks its own
success/failure state. When either one fails, the board's header shows a
red banner naming which one failed, the error itself, and a live countdown
to the next automatic retry — instead of just leaving the heartbeat icon
quietly greyed out with no explanation. Both schedulers already retry a
failed attempt after `REFRESH_RETRY_SECONDS` (default 60) rather than
waiting a full cycle (see "Run" above) — the banner just makes that visible.
The countdown ticks down client-side every second from timestamps already
in the last `/api/status` response; it doesn't poll the server any faster.
The banner clears itself the moment either scheduler reports success again,
with no restart or manual dismissal needed.

This matters most on a headless Raspberry Pi you can't glance a monitor at
— on boot, before Wi-Fi has associated or DNS has come up, or if the router
or the upstream service itself is briefly down. The most common reasons a
poll fails on the Pi:

| Symptom (banner error text) | Likely cause | Self-resolves? |
| --- | --- | --- |
| `Failed to resolve 'ccsplus.ual.com'` / `Failed to resolve 'aeroapi.flightaware.com'` | DNS not up yet — most common right at boot, before Wi-Fi has fully associated | Yes, once the network finishes coming up; the 60s retry catches it within a cycle or two |
| `Connection refused` / `Network is unreachable` | Wi-Fi not associated yet, wrong Wi-Fi credentials, or the router itself is down/rebooting | Usually — resolves once Wi-Fi reconnects; a permanent credential problem needs `raspi-config`/`nmcli` fixed by hand |
| `Connection timed out` / `Max retries exceeded` | Weak/dropping Wi-Fi signal, or the upstream ISP link is down even though the LAN is fine | Sometimes — depends on whether the outage is local or upstream |
| `certificate verify failed` / `certificate has expired` | System clock is wrong (no hardware RTC, and NTP hasn't synced yet right after boot) — TLS certs validate against wall-clock time | Yes, once NTP catches up (usually seconds after network comes up) |
| A captive-portal login page or unexpected HTML instead of calendar/JSON data | Connected to a network requiring a browser-based login (hotel/public Wi-Fi) — not applicable to a normal home install | No — needs the captive portal completed manually; not something a retry can fix |
| `401`/`403 Client Error` | ICS username/password or AeroAPI key is wrong, expired, or was rotated | No — needs the credential updated in `.env` or `/calendar` |
| `429 Client Error` | AeroAPI personal-tier rate limit (10 requests/min) or monthly free-credit cap hit | Usually — clears on its own once the limit window rolls over |
| `500`/`502`/`503 Server Error` | FlightAware's or United's own service is briefly down/degraded | Yes, typically within a few retries |
| `No AeroAPI key configured` | Flight Info Source is set to AeroAPI but no key has been saved yet | No — needs a key entered on `/calendar` |

Everything in the "self-resolves" column is exactly what the 60-second
retry is there for. Anything that doesn't self-resolve still shows in the
banner so it's obvious a person needs to intervene, rather than the board
just sitting stale indefinitely with no explanation on screen.

## Running unattended (systemd)

For a board that's meant to just stay up on its own — a Raspberry Pi
driving a physical display being the main case — `deploy/flifo-board.service`
is a ready-to-install systemd unit that starts the app on boot and
restarts it if it ever crashes:

```bash
sudo cp deploy/flifo-board.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flifo-board
```

Edit `User=`, `WorkingDirectory=`, and `ExecStart=` in the unit file first
if your clone isn't at `/home/pi/FLIFO_Board` under user `pi` (or your venv
isn't named `flifo_venv`) — it's a plain text file, no templating. Running
a second, independently-configured board (a different pilot's manual
commute entries, a separate `.env`/`flifo.db`) on the same machine just
means copying the unit under a different filename with its own
`Description=` and paths pointing at that clone.

Useful commands once it's installed:

```bash
sudo systemctl restart flifo-board   # after a git pull that touches .py files
sudo systemctl status flifo-board    # confirm it's active *and* enabled
sudo journalctl -u flifo-board -f    # tail its live logs
```

A CSS/template-only change (`static/`, `templates/`) just needs a page
refresh in the browser viewing the board — `restart` is only needed when
Python code changes, since Flask's template cache and the running
process's imports don't otherwise pick those up.

## Debugging fetch failures (`ICS_DEBUG`)

If the calendar host rejects requests (auth errors, unexpected header
requirements, connection drops like `Remote end closed connection without
response`), set `ICS_DEBUG=true` in `.env` and restart. Every ICS refresh
(and every AeroAPI request — `aeroapi_client.py` shares the same flag) then
logs, to the console:

- the exact outgoing request: method, URL, and every header (credentials
  and anything that looks like a token/cookie/secret are redacted)
- if a response came back: status code, all response headers, and a preview
  of the body
- if the connection failed before any response: the full exception chain
  (e.g. `ConnectionError` → `ProtocolError` → `RemoteDisconnected`), so you
  can tell a WAF/bot-detection drop apart from a timeout, DNS failure, or
  TLS error

Trigger an ICS fetch on demand instead of waiting for the 15-minute cycle:

```bash
curl -X POST http://localhost:5000/api/refresh
```

Then compare the logged request headers line-by-line against what a real
browser/client sends for the same endpoint (e.g. from browser dev tools'
Network tab). Turn `ICS_DEBUG` back off once you're done — it's verbose and
logs full header values that aren't sensitive alongside the redacted ones.

## Previewing with simulated data

The `/calendar` debug view has a "Data source" toggle: **Real World Data**
(the default — everything above) or **Simulated Data**. Switching to
simulated mode serves a freshly-generated, self-consistent fake schedule
(`simulate.py`) computed relative to "now" each time it's requested — a
past arrived flight, a current en-route flight, a next flight later the
same day, a multi-day block further out, and one deliberately malformed
title — run through the **exact same** `parser.parse_calendar()` pipeline
the real feed uses, so it's a faithful preview of the real rendering path,
not a separate mock UI to keep in sync by hand.

- Switching to simulated mode never reads or writes the real
  `flight_events` / `refresh_log` tables — your actual ingested data is
  untouched and reappears as soon as you switch back to "Real World Data".
- The background scheduler keeps refreshing the real feed on its normal
  interval the whole time, regardless of which mode the toggle is in.
- Simulated mode always shows locally-derived timing/status, regardless of
  the Flight Info Source toggle — there's nothing real to poll AeroAPI
  about.

## Browsing flight history

The board's `←` / `Current Flight` / `→` buttons (bottom of the main
display, deliberately understated) step through every stored flight/block
record chronologically, not just today's current/next:

- `flight_events` rows are never deleted just because they age out of the
  ingest's lookback/lookahead window or because a flight has already
  happened — so the database accumulates real history over time.
  `GET /api/timeline` returns that full sorted set. (An ICS row can still
  be deleted, but only for one specific reason — see "Reroutes / stale
  calendar events" below — and never for merely being old.)
- `←`/`→` slide a window (sized per "Rows Shown" below) over that list one
  record at a time. Whichever row is the true live current flight gets the
  amber highlight, wherever it lands in the visible window — including
  scrolling out of view entirely while browsing further away.
- Browsing is purely client-side (`templates/board.html`) and per-tab — it
  suspends following live `/api/status` updates until "Current Flight" is
  pressed again, so it won't fight another device's view of the same board
  or silently snap back mid-browse on the 30s poll.
- "Current Flight" resets to live mode; its green highlight indicates
  whether you're currently in live mode or browsing history.

**Rows Shown** — a "Board Display" control on `/calendar` (1–20, default
5) sets how many rows the board renders at once, for a bigger physical
display that can comfortably fit more than the default. Persists in
`app_settings` (like the real/simulated data toggle — not mirrored to
`.env`, since losing it just resets to the default 5, not a broken app).
Takes effect on the board's next poll/reload, no restart needed.

## Reroutes / stale calendar events

`storage.save_events()` is a pure upsert — it inserts or updates whatever
the latest ICS fetch returned, but never deletes anything on its own. That
matters when Crew Scheduling reroutes a pairing (weather, an aircraft swap,
any operational disruption) and the subscribed calendar changes underneath
you: the old VEVENT either gets edited in place (which changes its
`occurrence_key` here, since that's built from `uid` + departure time — see
`parser.build_flight_event()`) or gets removed outright and replaced with a
new one. Either way, without something actively cleaning up, the *old* row
would sit in `flifo.db` forever — and if it hadn't departed yet, it would
still be chronologically eligible to win current/next selection over the
flight you're actually now on.

Every successful `refresh_calendar()` run guards against this
(`storage.prune_stale_ics_events()`, called from `ingest.py` right after
`save_events()`): any `source="ICS"` row whose scheduled departure falls
inside this fetch's lookback/lookahead window, but whose `occurrence_key`
didn't come back in this fetch's results, gets deleted — *unless* it's
already expired (past its scheduled arrival + `ARRIVAL_GRACE_MINUTES`), in
which case it's left alone regardless. That second condition is what keeps
this from ever eating real history: a flight that's already flown is
protected unconditionally, even on the day it ages out of the lookback
window, so `/api/timeline` browsing is unaffected. Only a row that's
future-dated (or in progress) *and* no longer present on the source
calendar is considered stale. `source="MANUAL"` rows are never touched by
this — same guard as manual delete. Anything pruned is logged
(`Pruned N record(s) no longer on the source calendar: ...`).

## Manually adding / deleting records

The `/calendar` debug view has forms under "Manually Add a Record":

- **Add Flight (Manual Title)** takes a title in the exact same format the
  ICS feed uses (`UA123 SFO 19Aug 0830 - ORD 19Aug 1420`) plus optional
  gates, and runs it through the identical parser the ICS feed does
  (`manual_entry.py` -> `parser.build_flight_event()`) — same validation,
  same airport-timezone handling, no separate looser path to keep in sync.
  Always available.
- **Add Flight (via AeroAPI)** — only shown when Flight Info Source is set
  to AeroAPI — takes just a flight number, departure station, and
  departure date. `manual_entry.add_manual_flight_via_aeroapi()` looks it
  up on AeroAPI (`aeroapi_client.find_flight_for_new_record()`) and builds
  the whole record from the result: route, scheduled times, and any live
  OOOI/delay/status data already available, seeded immediately rather than
  waiting for the next scheduled poll. The departure station matters
  because a flight number can operate more than once on the same day
  (different routes) — station + date together pins down the right one;
  matching by date alone isn't enough. Optional gate fields here override
  whatever AeroAPI itself reports for gates, if anything.
- **Add Day Off / Block** takes a code (e.g. `OFF`) and a start/end date,
  building a `BLOCK`-type record the same way a multi-day ICS entry would
  (see "Multi-day block entries" above) — but without the ICS parser's
  2-day minimum, since this is a deliberate action, not a guess.

Manual writes go straight to `flight_events` tagged `source="MANUAL"`,
alongside whatever the ICS feed has already stored (`source="ICS"`) — they
show up on the board, in `/api/timeline`, everywhere, exactly like a real
synced record. The only difference: a `Delete` button appears in the debug
table's `Actions` column **only** for `MANUAL` rows.
`storage.delete_manual_event()` enforces `source = 'MANUAL'` in the SQL
itself, not just by hiding the button — so even a hand-crafted request
naming an ICS record's key deletes nothing. Manual add/delete always act on
real stored data regardless of whichever "Data source" toggle you're
currently viewing.

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
`/calendar` (no manual `.env` editing, no restart needed to take effect) —
masked everywhere it's displayed back (`aeroapi_sync.masked_api_key()`),
and only ever sent as the `x-apikey` header on outbound AeroAPI requests. A
"Poll AeroAPI Now" button triggers an immediate check outside the normal
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
also mean re-entering the key by hand.

**Display rule** (`models.FlightEvent.effective_dep_dt_utc()` /
`effective_arr_dt_utc()`): the published/scheduled time shows until AeroAPI
reports something newer —

1. Once AeroAPI reports a delay (`departure_delay`/`arrival_delay` non-null),
   the delay-adjusted estimate (`estimated_out`/`estimated_in`) replaces the
   published time.
2. Once the flight actually happens (`actual_out`/`actual_in` non-null),
   that replaces the estimate — the real thing that happened always wins.

**Status ladder** (`FlightEvent.live_status()`), most specific first:
`CANCELLED`/`DIVERTED` (from AeroAPI's own flags) → `ARRIVED` (`actual_in`,
at the gate) → `LANDED` (`actual_on`, wheels down but not yet at the gate —
a distinct status from `ARRIVED`, since that gap can be significant) →
`EN ROUTE` (`actual_off`) → `TAXIING/LEFT GATE` (`actual_out`) → `DELAYED`
(a delay is known but nothing's happened yet) → `ON TIME`.

**Polling cadence** (`aeroapi_sync._cadence_for()`) is dynamic per flight,
on its own 60-second scheduler separate from the 15-minute ICS refresh, and
is keyed off **each flight's own OOOI progress — not which board slot
(current/next) it happens to occupy**, except for the pre-departure phase:

- Not yet departed, *either slot*: **nothing at all** until within
  `_PRE_DEPARTURE_TRACKING_WINDOW` (24 hours) of the delay-adjusted
  estimated departure (or scheduled, if no delay is known yet) — gates,
  delays, and estimates essentially never exist any earlier than that, and
  capping this is what keeps the AeroAPI query budget bounded per flight
  instead of scaling with however long a flight sits on the board before
  its own departure (behind a long block, or simply as a "next" flight
  days out). Within that 24h window: every **60 minutes** until within
  `_PRE_DEPARTURE_MEDIUM_WINDOW` (6 hours) of departure — gates/delays
  essentially never appear that early either, so the first ~18 hours of
  the window would otherwise just be wasted queries — then every
  **15 minutes**, tightening to every **1 minute** once within 15 minutes
  of departure. Only that final tightening is *current-slot-only*, since
  imminent departure only matters for whichever flight is actually pinned
  at the top of the board right now; the 60min/15min tiers apply to both
  slots equally. Every flight passes through this same staged check
  regardless of slot as it approaches its own turn, so nothing is ever
  permanently skipped, just deferred until it's worth a query.
- `actual_out` known, `actual_off` not yet: every **1 minute**.
- `actual_off` known, `actual_on` not yet (airborne): every **60 minutes**
  until within `_AIRBORNE_FAR_WINDOW` (2 hours) of `estimated_on`
  (AeroAPI's own estimated *touchdown* time — not `estimated_in`, which is
  the later estimated *gate* arrival, including taxi-in; falls back to
  scheduled arrival only if no live estimate exists yet at all), then
  every **5 minutes**, tightening to every **1 minute** once within
  10 minutes of touchdown — same staged-tightening idea as the
  pre-departure case above, applied to touchdown instead. Without the
  final tightening, `actual_on` landing anywhere in the middle of the
  5-minute gap between polls (which is most of the time) would sit
  undetected for up to the full 5 minutes before the board's pill
  updates; without the 2-hour coarsening, a long flight would poll every
  5 minutes for hours before there's anything to find. The 2-hour/5-minute
  tiers mainly matter for longer flights — a short hop is already inside
  the 2-hour window well before it's even airborne. 10 minutes (not 15)
  for the final tightening specifically because `estimated_on` is a
  tighter, more accurate reference point than `estimated_in` was — a
  flight touching down more than 10 minutes ahead of AeroAPI's own
  touchdown estimate is unlikely.
- `actual_on` known, `actual_in` not yet: every **1 minute**.
- `actual_in` known: fully resolved, polling stops for that record — and
  the board's 15-minute "still current" grace countdown
  (`ARRIVAL_GRACE_MINUTES`) counts from that *real* arrival moment, not the
  originally published one (`flight_state.select_current_and_next(...,
  use_live_times=True)`), so a delayed flight keeps its full grace window
  measured from when it actually landed.

The last four rules apply **regardless of slot**: a flight that's already
departed keeps its fast/phase-based cadence even if it ends up sitting in
"next" — which happens whenever a long-spanning block is occupying
"current" while an ordinary flight, already in progress, chronologically
follows it. Without this, an already-airborne flight parked in the "next"
slot would get throttled to the slow 15-minute cadence meant for flights
that haven't happened yet, and something like `actual_on` could sit
unpolled for a long stretch even though a poll was due.

AeroAPI's own results (`aeroapi_client.fetch_flight_status()`) are written
via `storage.update_aeroapi_fields()`, a plain `UPDATE` that's deliberately
a *separate write path* from the ICS upsert (`storage.save_events()`) — so
a routine 15-minute ICS refresh can never wipe out live data that's already
been polled for that flight.

## Time-basis tags, gate states, and the delayed/cancelled blink

Three small pieces of the board, all AeroAPI-mode only (Local Timing
always shows plain scheduled times/gates, no tags):

- **SCH / EST / ACT** — a small tag sits just left of each time/date pair,
  labeling exactly which basis that time reflects: `SCH` (still the
  published schedule), `EST` (a delay-adjusted estimate — `estimated_out`/
  `estimated_in`), or `ACT` (the real `actual_out`/`actual_in`). It's the
  same priority chain `FlightEvent.effective_dep_dt_utc()`/
  `effective_arr_dt_utc()` already use for which time to *show* — see
  `app._time_basis()` — just exposed as a label rather than left implicit.
- **NO GATE / CNTCT OPS / an actual gate** — a gate value (AeroAPI or
  manually entered) always wins and displays regardless of query status.
  Absent that: `NO GATE` means this flight has never been queried yet
  (outside the 24h pre-departure tracking window, or a BLOCK); `CNTCT OPS`
  means it has been queried and AeroAPI simply hasn't published one yet.
  See `board.html`'s `fmtGate()`.
- **Delayed/cancelled blink** — a `DELAYED` or `CANCELLED` status pill
  alternates every 1 second between its normal outline style and an
  inverted fill (solid color, dark text) via the `status-blink` CSS class
  and `pill-inverse-blink` keyframes in `static/style.css`. Scoped to
  exactly those two statuses, not `DIVERTED` or anything else.

**Minimum-connection warning** — row 2 (whatever's immediately after the
live current flight) flashes a full-width amber "MINIMUM CONNECTION" pill
every 5 seconds (visible for 2 of those) when the gap between row 1's
arrival and row 2's departure — using whichever times are actually
displayed, live or scheduled — is under 50 minutes. Both rows must be
genuine `FLIGHT` records (a `BLOCK` has no real connection to warn about),
and it's live-mode only — browsing history with the nav buttons suspends
it, same reasoning as the tags/gates above. See `minConnectionRow2Key()`/
`tickMinConnectionAlert()` in `board.html` and `.min-connection-overlay`
in `static/style.css`.

## Airline column

Every row's Airline column always shows [static/ua_white.png](static/ua_white.png)
— this board is built for a single-carrier (United) schedule, so the logo
isn't looked up per-flight. It's a processed version of
[static/ua.png](static/ua.png) (the original color/badge asset, kept
alongside it) with the dark badge background made transparent, leaving just
the white globe mark + wordmark so it sits cleanly on the navy board. If
this ever needs to vary by carrier, that's a `flight.airline_code -> logo`
lookup in `renderRow()` in [board.html](templates/board.html) plus
per-carrier assets in `static/`.

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
- AeroAPI's gate/terminal field availability hasn't been confirmed against
  a live key — `aeroapi_client.py` reads them opportunistically
  (`gate_origin`/`gate_destination`) and just falls back to "No Gate" if
  they're not present in the response.
- The dev server (`app.run()` via `python run.py`) is Flask's built-in
  server — fine for a household wall display, not meant for
  internet-facing production use.
