# RUNBOOK: ORB-IEX paper trading

## Persistent PAPER on this Mac: `paper-supervise`

The operator authorized persistent PAPER operation on 2026-10-15. The installed
`~/Library/LaunchAgents/com.orbiex.paper.plist` runs `paper-supervise` from
`~/orb-iex`; its checked-in copy is
`deploy/macos/com.orbiex.paper.plist`. No credentials are stored in the plist.
The supervisor relaunches after each session and backs off after failures.
macOS restarts the supervisor if it exits and starts it at user login.
Every child launch still validates the existing PAPER safety gates.

```sh
# Install/load once while logged into this Mac (do not start another copy).
cp deploy/macos/com.orbiex.paper.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.orbiex.paper.plist
# Inspect the service and current bot heartbeat.
launchctl print gui/$(id -u)/com.orbiex.paper
cat runtime/health/heartbeat.json
# Intentionally stop the persistent service; prevents automatic restart.
launchctl bootout gui/$(id -u)/com.orbiex.paper
```

Output goes to `runtime/logs/paper-supervise-console.log` and the normal daily
JSON logs. Keep the Mac plugged in, logged in, lid open, and online. Caffeinate
prevents idle sleep; it cannot keep a powered-off or closed-lid Mac trading.
After a reboot, the LaunchAgent starts at login. A missed opening scan still
blocks entries for that day; persistence does not bypass KILL or other gates.

## One-launch PAPER session: `paper-auto`

**Full post-fix PAPER acceptance remains pending. Persistent PAPER was authorized
on 2026-10-15; real money is not approved.**
This command targets one current/next exchange-calendar session, then exits. It reuses the normal
ORB scan, sizing, execution, cutoff and EOD engine. It requires the existing private `.env`, PAPER
host, configured account allowlist and owner, and the same account/journal locks as `paper`.

The internal entry gate starts closed. Broker/journal reconciliation and integrity checks precede
recovery; owned overnight exposure is flattened through the existing recovery path only during a
verified regular session. Pre-open polling waits up to 60 seconds without submitting flatten orders.
Entries require fresh account/owner/clock checks, readable broker state, zero account positions,
zero bot/open foreign orders, zero unresolved intents, healthy journal/schema, no safety breakers,
and the existing candidate history/corporate-action/quote/spread checks. Foreign inventory is
preserved and blocks entries. Ambiguous recovery, data, ownership, broker or EOD outcomes fail closed.

`runtime/KILL` is exclusively an external human brake: the program never removes, renames or
overrides it. Its presence permits safe owned-risk recovery but prohibits entries. Activation
blocks entry submissions at the engine and the broker HTTP boundary, cancels owned entries even
off-market, and latches entries off for that process even if a human later removes the file.
A fresh launch must validate every gate again. Persistent unrelated safety breakers require review.

For a supervised acceptance run on this Mac:

```sh
cd ~/orb-iex
ORB_IEX_INSTANCE_ID=orb-mac-supervised .venv/bin/python -m orb_iex paper-auto
```

One-launch background command, **only after supervised acceptance approves unattended PAPER**:

```sh
(cd ~/orb-iex && mkdir -p runtime/logs && ORB_IEX_INSTANCE_ID=orb-mac-supervised nohup /usr/bin/caffeinate -is .venv/bin/python -m orb_iex paper-auto >> runtime/logs/paper-auto-console.log 2>&1 < /dev/null &)
```

Keep the Mac powered, lid open and network connected. With the current KILL present, this command
will recover owned risk and keep new entries blocked; it cannot turn into a normal trading run.
Only the human operator can remove KILL after reviewing acceptance and remaining breakers.
Do not reset the journal or delete breaker rows to obtain a green gate.

Atomic, fsynced `runtime/health/paper-auto.json` records startup, fingerprint suffix, owner,
transitions, recovery, CTVA/SPHD quantities, entry authorization, breaker history, counts, cutoff,
EOD, final broker positions/orders, unresolved intents and `flat_gate`. Startup refusal is separately
recorded in `runtime/health/paper-auto-startup-failure.json`. A final `SAFE` requires strict shutdown
confirmation of flat/no orders and no latched failure; incomplete shutdown exits nonzero and records
`FAILED CLOSED`. Unknown broker state is reported as unknown, never as zero.

Acceptance must exercise this exact command through real PAPER recovery and the full session
lifecycle, preserving logs and report at the tested Git SHA. A simulator pass or a closed-market
read-only account snapshot does not satisfy that acceptance.

