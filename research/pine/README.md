# ORB Pine v6 research projections

Open `baseline_a.pine` or `candidate_b.pine` in TradingView Pine Editor, use
regular-session US-stock candles at 1 or 5 minutes, then Add/Update on chart.
Both were compiled and executed in native TradingView on AAPL, 5 minutes.
Results and visible dates are in `native-evidence.json`.

Defaults: $100,000, margin 25% (4x), one position, no pyramiding,
$0.0035/share/side commission, one tick/side slippage, bar magnifier off,
no tick/fill recalculation, no processing entries on the signal bar's close.
For 2x costs change only declaration `commission_value=0.007, slippage=2`.
One tick equals $0.01 only for a symbol with that tick size.

Only completed opening bars enter signals. RVOL uses the previous 14 opening
volumes; today's observation is appended afterward. Daily SMA true-range ATR
and ADV use `[1]` with `lookahead_on`, the confirmed-previous-day pattern.
No unshifted future daily OHLC enters a decision. First-bar presence and the
expected 1/5 opening-bar count are required. Missing observations are skipped.

These are **single-chart projections**, not the portfolio system. There is no
dynamic top-20 universe, IEX history, bid/ask, historical borrow/depth/halts,
partial-fill/cancel-race or broker-protection simulator. A uses the bar close
instead of a quote clearance check. The emulator's protective exit differs from
cancel-confirm-new; EOD closes on the final regular bar, rather than 60 seconds
before the calendar close. Early-close final bars follow native session flags.
The study cannot establish a fully equivalent production replay.

B enforces its volume cap in source but **blocks all entries** because native
historical candles cannot prove its quote/depth gates. Its zero trades are
missing-evidence behavior, not a profitable B backtest. Its full executable
implementation and paired journal collection are in Python signal/shadow mode.

The AAPL native sample is exposed, not a holdout. Native chart feed and Python
IEX/SIP data differ, so no Python parity or portfolio-performance claim is made.
On October 28, 2026, the TradingView account is Basic. Baseline A is saved as
`ORB-IEX baseline_a chart projection`, version 1.0. Candidate B remains compiled
on layout `zOWgj5EF` and in the unsaved editor; attempting Save shows the
one-saved-script upgrade restriction. Keep both local files; do not overwrite A
to save B. Both scripts execute on AAPL and MSFT 5-minute regular-session charts.
The exposed research window ends at October 29, 2026 00:00 UTC; unchanged scripts
do not generate subsequent entries. This is historical research, not a forward
TradingView signal service.

## Separate forward configuration v1

`baseline_a_forward_v1.pine` and `candidate_b_forward_v1.pine` preserve the frozen
rules and costs; the original files and `native-evidence.json` remain unchanged.
Default start: October 29, 2026 00:00 UTC. **Use forward end date** defaults off;
when enabled, the end is exclusive and must be after the start. The far-future
end input is ignored while that switch is off. Settings display date inputs in
the chart timezone; the opening-range clock remains `America/New_York` (DST-aware).
Forward copies reject non-standard charts and non-stock/non-New-York listings.
Confirmed-bar decisions, shifted previous-day ATR/ADV, regular-session flags,
the opening-bar completeness check and Candidate B's evidence block are unchanged.

Compile a forward copy as an **unsaved** script on a disposable chart tab. Do not
save that tab over the existing layout or overwrite the saved baseline. Basic's
one-script library and indicator-count limits still apply; replace a chart
instance in the disposable tab if needed. Historical prices before the forward
start warm up indicators but cannot authorize entries. Changing the start to an
earlier date is an exposed diagnostic, never untouched forward evidence. A
future completed session cannot be verified before it occurs.

The journal's [A versus B view](http://127.0.0.1:8788/#/research?tab=paired)
links to this layout and to stock charts from PAPER trade details, SHADOW trades,
paired signals and rejected entries. Only the symbol and five-minute interval
are sent in chart URLs; fills, P&L and the localhost referrer are not sent.
TradingView chart performance is independent and is never imported into the
journal. Alpaca SHADOW collects its own evidence automatically; refresh the
journal to read new observations. Its simulated portfolios and recorded PAPER
history remain separate.

Basic has three price alerts but no technical/strategy alerts or webhooks.
The account's Webhook URL option displays the Basic-to-Essential upgrade prompt.
No alerts, receivers, tunnels, broker routing, subscriptions or publication
were created. Do not expose localhost for TradingView. Future eligible webhook
work needs a secure authenticated endpoint and validated, deduplicated,
timestamped notification-only events; it must never route broker orders.

Verification artifacts are ignored under `runtime/tradingview-workflow/`:
five online SQLite backups, before/after integrity and protected-history checks,
actual SHADOW and journal SIGKILL/launchd recovery, GET-only broker ownership/flat
state, HTTP checks and test logs. The journal remains loopback-only and read-only;
At the October 28 verification, PAPER safety was failed closed with all seven
breakers and KILL.
Browser checks cover 375px and 1280px, chart-link navigation, actual trade detail,
empty A/B portfolios and source labels with no journal console errors. The new
link escaping/render checks run with `node --test tests/dashboard_chart_links.test.cjs`.
Full offline pytest: 845 passed; focused dashboard/paired recovery tests: 60
passed; JavaScript checks: 2 passed. JavaScript syntax and Git whitespace checks
passed. Full pytest used the existing research-capable environment with this
checkout's `src` first on `PYTHONPATH`; no dependencies were installed.

October 29 closeout: both forward copies compiled on AAPL; A also ran on MSFT.
Native inputs show the end switch off; an enabled end before the start raised
the expected runtime error, then defaults were restored. B's MSFT forward recheck
hit Basic's indicator-count limit; historical B/MSFT evidence remains intact.
See `forward-v1-evidence.json`. No scripts or layouts were saved over originals.

The live journal's exhausted-file-handle defect was fixed by retaining its
thread-safe SQLite source adapters. Diagnostic reads now select only the evidence
used, concurrent trade rebuilds share a lock, and SQLite WAL changes invalidate
the trade cache. Twenty live health polls kept SQLite descriptors at 42/42;
health, overview and operations returned HTTP 200 in under one second. Current
checks: 100 journal/Pine regression tests, 82 operational tests and 2 JavaScript
checks passed; the earlier 845-test full-suite result was not rerun. KILL remains
latched and entries disabled. The current market session is still in progress;
its completion and after-close review have not been verified yet.
