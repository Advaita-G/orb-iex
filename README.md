# ORB-IEX: Alpaca paper-trading bot

A port of an opening-range breakout ("Stocks in Play") strategy from a published
quantitative research paper (its QuantConnect `Main.cs`, not included here)
to an **Alpaca PAPER** account, using **free Alpaca Basic data only**.
Live-money trading is impossible by design (see `docs/SAFETY.md`).

## Setup (macOS / Linux)

```
cd orb-iex
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env && chmod 600 .env   # paste your PAPER keys into .env
```

## Commands

```
pytest                                   # offline tests, no keys needed
python -m orb_iex check-config           # settings + keys present, no network
python -m orb_iex check-alpaca           # read-only account and data checks
python -m orb_iex signal --date 2026-09-24   # replay a past session: signals only
python -m orb_iex shadow --date 2026-09-24   # replay with the local fill simulator
python -m orb_iex signal                 # live: log signals, no orders
python -m orb_iex shadow                 # live: signals + simulated fills, no Alpaca orders
python -m orb_iex paper                  # live: real Alpaca PAPER orders
python -m orb_iex status                 # today's paper journal (trades, EOD state)
python -m orb_iex health                 # bot running, Alpaca, calendar, journal, data age, disk
python -m orb_iex review                 # daily review: trades, P&L, PF, R, MFE/MAE, slippage
```

Start `paper` any time. The bot then waits. It prepares data before the
open, runs the 09:31 sweep, scans at 09:35:03 ET, manages stops, and
closes everything from 15:59 ET. It runs every trading day until you
press Ctrl+C. After a restart it resumes from `runtime/journal.sqlite`.
The first run downloads about 20 sessions of history for every eligible
listing (several minutes). Later runs only add the newest day.

Exit codes: 0 ok, 1 a required check failed, 2 safety stop, 3 config error
(also a `.env` that is not mode 600 with `--require-private-env`), 4 network
error, 5 disk nearly full, 6 another instance is already running.

## What happens each trading day

| ET time | Action |
|---|---|
| pre-open | load calendar; daily universe = all tradable NYSE/Nasdaq listings with ADV(14) >= 1M and ATR(14) > $0.50 (SIP history); IEX opening-bar history |
| 09:31 | open sweep: close any leftover position |
| 09:35:03 | IEX opening bars for all eligible names (200 per request), rank, place up to 20 stop entries |
| ~09:37 | diagnostic re-request of the opening bars (logged only, never trades) |
| on fills | protective stop at average fill price ± 0.10 ATR, sized to the filled quantity |
| 15:59:00 | cancel entries and stops, wait for confirmation (max 20 s), close positions |
| 15:59:40 | close sweep |
| after close | verify zero positions, write the daily order summary |

Early closes shift the last three rows (for example 12:59:00 / 12:59:40 / 13:00).
Sizing: 1% of (equity / 20) at risk per trade, each position at most 4 x equity / 20
(4x gross in total).

## Trading journal

Every run writes an append-only record (SQLite) and, after each close, CSV and
Markdown exports. It is a record only: the bot never reads it to make decisions.

| What | Where (default `journal.dir = "runtime/journal"`) |
|---|---|
| Journal (append-only) | `trade_journal.sqlite` (paper), `trade_journal_shadow.sqlite`, `trade_journal_signal.sqlite` |
| Daily exports | `exports/daily/trades_<day>.csv`, `candidates_<day>.csv`, `events_<day>.csv`, `summary_<day>.md` / `.json` |
| Weekly / monthly | `exports/weekly/summary_<YYYY>-W<ww>.md`, `exports/monthly/summary_<YYYY-MM>.md` |
| Strategy change log | `strategy_changes.sqlite` (append-only, written by a person) |
| Replays | `runtime/replay/<day>-<mode>-trade_journal.sqlite` and `<day>-<mode>-exports/` |

Recorded: each run (commit, version, config hash, equity/cash, bot positions,
limits, feeds), every scanned candidate with its rule checklist and exact skip
reason, each trade plan (trigger, stop, qty, exposure, risk $ and %, 1R),
every execution event (submit, fill, partial fill, replace, cancel, reject,
recovery), and each trade outcome (exit, P&L gross/net, R, MFE/MAE, holding
time, rules_followed / execution_clean / data_complete, deviation tags).