Local verification on 2026-10-04: full `python -m pytest -q` passed **569 tests**
(536 inherited + 33 orchestrator cases), zero skips/xfails/deselections. All inherited
test files are byte-identical to `c87e2710`. The incident/chaos subset separately passed
203 tests. The combined deterministic run recovered CTVA/SPHD, used the normal ORB scan,
performed cutoff/EOD and finished `SAFE` with a verified flat report. Real acceptance is pending.

## Start

One-time setup on the Mac (one paper account per machine: this Mac has its OWN Alpaca paper account and keys, the VM
has a different one; never copy `.env` or the allowlist line between them):
1. Put this Mac's paper keys in `.env` and add the line `ORB_IEX_INSTANCE_ID=mac`.
2. `python -m orb_iex account-fingerprint` (read-only) prints the fingerprint of the account behind those keys.
3. In `config/settings.toml` `[safety]` set `allowed_account_fingerprints = ["<that value>"]` and
   `account_owner_instance_id = "mac"`. Until then `paper` refuses to start (exit 2). Wrong keys, a copied config or
   the wrong machine id also stop it at startup, before any order.

```
cd orb_iex && source .venv/bin/activate
python -m orb_iex paper
```

Start it any time before 09:30 ET (the first run needs several minutes to download history), and keep the computer awake with a
network connection. It runs every trading day until Ctrl+C. To stop it
from sleeping on macOS, use `caffeinate -i python -m orb_iex paper`.

What you should see:
- `preflight OK mode=paper account=...XXXX`
- `paper order submission ENABLED on paper-api.alpaca.markets`
- `trade_updates stream connected`
- `prepared <day>`, then at 09:35:03 the `CANDIDATES` table and `SIGNAL` lines.

## Stop

- **Ctrl+C / SIGTERM (`orbctl stop`).** New entries are blocked at once, every unfilled ORB-IEX entry order across dates
  is cancelled and confirmed terminal at Alpaca (up to `runtime.shutdown_timeout_seconds`, default 20 s; manual orders
  are never touched), then the bot exits. A fill that wins the cancel race is protected or flattened before exit.
  Confirmed working, correctly sized stops of existing positions may remain. This does not mean flat.
  Unresolved intents or unmanaged positions prevent successful shutdown. The result is in `health/shutdown.json` and
  the journal events `shutdown_requested`, `entry_cancel_requested`, `entry_cancel_confirmed`, `fill_won_cancel_race`,
  `position_protected_on_shutdown`, `position_flattened_on_shutdown`, `shutdown_incomplete`. If you see
  `SHUTDOWN INCOMPLETE`, look at the Alpaca paper dashboard for live `orbiex-` orders and open positions.
  Everything is saved in `runtime/journal.sqlite`; a restart recovers.
- If you stop during the day and do not restart: Alpaca DAY orders expire at
  the close, but open **positions stay open overnight**. The next start runs
  recovery and blocks entries until the bot-owned exposure is resolved. Review only
  bot-owned exposure in the Alpaca paper dashboard; account-wide actions can touch manual holdings.

Read [the October 2 execution handoff](HARDENING-2026-10-02.md) before an
updated start. Back up the runtime journal before its additive migration.
Offline fixtures do not close actual account positions; the last snapshot
still held CTVA -72 and SPHD -409. Supervised drills remain required.

## Kill switch (no new entries)

```
touch runtime/KILL      # block new entries; stops and exits keep working
rm runtime/KILL         # allow entries again (from the next scan)
```

## Restart

Just run `python -m orb_iex paper` again. The bot:
- never re-runs a scan that already ran today (and skips it after 09:36:03);
- re-adopts entries and stops by client_order_id;
- recreates or resizes a missing or short stop, or flattens if it cannot;
- runs the EOD sequence late if restarted between 15:59 and 16:00;
- after the close, only checks for leftovers and writes the summary.

## Daily checks

```
python -m orb_iex status          # today's trades and EOD state
```

- Log file: `runtime/logs/orb_iex-YYYYMMDD.jsonl` (one JSON object per line).
  Useful events: `candidates`, `signal`, `order_intent`, `fill`,
  `entry_skip`, `data_divergence`, `order_summary`, `overnight_positions`.
- `flat_check: zero overnight positions confirmed` must appear after the close.

## Trading journal review

- After each close the bot writes `<journal.dir>/exports/daily/summary_<day>.md`
  (plus trades/candidates/events CSVs) and refreshes the weekly and monthly
  summaries. Rebuild any day with `python -m orb_iex journal-export --date D`.
- The journal is append-only. Rows are never updated or deleted; a restart
  that sees the same event again adds nothing.
- Deviation tags on each trade: DATA_STALE, DATA_MISSING, ENTRY_LATE,
  ENTRY_SLIPPAGE, PARTIAL_FILL, STOP_REPLACED, STOP_REJECTED, EOD_EXIT,
  SHORT_UNAVAILABLE, RECOVERY_EVENT, DUPLICATE_EVENT, BROKER_REJECT,
  MANUAL_INTERFERENCE.
