# SPEC: the strategy behaviour to reproduce

Source of truth: the strategy's QuantConnect `Main.cs` (not included in this repo). This file explains it.

## Universe
- Rebuilt on the first trading day of each month.
- Take all stocks with price > $5, exclude SPY, sort by dollar volume
  (descending), take the top `universe_size` (default 200).
- In ORB-IEX: built from Alpaca's active tradable US equity list plus the
  previous session's SIP daily bars (free, older than 15 min). Dollar volume =
  close × volume. Open question: QC's `Fundamental` may include/exclude ETFs,
  ADRs, preferreds. Settle by diffing against a QC universe export.
- Note: the field default `_universeSize = 1000` is dead code; `ReadInt`
  defaults to 200.

## Indicators (per symbol, daily, raw/unadjusted prices)
- **ATR(14)**: LEAN `AverageTrueRange` with `MovingAverageType.Simple`
  (simple mean of true range, NOT Wilder smoothing). Daily bars.
- **Average daily volume**: simple mean of the last 14 daily volumes.
- Daily bars = regular session only (09:30–16:00); QC's daily consolidator
  excludes extended hours. Build them from SIP minute bars, or confirm Alpaca
  daily bars match before using them.
- Warm-up quirk (verify in QC): ATR is auto-warmed (created via `ATR()` with
  `AutomaticIndicatorWarmUp`), but the two manual `SimpleMovingAverage`
  objects are not. A symbol added mid-run may be ineligible for ~15 sessions.
  Default: replicate QC. Decide after verifying.

## Opening range and relative volume
- Opening bar = 09:30–09:35 (5 minutes) OHLCV. Only the first bar of the day counts.
- RelVol = today's opening-bar volume ÷ SMA of the **previous** 14 opening-bar
  volumes. Today's volume is added to the SMA **after** the ratio is computed.
- RelVol is undefined (symbol not ready) if the SMA is not full or is 0.
- A day with no opening bar is skipped (not zero-filled) and does not update the SMA.

## Scan (once per day at 09:35)
A symbol is a candidate only if ALL hold:
- in the current universe, has state, price > 0
- ready: opening bar is today's, RelVol defined, ATR ready, ADV ready
- opening-bar **open** > $5
- RelVol >= 1
- ATR > 0.50 (strict)
- ADV >= 1,000,000

Sort by RelVol descending, then ticker ascending. Take the top 20.
**Skipped picks are not backfilled**: if a top-20 name is skipped below, the
21st does not replace it.

## Entry (per candidate)
- Skip if the symbol already has a position or any open order.
- Opening bar close > open → LONG, buy-stop at bar HIGH,
  stop = entry − 0.10 × ATR.
- close < open → SHORT, sell-stop at bar LOW, stop = entry + 0.10 × ATR.
- close == open → no trade.
- Skip if current price is already through the entry level
  (long: price >= entry; short: price <= entry). QC "price" = 09:34 minute close.
- stopDistance = |entry − stop|; skip if <= 0.

## Sizing
```
allocation     = TotalPortfolioValue / 20
riskQty        = floor(allocation * 0.01 / stopDistance)
weight         = min(0.05, maxGrossExposure / 20)
exposureLimit  = min(|CalculateOrderQuantity(symbol, direction*weight)|,
                     floor(TotalPortfolioValue * weight / entryPrice))
qty            = direction * min(riskQty, exposureLimit)
```
- Skip if qty == 0. Shorts: skip if not shortable.
- Replicate LEAN `CalculateOrderQuantity` (free-portfolio buffer ~0.25%, lot
  rounding) in Python; do not substitute Alpaca buying power.
- With defaults the 5% notional cap usually binds.
- Orders are DAY time-in-force; nothing is held overnight.

## Order state machine (port of `SymbolState.HandleOrderEvent`)
- Entry fill (full or partial):
  - if session is closing (after 15:55) or trade already closed → market-exit
    exactly that fill quantity, no stop ("Late fill exit").
  - else keep ONE protective stop sized to the cumulative filled quantity:
    create it on the first fill, resize on later fills.
  - if resizing fails → market-exit that fill ("Stop update failed exit").
- Stop filled → trade closed; cancel any open entry remainder.
- Stop rejected or canceled while the session is open and trade not closed →
  trade closed; cancel entry remainder; market-exit the whole position
  ("Stop failed exit").
- Python: use Alpaca's cumulative `filled_qty` (safe against duplicate events).
  Alpaca `replaced` is NOT a cancel: rebind the stop's new order ID.

## Session events (from that day's calendar close; early closes shift)
- open + 1 min (09:31): sweep — market-close any leftover position.
- 09:35: scan (ORB-IEX: 09:35:03, see PLAN).
- close − 5 min (15:55): session closing = true; cancel all entries and stops;
  liquidate all ("Regular-session exit").
- close − 2 min (15:58): sweep leftovers.
- Sweeps skip a symbol that already has a pending market exit (would flip it).
- "Session closing" is true outside 09:35–15:55.

## Universe removal
Removed symbol: cancel its open entry, drop its state.

## Backtest-only (do not port as trading logic)
Phase/date presets, SetCash(25,000), fee and slippage models (keep as
*modeled* costs in logs), benchmark, 10,000-order threshold. Port
`PrintOrderSummary` as an end-of-day log.