```
python -m orb_iex journal-export --date 2026-09-28   # rebuild exports
python -m orb_iex changelog add --rule max_gross_exposure --old 1 --new 4 \
    --reason "paper: 4x max leverage" --evidence "163 tests; replay 2026-09-24"
python -m orb_iex changelog list
```

On the Oracle VM the bot uses `config/settings.oracle.toml`: all runtime data
(journal, logs, cache, state, heartbeat, kill switch) lives under
`/home/opc/orb_iex_data`, the journal under `/home/opc/orb_iex_data/journal`.

## Journal dashboard (read-only)

A local diagnostic dashboard over the journals above. It answers four questions:
is the bot safe and reconciled, did it execute the strategy correctly, is there an
edge after realistic costs, and what deserves investigation.

```
python -m orb_iex journal-serve            # http://127.0.0.1:8787 (--port, --open)
python -m orb_iex journal-serve --mode paper   # which journal to read (paper default)
python -m orb_iex journal-snapshot         # every endpoint as one JSON file
```

Pages: Today (the day's result in one sentence, each trade with its verdict, open positions,
bot status), Trades (filterable list, each row opening the trade's fills and step-by-step record),
Calendar, Mistakes (the loss journal: bot mistakes with fix status, bad-luck losses, and losses
not reviewed yet), Research (summary, breakdowns, the skipped-trade funnel, paper vs backtest) and
The bot (safety stops, today's timeline, health checks, recurring problems).

Daily loss review: `docs/DAILY-REVIEW.md` is run after every weekday close by launchd
(`deploy/macos/com.orbiex.daily-review.plist` → `deploy/macos/daily-review.sh`). It judges each
loss as bad luck or a bot mistake in `docs/loss-journal.jsonl`, makes one tested fix on a
`daily/...` branch in a separate worktree for the owner to approve, and writes
`runtime/reports/daily-review-<day>.md`. It never touches the running bot, orders, or remotes.

Rules it follows, and that the tests enforce:

- **Read-only.** Every database is opened `mode=ro`, the code imports no order path,
  and all write methods return 405. It cannot place, cancel or replace an order, and
  cannot change strategy or risk settings.
- **One trade is one position lifecycle** (flat to flat, carry-overs included), built
  from raw fills and checked against `orb_iex.reconcile`, not one order.
- **Three P&L views stay apart**: broker/PAPER realized, modeled cost, modeled net.
  Modeled slippage is never added to the slippage already inside the fills. Fees are
  `null`, never `0`. R uses the risk frozen in the plan, not a later stop.
- **Missing is missing.** An absent plan, ATR, quote or session is shown as
  unavailable with the data the bot would have to record; nothing is silently zeroed.
- **Ratios need a sample.** Sharpe/Sortino need 20 sessions, bucket statistics need
  5 trades, and the UI says so instead of printing a number.
- The backend is stdlib-only (`http.server`, `sqlite3`) and binds to localhost; the
  frontend is plain HTML/CSS/JS with no build step and no external requests.

## Oracle VM (unattended paper)

Setup, systemd service `orb-iex-paper` and the `orbctl` commands are in
`docs/RUNBOOK.md` ("Oracle VM"). Files are in `deploy/oracle/`.

## Files

- `config/settings.toml`: strict settings. Unknown keys fail.
- `config/settings.oracle.toml`: the Oracle VM settings (paper, data under `/home/opc/orb_iex_data`).
- `deploy/oracle/`: install script, systemd unit, journald limits, `orbctl`.
- `runtime/`: logs (`logs/*.jsonl`), journals (`journal.sqlite` for paper), data
  cache, reports. Git ignores this folder.
- `docs/ARCHITECTURE.md`: how the parts fit together.
- `docs/DIVERGENCES.md`: every difference from the QuantConnect backtest.
- `docs/SAFETY.md`: the paper-only guards.
- `docs/RUNBOOK.md`: daily operation, kill switch, restarts, troubleshooting.