- Rule changes are never made from journal results automatically. When you
  decide to change a rule, record it: `python -m orb_iex changelog add ...`.

## Comparing with QuantConnect

- `python -m orb_iex shadow --date YYYY-MM-DD` replays a finished day with
  free data. The report is in `runtime/reports/replay-<day>-shadow.json`.
- Fills carry modeled commission ($0.0035/sh) and slippage ($0.01/sh), so
  paper P&L can be compared with the backtest cost model.

## Oracle VM (unattended paper)

One-time setup as user `opc` on a fresh Oracle Linux 8/9 VM:

```
sudo dnf -y install git
git clone https://github.com/Advaita-G/alpaca-orb-bot.git /home/opc/alpaca-orb-bot
bash /home/opc/alpaca-orb-bot/orb_iex/deploy/oracle/install.sh
nano /home/opc/orb_iex_data/.env          # APCA_API_KEY_ID / APCA_API_SECRET_KEY (paper keys)
chmod 600 /home/opc/orb_iex_data/.env
orbctl check                              # read-only Alpaca check, no orders
orbctl start
```

`install.sh` installs git, python3.11 and chrony, creates the venv, runs the
offline tests, creates `/home/opc/orb_iex_data` (mode 700), installs the
systemd unit `orb-iex-paper`, the journald size cap and `/usr/local/bin/orbctl`.
It enables the service (starts at boot) but does not start it. It never places orders.

| Command | What it does |
|---|---|
| `orbctl start` / `stop` / `restart` / `status` | control `orb-iex-paper` (systemctl) |
| `orbctl logs` | live logs (`journalctl -u orb-iex-paper -f`) |
| `orbctl health` | bot running, paper account, calendar, journal writable, last data update, disk |
| `orbctl review [--date D]` | daily review from the trading journal |
| `orbctl summary [D]` | today's journal summary (`journal/exports/daily/summary_D.md`) |
| `orbctl kill-on` / `kill-off` | block / allow new entries |
| `orbctl update` | stop, pull main, install, test, start |

Unattended behaviour:
- Keys only in `/home/opc/orb_iex_data/.env`. The service passes
  `--require-private-env`: the bot refuses to start unless the file is mode 600.
- `Restart=always` after 5 s, forever (crash, network loss, disk low at start). Exit 7 = fatal runtime error: the bot
  already blocked entries, protected or flattened its positions and saved `health/fatal.json`; the restart recovers.
  Exit 2 (safety stop) and 3 (config error) are not restarted: fix, then `orbctl start`.
- First start on a machine: run `python -m orb_iex --config config/settings.oracle.toml --env-file
  /home/opc/orb_iex_data/.env account-fingerprint`, put the value in `safety.allowed_account_fingerprints`, and make
  sure `ORB_IEX_INSTANCE_ID` (service unit) equals `safety.account_owner_instance_id`. Until then the service exits 2.
- One paper account per machine: the Mac and the VM use different Alpaca paper accounts, keys and fingerprints; each
  settings file allowlists only its own account (see docs/SAFETY.md "Cross-machine"). Never copy `.env` or the
  allowlist line from one machine to the other.
- Waits for `network-online.target` and `time-sync.target` (chrony).
- One instance only (lock file `orb_iex.lock`; a second start exits 6).
- `systemctl stop` sends SIGTERM: the same clean shutdown as Ctrl+C. Open
  protective stops stay at Alpaca. A restart resumes from the state journal.
- Disk: start refused below `runtime.min_free_disk_mb` (exit 5). While running,
  a low disk blocks new entries only (`disk_low`); stops and exits continue.
- Logs: the bot gzips JSON logs after 1 day and deletes them after
  `runtime.log_retention_days`. journald is capped at 300 MB.
- Heartbeat: `health/heartbeat.json` every 30 s, read by `orbctl health`.

## Troubleshooting

