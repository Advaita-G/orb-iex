# SAFETY: why ORB-IEX cannot trade real money

The guards are layered. Any one of them alone blocks live trading.

1. **Hard-coded paper host.** `constants.PAPER_TRADING_HOST =
   "paper-api.alpaca.markets"`. No setting, environment variable or flag
   changes it. Unknown settings keys make the bot refuse to start.
2. **Live endpoint refused.** `safety.assert_allowed_url` refuses
   `api.alpaca.markets` and every host not on the allowlist (paper trading +
   market data). Non-HTTPS URLs, odd ports and embedded credentials are
   refused too. The stream URL must be `wss://paper-api.alpaca.markets/stream`.
3. **APCA_API_BASE_URL check.** If this variable is set to anything but the
   paper host, the bot stops before any network call.
4. **Paper account verified at startup (fail closed).** `/v2/account` must
   answer on the paper host (live keys do not authenticate there). The
   account number must start with `PA`, and the status must be ACTIVE. If
   anything is unclear, the bot does not start. Paper mode also refuses a
   blocked account and a clock skew above 2 s.
5. **Single write choke point.** Only `broker.AlpacaPaperBroker._write` sends
   POST/PATCH/DELETE. A test fails if any other module contains those verbs.
   Every write rebuilds the URL from the paper constant and re-checks it.
6. **Arming.** The broker refuses every write until `arm()` succeeds. That
   needs the `paper` command, `trading.enable_paper_orders = true` and a
   verified paper account. `signal` and `shadow` never create this broker.
7. **Only ORB-IEX order ids.** The broker refuses any order whose
   client_order_id is not `orbiex-YYYYMMDD-SYMBOL-role`.
8. **No redirects** are followed. A redirect on any request is a safety stop.
9. **Kill switch.** While the file `runtime/KILL` exists, no new entry is
   placed. The engine checks it, and the broker checks it again. Protective
   stops and exits still work.
10. **Only its own orders and positions.** Cancels, the EOD exit, the sweeps
    and flattens act only on `orbiex-...` orders and on shares that came from
    ORB-IEX fills (journal), capped by what Alpaca holds. Unrelated orders
    and positions in the account are never canceled or closed (tests).
11. **No live switch exists.** The settings schema has no key that could
    enable live trading. `runtime.mode = "live"` is rejected.

12. **Unattended guards (Oracle VM).** The systemd unit holds no keys; the
    bot reads them only from `/home/opc/orb_iex_data/.env` and refuses to
    start unless it is mode 600 (`--require-private-env`). One instance per
    data directory (flock). A safety stop or config error is not restarted
    by systemd. Low disk blocks new entries only. Setup scripts and the
    health check use GET requests only and never place test orders.

13. **Crossed orders (execution layer).** A crossed ORB entry is skipped, never
    sent as a market order and never retried. A crossed protective stop is never
    skipped: flattening is requested after entry/stop cancellation is confirmed.
    Stop quantity changes use cancel-confirm-new; no engine PATCH remains.
    Only correctly sized `new` stops count as protected. Lost replies are
    reconciled before retry; failed lookups never authorize a blind resend.

14. **Flat at the close (execution layer).** A per-step guard makes sure every ORB-IEX position has a live
    protective stop or a working exit; EOD cancels and exits are retried until done; after the close the
    bot stays on any leftover (extended-hours marketable limit) and the day is not closed out until Alpaca
    shows ORB-IEX flat. If that fails the bot logs CRITICAL and blocks new entries until it is flat.

15. **No late entries.** Unfilled ORB entries are cancelled at `schedule.entry_cutoff_time` (default 10:30 ET, an operational default, not a researched cutoff) and
    confirmed terminal; a cancelled signal is never re-entered that day (also after a restart); a fill during the
    cancel race is protected at once. Once the EOD window begins no entry can be submitted or stay working
    (`_submit` refuses it), and EOD cancels entries before it flattens.

## Keys

- Keys are read only from `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`, from the
  environment or `.env`. Git ignores `.env`. The bot warns when `.env` is
  readable by other users.
- The `Credentials` repr is masked. A log filter replaces any key value with
  `***` in both the console and the JSON logs. Tests check both.

## Data plan

- Only free feeds: `iex`, plus `sip` data older than 16 minutes. The data
  layer refuses a SIP request that ends less than
  `history_min_age_minutes` ago before any network call.

## Verified on 2026-09-25

