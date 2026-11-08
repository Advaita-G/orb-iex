# ARCHITECTURE: ORB-IEX

```
cli.py ──> preflight.py (fail-closed account/environment checks)
   │
   └─> runner.py  (schedule loop, one thread)
         ├─ calendar.py    Alpaca /v2/calendar -> Session, Schedule (America/New_York)
         ├─ clock.py       SystemClock / FakeClock (tests, replays)
         ├─ marketdata.py  free data only, SQLite cache, batching, paging, SIP-age guard
         ├─ universe.py    monthly top-200 by prior-day dollar volume
         ├─ indicators.py  ATR(14) SMA, ADV(14), relative opening volume
         ├─ strategy.py    filters, ranking, direction, entry levels, sizing (pure)
         ├─ engine.py      order state machine, EOD, sweeps, recovery
         │     └─ broker.py   AlpacaPaperBroker: the ONLY order-write choke point
         │        sim.py      SimBroker: same interface, local fills (shadow, tests)
         ├─ stream.py      trade_updates WebSocket (thread) -> queue
         └─ journal.py     SQLite: every decision and order, for restarts
http.py        GET-only client (host allowlist, rate limits, retries, no redirects)
safety.py      host checks, paper-account check, kill switch
secrets.py     keys from env/.env only; refuses a live APCA_API_BASE_URL
logging_setup  console + JSON lines, secret redaction, variant=ORB-IEX on every line, log retention
ops.py         unattended ops: disk guard, instance lock, private .env, heartbeat, health, review
deploy/oracle  systemd unit orb-iex-paper, journald cap, install.sh, orbctl
```

## Data flow

1. **Prep** (after the previous close + 17 min, normally pre-open):
   - every tradable NYSE/Nasdaq listing (Alpaca assets);
   - regular-session daily bars for the last 20 sessions, from SIP 30-minute
     bars (09:30 to close only), for all of them; keep ADV(14) >= 1M and
     ATR(14) > $0.50 (the day's eligible list, saved in the journal);
   - IEX 09:30–09:35 bars for the last 30 sessions for the eligible names;
   - asset flags (tradable, shortable, easy_to_borrow).
   All past-session data is cached in `runtime/cache.sqlite`.
2. **Scan 09:35:03**: IEX 5-minute requests (200 symbols each) for all eligible names, then
   features -> filters -> rank -> top 20 -> entry plans -> engine.
3. **Orders**: entry = DAY stop order at the range high (long) or low (short).
   First partial fill cancels the remainder. After terminal confirmation and
   broker reconciliation, one stop covers the true bot-owned position, at
   average fill price ± 0.10 × ATR. See the October 2 hardening handoff.

## Event handling

- Order updates come from the stream AND from a REST poll every
  `order_poll_seconds` (every 2 s during the EOD wait). Both give snapshots with
  cumulative `filled_qty`. `engine._apply` merges a snapshot:
  - fill delta = new cumulative − known cumulative (never negative);
  - a status can only move forward (pending -> working -> terminal);
  - so duplicate and out-of-order updates change nothing.
- A fill is stored once per (order_id, cumulative qty) in `fills`.
- Stop resize = cancel-confirm-new, including racing fills. No engine PATCH
  path remains. Stops count as protected only in `new` with matching side/qty.
- `MutationGate` persists intent before every HTTP write attempt. Unresolved
  outcomes block conflicting mutations and new entries until reconciled.
- After a stream reconnect the runner reconciles over REST at once.

## Restart and recovery (`engine.recover`)

1. Pull broker order/position truth and merge silently (no side effects).
   Orders with an `orbiex-YYYYMMDD-...` id that the journal does not know are
   adopted, including old-day entries. Resolve durable intents first.
2. For each planned trade: a missing entry is looked up by client_order_id,
   and submitted only while still inside the scan grace window.
3. For each trade: stop filled -> closed, cancel the entry remainder. Stop
   lost while offline -> flatten. Position without a matching stop -> resize
   the stop, or recreate it at the journaled price, or flatten if no price.
4. An ORB-IEX position with no plan today (e.g. an overnight leftover):
   flattened during the session, left for the next 09:31 sweep outside it.
   Positions not from ORB-IEX fills are never touched.
5. The scan marker means a day is never scanned twice. After the close, only
   cleanup runs.

## Modes

| mode | broker | orders |
|---|---|---|
| signal | none | logged as INTENT only |
| shadow | SimBroker (IEX 1-min bars) | simulated, never sent |
| paper | AlpacaPaperBroker (armed after preflight) | Alpaca paper |

`--date` on signal/shadow replays a finished session on a fake clock, with
real historical free data and a throw-away journal in `runtime/replay/`.

## Ownership

`Journal.owned_qty()` = net signed shares from fills of ORB-IEX orders (all
days) plus `owned_adjust`. `Engine.bot_positions()` caps that by the Alpaca
position (same side, never larger). EOD, sweeps, flattens and recovery use
only `bot_positions()`. The cap is saved only pre-open or after the close,
when no fill can be in flight.

## Trading journal (`tradelog.py`)

A separate, append-only SQLite record (UPDATE/DELETE blocked by triggers,
writes are INSERT OR IGNORE on natural keys): `runs`, `candidates` (with the
rule checklist and skip reason), `plans`, `exec_events`, `outcomes`,
`day_summaries`; plus `strategy_changes.sqlite`. The engine and runner append
to it; nothing reads it back for decisions. Outcomes are built after the close
from the broker truth (state journal orders/fills) and IEX 1-minute bars for
MFE/MAE. Exports: daily CSV + Markdown, weekly and monthly Markdown.

## Journal tables

`days` (scan marker, session_closing, done events, EOD status, summary),
`universe`/`universe_meta` (daily eligible list), `candidates`, `trades`, `orders` (with
`replaces`/`replaced_by`), `fills`, `diagnostics` (09:35 vs 09:37 bars).

## October 3 independent audit

The existing per-symbol mutation gate remains the only broker-write boundary. Reconciliation now cancels recovered exits whose side, count or remaining quantity disagrees with broker position, and cancels wrong-side stops immediately. Pending cancellation must finish before replacement exits. The runner also reconciles a normal paper-mode return; an incomplete graceful shutdown raises `FatalRunnerError` instead of producing a clean exit.

A second `InstanceLock` is keyed by account fingerprint under `~/.cache/orb_iex/accounts/`, so changing runtime directories cannot evade same-user, same-host exclusion. It is not a distributed lock. See [the overnight report](OVERNIGHT-REPORT-2026-10-03.md) for remaining guarantee limits and research findings.