| Message | Meaning / action |
|---|---|
| `SAFETY STOP: ... not the Alpaca paper host` | `APCA_API_BASE_URL` points to live. Remove it. |
| `SAFETY STOP: account number does not start with 'PA'` | These are not paper keys. Use paper keys. |
| `SAFETY STOP: trading.enable_paper_orders is false` | Set it to `true` in `config/settings.toml` to allow paper orders. |
| `SAFETY STOP: clock skew` | Sync the system clock (NTP), then restart. |
| `prep_failed` | Network or data problem. It retries every minute. With no prep, there is no scan and no trade. |
| `scan_missed` | The bot was not running at 09:35. No entries today (by design). |
| `stream down ... reconnect` | Reconnects by itself. The REST poll keeps orders in sync meanwhile. |
| `flatten_gave_up` | 5 exit attempts failed. Close the position in the Alpaca dashboard. |
| `SAFETY STOP: ... allowlist` / `... belong to` / `ORB_IEX_INSTANCE_ID is not set` | Account fingerprint not allowlisted, or this machine is not the declared owner. Run `account-fingerprint`; fix the settings or the machine id. |
| exit 7 / `fatal_error` | Unexpected exception. Read `health/fatal.json` and the CRITICAL log line (traceback). Positions were protected or flattened; the service restarts in 5 s. Repeated exit 7 = a bug: stop the service (`orbctl stop`) and look. |
| `entry_skip ... stale_quote` | The IEX quote was older than `trading.max_quote_age_seconds` (default 2 s); the entry was skipped and journaled. |
| `entry_skip ... skip_quote_unavailable` | No usable quote (none, one-sided, locked book, request failed). Nothing was sent; the signal is skipped for the day. The journal row `entry_quote` says why. |
| `SHUTDOWN INCOMPLETE` (CRITICAL) | A stop (Ctrl+C / SIGTERM) could not confirm every entry cancel or flatten within the timeout. Check Alpaca for live `orbiex-` orders and open positions; `health/shutdown.json` has the detail. |
| `fatal_cancel_entries` / `fatal_cancel_unconfirmed` | The process died on an error and cancelled its unfilled entries (`health/fatal.json` has the result). `unconfirmed` = Alpaca had not confirmed yet; recovery cancels it. |
| `broker_ambiguous` | Alpaca answered 5xx / timeout. The order was looked up before anything else; no second entry is ever created. |
| exit 5 / `disk_low` | Disk nearly full. Free space under `/home/opc/orb_iex_data`. |
| exit 6 | Another instance holds the lock. Use `orbctl status`; do not start a second copy. |
| `.env has permissions` | Run `chmod 600 /home/opc/orb_iex_data/.env`, then `orbctl start`. |
| `SKIP ... crossed_before_submit` | The price was already through the ORB trigger when the entry was about to be sent. Normal: the trade is skipped on purpose (no market order, no retry). |
| `unprotected_position` / `stuck_pending_order` | Protection is unconfirmed; entries are blocked and recovery escalates toward flat. Check broker truth and unresolved intents. Quantity changes use cancel-confirm-new. |
| `protective_stop_crossed` | The position was already through its protective stop; the bot flattens it. Check the next summary. |
| `duplicate_stop` | A second live stop for one symbol was found and canceled. Should not happen; report it. |
| `postclose_exit` | A position was still held after the close; the bot is closing it with an extended-hours limit order. Check `orbctl logs` until `zero overnight positions confirmed`. |
| `overnight_unflattened` (CRITICAL) | It could not be closed before 20:00 ET. New entries stay blocked until flat; close it in the Alpaca dashboard if you can. |
| `entry_skip ... skip_spread_too_wide` | The IEX spread was wider than `trading.max_quote_spread_pct` (default 1% of the mid). Nothing was sent; the signal is skipped for the day. `entry_quote` has bid, ask, spread, age, feed. |
| `ENTRY CUTOFF ...: cancelling N unfilled entries` | Normal at the cutoff time (`schedule.entry_cutoff_time`, default 10:30 ET, an operational default). The listed signals are dropped for the day. |
| `cutoff_race_fill` | An entry filled while its cutoff cancel was pending. It was given a protective stop and is flattened at the end of the day. |
| `cutoff_cancel_unconfirmed` | Alpaca still shows the entry live 15 s after the cutoff cancel. The bot keeps cancelling; check the Alpaca dashboard. |
| `OVERNIGHT POSITIONS` | Something was still open at the close. The next 09:31 sweep closes it. |

## Account

The bot touches only its own orders (`orbiex-...` ids) and its own shares
(from its own fills). Your other orders and positions in the same paper
account are left alone. They are listed as `unrelated_positions` in the log.
The bot does not enter a symbol in which you already hold shares or have an
open order. If you sell some of the bot's shares by hand, the bot counts
only what is still there.

## October 3 audit: before another paper session

The audited configured account still held CTVA -72 and SPHD -409 in the read-only snapshot; do not treat local `entry_open` or a green test suite as proof of broker flatness. Re-read positions and open orders, then perform supervised owned-position recovery. The existing empty account allowlist still blocks paper startup; confirm the intended account and distinct machine identity before enabling it.

An incomplete Ctrl+C/SIGTERM or normal-return cleanup now exits through the fatal recovery path (exit 7), writes `health/shutdown.json` when storage permits, and must not be described as clean. Inspect both shutdown and breaker state. Complete cleanup can preserve a confirmed working protective stop; EOD requires flatness.