- 152 offline tests pass.
- `check-alpaca`, the signal replay, the shadow replay, and live
  signal/shadow/paper starts all ran against the real paper account.
- Alpaca shows 0 orders and 0 positions afterwards.
- The key values appear in no file under `runtime/`, `src/`, `tests/`,
  `config/` or `docs/`.
- No live host appears in the source except as the refusal constant (test).

## Fatal errors (P0-A)
An unexpected exception in the run loop: CRITICAL log with traceback, new entries blocked, one broker reconciliation
(before mutations), every ORB-IEX position either confirmed protected (a `new` stop for exactly the shares held, or an exit
covering all of them) or sent to the flatten path, state persisted (`fatal_state` in the journal, `health/fatal.json`,
trading journal, heartbeat), exit 7, systemd restarts after 5 s and recovery runs. Ctrl+C / SIGTERM are clean stops.
Unfilled ORB-IEX entry orders are cancelled and the cancel is confirmed in the reconciliation (only `orbiex-` orders of
all dates; unrelated orders are never touched); a fill that wins the cancel race is protected or flattened.
Known limits: a cancel Alpaca has not confirmed within three rounds is reported as `cancel_unconfirmed` and left to
recovery (cutoff/EOD cancel it); a crash loop restarts every 5 s with no back-off; if Alpaca is unreachable the handler
reports "positions unknown" after one failed read and relies on recovery.

## Ambiguous broker replies (P0-A)
5xx, 408, timeout and connection reset are never rejections. ENTRY: look up by client_order_id; found = adopted,
otherwise unconfirmed and never resent (fail closed). STOP/EXIT: adopt if found; a definite absence may permit
the same-ID retry. A failed lookup never permits a blind resend. CANCEL: a retry requires a confirmed live read.
REPLACE: adopted if the new id exists, resent only when the old order is confirmed unchanged.

Execution details, guarantees and the current operating decision are in
[the October 2 handoff](HARDENING-2026-10-02.md). Broker outages, stuck cancels
and dual storage failure can defeat strict timing/durability guarantees;
persistent breakers block new entries when recovery is unresolved.

## Cross-machine: one paper account per machine (decided 2026-10-01)
Operating rule:
- The Mac bot has its OWN Alpaca paper account, its own API keys and its own account fingerprint.
- The VM bot has a DIFFERENT paper account, different keys and a different fingerprint.
- Each machine's settings file allowlists only its own account: `config/settings.toml` (Mac) holds the Mac
  account's fingerprint, `config/settings.oracle.toml` (VM) holds the VM account's. A config that names more
  than one account is refused at load time, and a fingerprint must never appear in both files.
- With distinct configured ownership, each machine refuses the other account. This is not distributed exclusion against duplicated keys/configuration. Never copy `.env` keys or the allowlist line between machines.

How it is enforced (before the broker is armed; any mismatch = exit 2 and no restart loop):
1. `safety.allowed_account_fingerprints` (at most one entry): the account behind the keys must match it. The
   fingerprint is 16 hex chars of sha256(account id + account number); print it with
   `python -m orb_iex account-fingerprint` (read-only). Empty list = paper mode refuses to start.
2. `safety.account_owner_instance_id` must equal this machine's `ORB_IEX_INSTANCE_ID` (use a different value on each machine).
3. The local file lock (`orb_iex.lock`) stops two processes on ONE machine.
Wrong keys on the right machine, the right keys on the wrong machine and a copied config all fail at startup.

Not implemented, on purpose: a lease or lock stored in the Alpaca account (for example a watchlist). The one-account-
per-machine rule replaces it. If someone declares the same account and the same instance id on two machines by hand,
nothing in the software can see it: that is an operating error, not something the bot can detect.

## October 3 clarification

Ctrl+C/SIGTERM are clean only when terminal-entry and position-management checks succeed; otherwise the runner raises a fatal recovery error. A normal paper-mode loop return now runs the same reconciliation. Same-host processes under the same OS user additionally share an account-fingerprint flock, including when their runtime directories differ.

A protection deadline initiates cancellation/escalation and blocks entries; it cannot force a broker to accept an order or fill during an outage/halt. An unresolved cancel does not authorize a competing exit. Different OS users/hosts and manual same-symbol trades remain outside the local lock's guarantee. Full acceptance limits: [October 3 report](OVERNIGHT-REPORT-2026-10-03.md).