The original audit identified CTVA's October 1 distribution contaminating raw ATR. The follow-up now excludes corporate-action windows and incomplete history; see the policy below. Unattended entries remain unapproved. Read [the comprehensive report](OVERNIGHT-REPORT-2026-10-03.md), [incident evidence](INCIDENT-FORENSICS-2026-10-03.md), and [research protocol](OVERNIGHT-RESEARCH-PROTOCOL.md).

## Supervised PAPER acceptance — execution hardening branch

**Current gate: BLOCKED.** The verified PAPER account is now allowlisted as `99b682d697a7b5b8` and owned by `orb-mac-supervised`; the process must export `ORB_IEX_INSTANCE_ID=orb-mac-supervised`. The account still has CTVA −72 and SPHD −409, zero open orders, and a legacy journal without breaker/intent/risk tables. No broker recovery has been executed. The entry kill switch is active at `runtime/KILL`. The next reported session is October 5, 09:30–16:00 America/New_York. Verify the current broker clock again before acting. Never use the live host or change keys to bypass a failed guard.

### 1. Identify the owner and capture evidence (read-only)

Stop any other machine's bot/service for this account first. One host owns this account; the local lock cannot detect another machine. From the existing checkout:

```sh
cd ~/orb-iex
git branch --show-current
git rev-parse HEAD
cd orb_iex
.venv/bin/python -m orb_iex account-fingerprint
.venv/bin/python -m orb_iex paper-state
```

Expected branch: `codex/overnight-execution-research`. Compare the fingerprint to the intended PAPER account in Alpaca. `paper-state` performs GETs and opens SQLite read-only; it never migrates, arms a broker, or sends orders. Exit 1 means the flat gate is blocked; `null` breakers/intents mean unavailable, not clear. Broker positions are account-wide; `journal_owned_qty` is only local fill evidence and may be stale. Never infer ownership from symbol alone. With the current legacy journal, EFX +96 locally disagrees with broker EFX flat, and 11 local nonterminal orders are stale; startup must reconcile them.

Only after confirming the account and exclusive host, edit the existing `[safety]` section (do not duplicate it). Set `allowed_account_fingerprints` to the verified fingerprint and `account_owner_instance_id = "orb-mac-supervised"`. This is a deliberate operator ownership declaration, not a credential change. If another machine is intended to own it, use that machine and its own unique declaration instead.

```sh
nano config/settings.toml
export ORB_IEX_INSTANCE_ID=orb-mac-supervised
.venv/bin/python -m orb_iex check-config
.venv/bin/python -m orb_iex check-alpaca
```

Verify PAPER host `paper-api.alpaca.markets`, matching fingerprint/owner, and no other active bot. `check-config` alone does not prove broker access or ownership. Leave the existing account guards enabled.

### 2. Recover the legacy positions with entries blocked

Do this while supervising the regular session, preferably before the 09:31 ET open sweep. Preserve the journal; never delete rows or reset ownership. With all bot processes stopped, take a SQLite backup, activate the entry-only kill switch, then use the existing tested recovery path:

```sh
backup_dir=$(mktemp -d runtime/pre-recovery.XXXXXX)
sqlite3 'file:runtime/journal.sqlite?mode=ro' ".backup '$backup_dir/journal.sqlite'"
touch runtime/KILL
.venv/bin/python -m orb_iex paper
```

Startup migrates the journal, reconciles broker orders/positions, and retrieves known nonterminal entry COIDs even when Friday is outside Monday's REST history window. Owned carried fills set an overnight breaker and flatten through the normal recovery/open-sweep state machine. KILL blocks new entries but permits protective/exit actions. The CTVA/SPHD missing-fill weekend tests exercise this exact class and preserve foreign inventory. Do not substitute manual market orders or clear breakers to force progress.

In another terminal, repeat the `cd .../orb_iex` and matching `export` above, then:

```sh
.venv/bin/python -m orb_iex paper-state
tail -f runtime/logs/orb_iex-$(date +%Y%m%d).jsonl
```

Require broker CTVA/SPHD flat, no owned open entry/stop/exit, reconciled local quantities, no unresolved intents, and explained/cleared breakers. `flat_gate: true` is deliberately stricter: all account inventory and orders must be empty as well as local nonterminal state. If manual inventory remains, this command stays blocked; preserve it and request a documented ownership review, never flatten it to make the gate green. If recovery stalls, keep KILL, retain logs/journal, and stop acceptance. If flat is verified, Ctrl+C the recovery process and inspect `runtime/health/shutdown.json`; incomplete shutdown must report failure (normally exit 7), not success. Re-run `paper-state` after the process has stopped.

### 3. Run a separately approved, attended session

Recovery alone is not acceptance. Before a fresh session, an operator must approve the run, resolve every unexpected breaker/intent using broker truth, and confirm no competing host. Retain the recovered journal. Do not remove KILL until those conditions hold.

```sh
.venv/bin/python -m orb_iex paper-state
rm runtime/KILL
.venv/bin/python -m orb_iex paper
```

Observe the whole session, using `paper-state` for snapshots (exit 1 is expected while positions are managed), JSON logs, and Alpaca's order view. Record order IDs, timestamps, and pass/fail evidence for each row. Missing market events are **not observed**, not passed; deterministic tests cover forced failure cases without engineering real broker failures.

| Stage | Required evidence |
|---|---|
| Startup | Correct PAPER host/fingerprint/owner; startup reconciliation completes before entries; no unexplained breaker or unresolved intent. |
| 09:35 scan | `corporate_action_snapshot` and `history_exclusions` diagnostics saved; only complete exact-session histories reach ADV(14) ≥1M, ATR(14) >0.50 and same-window RVOL ≥1. No candidate after scan deadline. Unavailable metadata excludes symbols. Verify a nonempty eligible sample or record candidate generation as not observed. |
| Entries/protection | `entry_quote` records usable ≤2s quotes and spread ≤1%; deterministic `orbiex-YYYYMMDD-SYMBOL-entry`; partial fill cancels remainder to terminal before opposite stop/exit; exactly one stable `new`/`partially_filled` stop, remaining stop quantity equals signed broker exposure. `accepted`/`pending_new` is unconfirmed. No PATCH in forbidden states. Protection failure blocks entries and invokes owned recovery; verify no flip or duplicate exit. |
| 10:30 ET cutoff | Remaining entries cancel; fill-beats-cancel is reconciled/protected; broker confirms zero live owned entries. A local `entry_open` label alone proves nothing. |
| Shutdown / EOD | Test Ctrl+C or SIGTERM in a separately supervised run; entries block immediately, cancels reconcile, positions have confirmed protection or explicit flatten. Incomplete shutdown persists a breaker and fails truthfully. Full-session EOD cancels entries, reconciles, cancels conflicting stops, flattens owned exposure, then proves flat and no orphan orders. Shutdown trial does not replace the full EOD session. |

Read the diagnostic audit without changing SQLite:

```sh
sqlite3 -readonly runtime/journal.sqlite "SELECT day,kind,body FROM diagnostics WHERE kind IN ('history_exclusions','corporate_action_snapshot') ORDER BY day DESC;"
.venv/bin/python -m orb_iex review --date 2026-10-05
.venv/bin/python -m orb_iex paper-state
cat runtime/health/shutdown.json
```

The shutdown file is meaningful only for its recorded run/time. End-of-session acceptance requires a fresh successful flat snapshot after the bot stops, all checklist observations, linked logs, exact Git SHA, and an operator's written approval. Missing partial-fill/failure observations remain explicitly backed by offline regression results, not claimed broker observations. **Unattended PAPER and real money remain unapproved. No automatic merge.**

### Feature-history safety policy

Production requires the exact preceding 15 exchange sessions of daily bars (14 true ranges), and exact preceding 14 session-open 09:30–09:35 observations for RVOL. Holidays follow the exchange calendar. Missing or mismatched observations exclude the symbol; older bars cannot fill gaps. Today's opening bar must match its session. The legacy pure indicator helper remains compatible for prevalidated callers; Runner enforces production eligibility before final features/sizing.

At decision time, fetch all pages of Alpaca corporate actions with `data_quality=all`. The API date filter is **process_date**, not ex-date, so the adapter queries the available date domain and evaluates effective dates locally. Any action effective from the oldest required daily session through today quarantines the symbol until a full clean window exists. This deliberately includes cash distributions and already-adjusted split fixtures; raw production bars are never mixed with guessed adjustment factors. Future-effective actions are ignored. Missing/incomplete/malformed/ambiguous metadata fails closed. A ≥2× or ≤½ opening/prior-close ratio independently excludes an unexplained price-basis discontinuity, without labeling it a corporate action. Ordinary smaller economic gaps retain full true range; extreme economic gaps may also be conservatively excluded. Numerical strategy thresholds are unchanged.

Snapshots are observed before the decision and journaled; fetching today's metadata cannot authorize a past/future replay session. Historical live-data replay now excludes features unless an original decision-time snapshot is supplied through a separately validated adapter; no such replay loader is claimed here. Provider publication delays still exist: a successful empty response proves only what Alpaca returned, not omniscient action completeness. Full-universe latency is unverified; a fetch exceeding the scan deadline skips trading for the day. [Alpaca endpoint documentation](https://docs.alpaca.markets/us/reference/corporateactions-1).

## Host sleep, disconnects and stale locks (2026-10-09)

Oct 8 root cause: a plain `paper` process on a laptop was suspended by macOS (about 09:24-09:57 ET), so the
09:35 scan window passed while the process was frozen. The process now keeps the Mac awake itself
(`caffeinate -i -s -w <pid>`, log event `keep_awake`; closing the lid on battery still sleeps the Mac).
A suspend is logged as `host_suspended` and `scan_missed` names it instead of "not running".
Near the scan a transient broker failure retries every 2 s, and the scan waits (until its grace deadline)
for a transient breaker to clear on fresh checks; manual breakers and KILL still block it for good.
A lock file left by a dead process is recovered (`stale_lock` log); a live holder still blocks and is named.

## Daily improvement pipeline (macOS launchd `com.orbiex.daily-review`)

What it does: after each NYSE close it runs `deploy/macos/daily-review.sh`, which asks a headless `claude -p`
session to follow `docs/DAILY-REVIEW.md` + `docs/DAILY-RESEARCH.md` (loss review, broker reconciliation, data audit,
one preregistered research hypothesis, report). It never touches the running bot, never merges, and never changes
strategy settings. Fixes land on `daily/*` branches; research on `research/daily-*` worktrees.

Schedule: weekdays 13:40 Pacific (16:40 ET) and on login/reboot. The script asks `orb_iex daily-gate` what is due:
holidays and weekends are skipped, early closes (13:00 ET) are honoured, a missed day is caught up (3 days back),
a day with a report is never redone, a day is given up after 3 failed attempts. A lock (`runtime/daily-review.lock`)
stops overlapping runs; a dead lock is replaced. Each day gets 2 tries (120 s apart) and a 90-minute limit.

Install / update / remove (touches only this job, never `com.orbiex.paper`):
```
deploy/macos/install-daily-review.sh            # idempotent
deploy/macos/install-daily-review.sh --uninstall
```
Inspect:
```
launchctl print gui/$(id -u)/com.orbiex.daily-review | grep -E "state|runs|last exit"
.venv/bin/python -m orb_iex daily-gate          # days still owed a review (empty = up to date)
ORB_DAILY_DRY_RUN=1 deploy/macos/daily-review.sh  # what would run; calls nothing
.venv/bin/python -m orb_iex research-ledger     # validates the ledger, prints the ranked backlog
tail runtime/logs/daily-review-launchd.log runtime/logs/daily-review-<day>.log
cat runtime/reports/daily-review-<day>.md       # the digest
```
Dashboard: Research tab, "Improvement pipeline". Re-run a failed day: delete `runtime/reports/daily-review-<day>.attempts`.

Limits: unattended model runs need the `claude` CLI logged in. The script prefers `~/.local/bin/claude`, falls back
to the npm one, and sends a notification and exits 1 if neither starts. Promotion of any strategy change requires the
owner's written approval in `docs/DIVERGENCES.md`; the pipeline only reports candidates.
# Canonical SHADOW operation — October 28, 2026

The canonical checkout is `~/orb-iex`, on
`codex/overnight-execution-research`. The reviewed RC and Candidate B source
changes were integrated locally; the isolated research worktrees are historical
references. This section supersedes earlier activation instructions for the
current runtime. **PAPER READY is false.**

1. `com.orbiex.paper` remains the single PAPER supervisor. `runtime/KILL` is an
   **entry-only** pause, not a shutdown: leave it present. The account allowlist,
   owner `orb-mac-supervised`, risk caps, stops, and manual breakers are unchanged.
   Broker reconciliation/protective exits remain armed on the paper host.
2. `com.orbiex.shadow` runs `.venv/bin/python -m orb_iex.shadow_session` from this
   checkout, using frozen `config/candidate_b.toml`, the existing Runner/Engine
   and a quote-driven SimBroker adapter. The completed paired collector now uses
   `runtime/candidate_b/paired-v1/` for independent A/B state and write-ahead
   observations. Earlier `shadow.sqlite` and `trade_journal_shadow.sqlite` are
   preserved as legacy evidence; new hypothetical fills live in the two state
   ledgers. See [SHADOW-RUNBOOK.md](SHADOW-RUNBOOK.md). No broker order
   endpoint is called by the collector. launchd restarts it after failure.
3. `com.orbiex.journal` serves the existing read-only journal at
   `http://127.0.0.1:8788/`. Port 8787 belongs to another local application and
   was left alone. The Bot page shows the PAPER pause, SHADOW heartbeat and last
   deterministic review. Research → A versus B shows paired decisions and exact
   exclusions. Existing P&L remains real recorded PAPER fills; hypothetical
   SHADOW fills remain in their separate journal. `/api/operations` reads both
   sources, never contacts the broker and cannot mutate state.
4. `com.orbiex.daily-review` now invokes
   `.venv/bin/python -m orb_iex.operational_review --pending`, with the configured
   owner environment. It runs at the existing 13:40 Pacific schedule, login and
   every 15 minutes to catch missed sessions. An exclusive OS lock prevents
   overlapping reviews. It does no network work when nothing is due and makes
   **no LLM calls**. The old `daily-review.sh` is retained as a legacy/manual
   workflow, no longer the scheduled command. Original reports are preserved;
   deterministic JSON/Markdown results are under `runtime/reports/deterministic/`.
5. Verify locally with the commands below. Inspect failures; never delete a
   breaker, pause file, database or lock to make a green status appear.

```sh
ORB_IEX_INSTANCE_ID=orb-mac-supervised .venv/bin/python -m orb_iex paper-state
.venv/bin/python -m orb_iex --config config/candidate_b.toml check-config
ORB_IEX_INSTANCE_ID=orb-mac-supervised .venv/bin/python -m orb_iex.operational_review --pending
launchctl list | rg 'com\.orbiex\.'
curl -fsS http://127.0.0.1:8788/api/operations
```

`paper-state` is GET-only. Its nonzero exit remains expected while manual
breakers are present; a flat broker account alone is not an activation gate.
The protected environment file is loaded by the existing secret loader. Never
print/copy keys into commands or reports. `check-config` is credential-free;
an explicitly supplied `--require-private-env` still validates file permissions.

## Forward evidence and restart semantics

The collector never feeds completed minute-bar highs/lows into the forward fill
path. It records observed IEX quotes before applying them. A hypothetical stop
or market order can fill only from a fresh, non-crossed, spread-limited quote
dated at/after submission; buy fills use ask, sell fills use bid. Repeated or
out-of-order quote timestamps cannot fill twice. Missing, future, stale or
invalid quotes produce no simulated fills. The simulator never invents a quote,
timestamp, alternate fill or a 100-share lot multiplier. This remains a
best-quote simulation, not consolidated executable/queue/depth/latency proof.

The paired collector commits each simulator checkpoint and engine journal
atomically, restoring orders, positions, cash, partial fills and quote watermarks
after restart. Pending ticks reuse recorded inputs; a committed portfolio is
not applied twice. The legacy bar/replay runner's `shadow_abandoned` semantics
are not used by this forward collector. Corrupt/mismatched state fails closed.
The real PAPER ledger is never replaced with a simulator or snapshot database.

Forward A/B decisions use shared recorded inputs and independent portfolios.
Only actual hypothetical closed counterparts are matched; missing counterparts,
executable counterfactual returns, filtered-winner execution and regimes stay unknown.
Review output distinguishes recorded PAPER fills from hypothetical SHADOW fills,
normal/doubled **additional modeled** costs and unresolved process attribution.
Same-symbol/day trips retain separate detail links/fill rows; ambiguous shared
plan/process context is marked unknown. Drawdown is computed over closed trades
in exit order, including intraday losses, and excludes unrealized excursions.
Session charts retain entry-session attribution for carried historical trades;
they are not an intraday broker equity curve or daily account statement.

## Activation blockers and recovery boundary

The preserved manual breakers are `auto_foreign_inventory`, `auto_clock_skew`,
`auto_broker_failure`, `auto_control_failure`, `auto_market_closed`, and
`auto_calendar_failure`. `auto_external_kill` additionally reflects the deliberate
entry pause. Do not clear any of them automatically. The clock/session boundary
repairs are regression-tested but do not erase historical incident evidence.

Candidate B additionally needs point-in-time universe/actions/borrow coverage,
adequate executable quote/trade/halt/latency and cost evidence, matched
chronological A/B economic results, a predeclared absolute portfolio drawdown
limit, at least **60 new sessions AND 200 trades** strictly after the final
operational freeze and October 28, and the existing one-evaluation/repeated-test
controls. IEX best quotes cannot certify full-market depth or a later stop fill.
The prior historical periods and small TradingView sample remain exposed.

Broker partial-fill/protection/EOD/recovery acceptance still requires the
separately approved, supervised PAPER lifecycle described earlier in this
runbook. No lifecycle order was submitted during this integration. Removing KILL,
clearing breakers, promoting B, increasing risk or enabling new PAPER entries
requires the existing evidence and explicit approval. Real-money trading remains
unsupported.

The USD 1,000 absolute equity drawdown proposal is documented but inactive;
see SHADOW-COMPLETION-CHANGE-REQUEST.md. Current risk settings were not changed.
Detailed preserved incident evidence is in BREAKER-DIAGNOSES-2026-10-28.md.
