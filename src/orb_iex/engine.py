"""Order state machine, end-of-day sequence, sweeps and restart recovery.

Port of Main.cs SymbolState.HandleOrderEvent, CloseAndCancelAll and
SweepLeftovers, adapted to Alpaca (DIVERGENCES AL-1..AL-3):
- order updates are snapshots with CUMULATIVE filled_qty, so a duplicate or
  out-of-order update can never double count a fill;
- a stop resize is an Alpaca replace: the old order ends `replaced` (NOT a
  cancel) and the new order has a new id; the trade follows the chain;
- cancels are asynchronous: EOD cancels, waits for terminal states (with a
  timeout), then closes what is left.

Ownership: the engine cancels only ORB-IEX orders (orbiex- ids) and closes
only ORB-IEX shares (bot_positions). Unrelated account holdings are never touched.

The engine is single-threaded. The stream thread only puts updates on a queue.

Execution-layer rules (2026-09-30 fixes):
- An ORB entry whose trigger is already crossed (quote precheck, or Alpaca's 422)
  is SKIPPED, never turned into a market order and never retried. A crossed
  PROTECTIVE stop is never skipped: the position is flattened instead.
- A protective stop is changed only when its broker order is in a replaceable
  state (`new`). Desired stop state (quantity = Alpaca's actual position) is
  queued per symbol and applied once by flush_stops(), so a burst of partial
  fills becomes one replace and there is never more than one live stop.
- A stale REST snapshot can never move an order backwards.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable

from . import constants as C
from .broker import (OrderRejected, OrderUncertain, is_crossed_stop_rejection,
                     is_not_replaceable_rejection)
from .calendar import Schedule
from .journal import Journal, OrderRec, TradeRec
from .execution import MutationGate
from .logging_setup import LOGGER_NAME, log_event
from .orders import (LOST, TERMINAL, OrderRequest, OrderUpdate, client_order_id,
                     parse_client_order_id, role_kind, status_rank)
from .safety import SafetyError, kill_switch_active
from .strategy import EntryPlan
from .candidate_b import liquidity_check

log = logging.getLogger(LOGGER_NAME)
ENTRY_MIN_CLEARANCE = Decimal("0.01")          # Alpaca: a buy stop must be >= base + 0.01 (sell: <= base - 0.01)
REPLACEABLE_STOP_STATUSES = frozenset({"new"}) # never PATCH accepted / pending_new / pending_cancel / pending_replace
MAX_STOP_PLACE_ATTEMPTS = 3                    # unconfirmed (timeout) protective-stop submits before flattening
MAX_STOP_SHRINK_FAILURES = 3
SHRINK_CONFIRM_SECONDS = 3                     # a smaller position must persist this long before the stop shrinks
FILL_LAG_GRACE_SECONDS = 15                    # Alpaca's /positions can trail a fill we already processed
CANCEL_RETRY_SECONDS = 2                       # a cancel that did not finish is sent again
FLATTEN_RETRY_SECONDS = 2                      # spacing of flatten attempts (no attempt cap before the close)
QUOTE_MAX_FUTURE_SECONDS = 2.0                 # a quote stamped later than this ahead of our clock is bad data
QUOTE_BATCH_REFRESH_FRACTION = 0.5             # a batch quote older than this share of the age limit is re-read per symbol
FATAL_CANCEL_ROUNDS = 3                        # cancel-and-confirm rounds for working entries when the process is dying
POSTCLOSE_WAIT_SECONDS = 15                    # let in-flight exits finish before the after-hours flatten
POSTCLOSE_RETRY_SECONDS = 20
POSTCLOSE_BUFFERS = (Decimal("0.01"), Decimal("0.02"), Decimal("0.03"), Decimal("0.05"))  # marketable-limit slack
AFTER_HOURS_END_HOUR_ET = 20                   # Alpaca extended hours end at 20:00 ET
OVERNIGHT_FLAG = "overnight_position"
SHUTDOWN_FLAG = "shutdown"                     # Ctrl+C / SIGTERM: no new entries from this moment on
FATAL_FLAG = "fatal_error"                     # the process is dying on an unexpected exception: no new entries
PROTECTIVE_STOP_STATUSES = frozenset({"new"})   # a stop that is really working
CUTOFF_CONFIRM_SECONDS = 3                     # how often a cutoff cancel is checked against Alpaca's order state
CUTOFF_UNCONFIRMED_ERROR_SECONDS = 15


def _side_sign(side: str) -> int:
    return 1 if side == "buy" else -1


def _px(value: Decimal) -> Decimal:
    """Order prices are whole cents (all ORB-IEX symbols trade above $5)."""
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _parse_ts(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


class Engine:
    def __init__(self, journal: Journal, broker, settings, clock, mode: str):
        self.j = journal
        self.broker = broker
        self.settings = settings
        self.clock = clock
        self.mode = mode
        self.gate = MutationGate(journal, broker, clock)
        self._reconciling = False
        self._shutting_down = False
        self._snapshotting = False
        self._broker_snapshot_valid = False
        self._positions_snapshot: dict[str, Decimal] = {}
        self._owned_snapshot = journal.owned_qty()
        self._fallback_orders = {o.order_id: (o.symbol, o.side, o.filled_qty, o.client_order_id) for o in journal.orders()}
        self.schedule: Schedule | None = None
        self.day: date | None = None
        self.stats = {"partial_fill_events": 0, "stop_qty_updates": 0, "duplicates_ignored": 0,
                      "out_of_order_ignored": 0, "stale_snapshots_ignored": 0,
                      "entry_crossed_skipped": 0, "entry_crossed_422": 0, "entry_unconfirmed": 0,
                      "stop_replace_deferred": 0, "stop_replace_failed": 0, "stop_replace_state_422": 0,
                      "duplicate_stops_canceled": 0, "protective_stop_crossed": 0,
                      "position_guard_actions": 0, "eod_cancel_retries": 0, "postclose_exits": 0,
                      "entry_cutoff_canceled": 0, "entry_window_skips": 0, "cutoff_race_fills": 0,
                      "spread_too_wide_skipped": 0, "stale_quote_skipped": 0, "quote_unavailable_skipped": 0,
                      "candidate_liquidity_skipped": 0,
                      "fatal_entries_canceled": 0}
        # quote_source(symbols, now) -> {symbol: quote with .bid/.ask}; None = no precheck
        self.quote_source: Callable | None = None
        self.last_reject: Exception | None = None        # why the most recent _submit failed
        self._stop_dirty: set[str] = set()               # symbols whose desired stop differs from the broker's
        self._stop_attempts: dict[str, int] = {}         # unconfirmed protective-stop submits
        self._shrink_seen: dict[str, datetime] = {}
        self._shrink_failures: dict[str, int] = {}
        self._seen_ts: dict[str, datetime] = {}          # newest snapshot time applied per order
        self._last_fill_at: dict[str, datetime] = {}     # symbol -> when a fill was last processed
        self._cancel_last: dict[str, datetime] = {}
        self._flatten_last: dict[str, datetime] = {}
        self._cutoff_adopted = False                     # unknown ORB-IEX orders looked for at the cutoff
        self._cutoff_logged = False
        self._cutoff_confirm_last: dict[str, datetime] = {}
        self._cutoff_unconfirmed_logged: set[str] = set()
        self._quote_batch: dict | None = None            # quotes fetched once for a place_entries run
        self._quote_batch_at: datetime | None = None     # when that batch was received
        self._quote_fetch_error: str | None = None       # a quote request failed during this entry run: fail fast
        self._pc_started: datetime | None = None
        self._pc_last: dict[str, datetime] = {}
        self._pc_attempts: dict[str, int] = {}
        self.tlog = None          # optional TradeLog (record only; never read back for decisions)
        self.entries_blocked: str | None = None   # e.g. "disk_low": no new entries; exits/stops continue
        self._stream_fill_events: set[tuple[str, str]] = set()

    def _tl(self, kind: str, symbol: str | None = None, key: str | None = None, **fields) -> None:
        """Append one execution event to the trading journal. Never breaks trading."""
        if self.tlog is None:
            return
        try:
            self.tlog.event(self.day.isoformat() if self.day else "-", kind, symbol, dedupe_key=key, **fields)
        except Exception as exc:  # pragma: no cover - journal problems must not stop the bot
            log_event(log, logging.WARNING, "tradelog_error", f"trading journal write failed: {exc}")

    # ------------------------------------------------------------------ day
    def start_day(self, schedule: Schedule) -> None:
        self.schedule = schedule
        self.day = schedule.session.day
        self.j.day(self.day)

    @property
    def dkey(self) -> str:
        return self.day.isoformat()

    @property
    def session_closing(self) -> bool:
        return self.j.day(self.day)["session_closing"]

    def set_session_closing(self, value: bool) -> None:
        self.j.set_session_closing(self.day, value)

    def market_open(self, now: datetime) -> bool:
        s = self.schedule.session
        guard = getattr(self, 'regular_market_guard', None)
        return s.open <= now < s.close and (guard is None or guard(now))

    def market_state_unknown(self, now: datetime) -> bool:
        """Inside the scheduled session, the broker check is stale or failed: neither open nor closed.

        Only a known-closed market may latch `overnight_position`; an unconfirmed one waits for the
        next fresh check (2026-10-26: a stale check blocked every entry for the rest of the day).
        """
        s = self.schedule.session
        state = getattr(self, 'regular_market_state', None)
        return s.open <= now < s.close and state is not None and state(now) == 'unknown'

    # ------------------------------------------------------------ order helpers
    def _seq(self, symbol: str, role: str) -> int:
        cids = {o.client_order_id for o in self.j.orders(self.dkey, symbol)}
        cids.update(i['client_order_id'] for i in self.j.intents(symbol=symbol))
        return 1 + sum(1 for cid in cids if (parse_client_order_id(cid) or (None, None, None))[:3]
                       == (self.day, symbol, role))


    def _record(self, u: OrderUpdate, role: str, day: str | None = None) -> OrderRec:
        rec = OrderRec(order_id=u.order_id, client_order_id=u.client_order_id, day=day or self.dkey,
                       symbol=u.symbol, role=role, side=u.side, type=u.type, qty=u.qty,
                       stop_price=u.stop_price, status="pending_new", filled_qty=Decimal(0),
                       filled_avg_price=None, replaces=u.replaces, replaced_by=u.replaced_by,
                       created_at=self.clock.now().isoformat())
        self.j.save_order(rec)
        return rec


    def _submit(self, req: OrderRequest, role: str, reason: str) -> OrderRec | None:
        self.last_reject = None
        if role == 'entry' and self.mode == 'paper' and self.settings.research_profile == 'candidate_b':
            self.last_reject = SafetyError('candidate_b is unqualified; paper entries refused')
            return None
        guard = getattr(self, 'entry_guard', None)
        if role == 'entry' and guard is not None and guard():
            self.last_reject = SafetyError('internal entry gate closed')
            return None
        if role == 'entry' and (self._entry_window_closed(self.clock.now()) or self._shutting_down):
            self.last_reject = SafetyError('entry window closed')
            return None
        log_event(log, logging.INFO, "order_intent",
                  f"ORDER {role} {req.side} {req.qty} {req.symbol} {req.type}"
                  f"{'' if req.stop_price is None else ' stop ' + str(req.stop_price)} ({reason}) cid={req.client_order_id}",
                  symbol=req.symbol, role=role, side=req.side, qty=str(req.qty), type=req.type,
                  stop_price=req.stop_price, client_order_id=req.client_order_id, reason=reason)
        try:
            self._broker_snapshot_valid = False
            u = self.gate.run(req.symbol, role, 'submit', req=req)
        except (OrderRejected, SafetyError) as exc:
            self.last_reject = exc
            self._tl('ambiguous' if isinstance(exc, OrderUncertain) else 'reject', req.symbol,
                     role=role, client_order_id=req.client_order_id, qty=req.qty, price=req.stop_price,
                     status='unconfirmed' if isinstance(exc, OrderUncertain) else 'rejected',
                     detail={'error': str(exc), 'status': getattr(exc, 'status', None),
                             'body': getattr(exc, 'body', None), 'reason': reason}, event_time=self.clock.now().isoformat())
            log_event(log, logging.ERROR, "order_rejected", f"{req.client_order_id} rejected: {exc}",
                      symbol=req.symbol, role=role, client_order_id=req.client_order_id, error=str(exc),
                      status=getattr(exc, 'status', None), body=getattr(exc, 'body', None),
                      classification=getattr(exc, 'classification', 'unknown'))
            if isinstance(exc, OrderUncertain):
                self.j.set_breaker('reconciliation_unhealthy', req.symbol, str(exc))
            return None
        existing = self.j.order(u.order_id)
        self._tl('submit', req.symbol, key=f'submit:{req.client_order_id}', role=role, order_type=req.type,
                 side=req.side, client_order_id=req.client_order_id, order_id=u.order_id, qty=req.qty,
                 price=req.stop_price, status=u.status, detail={'reason': reason}, event_time=self.clock.now().isoformat())
        rec = existing or self._record(u, role)
        self._apply(u, side_effects=False)
        self.gate.resolve(self._apply)
        return self.j.order(rec.order_id)


    def _cancel(self, rec: OrderRec, reason: str) -> None:
        if rec.status in TERMINAL:
            return
        last = self._cancel_last.get(rec.order_id)
        if last and (self.clock.now() - last).total_seconds() < CANCEL_RETRY_SECONDS:
            return
        if self.session_closing:
            self.stats['eod_cancel_retries'] += 1
        if not rec.cancel_requested:
            rec.cancel_requested = True
            self.j.save_order(rec)
        log_event(log, logging.INFO, "order_cancel", f"CANCEL {rec.role} {rec.symbol} {rec.order_id} ({reason})",
                  symbol=rec.symbol, role=rec.role, order_id=rec.order_id, reason=reason)
        try:
            self._broker_snapshot_valid = False
            if self.gate.run(rec.symbol, rec.role, 'cancel', order=rec):
                self._cancel_last[rec.order_id] = self.clock.now()
        except (OrderRejected, SafetyError) as exc:
            self._cancel_last[rec.order_id] = self.clock.now()
            log_event(log, logging.WARNING, "cancel_failed",
                      f"cancel {rec.order_id} ({rec.symbol} {rec.role}) failed: {exc}; state follows from updates",
                      order_id=rec.order_id, error=str(exc))


    def positions(self) -> dict[str, Decimal]:
        """ALL positions in the Alpaca account (bot-owned and unrelated)."""
        return self._positions_snapshot if self._broker_snapshot_valid else self.broker.list_positions()


    @staticmethod
    def _clip(owned: Decimal, actual: Decimal) -> Decimal:
        if owned == 0 or actual == 0 or (owned > 0) != (actual > 0):
            return Decimal(0)
        return owned if abs(owned) <= abs(actual) else actual

    def _held(self, sym: str, owned: Decimal, actual: Decimal) -> Decimal:
        """ORB-IEX shares in `sym`: its own fills capped by what Alpaca holds. For a few seconds
        after a fill we trust the fill (Alpaca's positions endpoint can lag the order event)."""
        held = self._clip(owned, actual)
        same_side = actual == 0 or (owned > 0) == (actual > 0)
        if held != owned and same_side and abs(actual) < abs(owned):
            t = self._last_fill_at.get(sym)
            if t is not None and (self.clock.now() - t).total_seconds() < FILL_LAG_GRACE_SECONDS:
                return owned
        return held

    def bot_positions(self, persist: bool = False) -> dict[str, Decimal]:
        """Shares ORB-IEX itself holds: journal fills of ORB-IEX orders, capped by
        what Alpaca actually holds. Unrelated positions are never included, so
        EOD, sweeps and flattens can never touch them. persist=True writes the
        cap back (only when no fill can be in flight: pre-open / after close)."""
        actual = self.positions()
        out = {}
        for sym, owned in self.j.owned_qty().items():
            held = self._clip(owned, actual.get(sym, Decimal(0)))
            if persist and held != owned:
                log_event(log, logging.WARNING, "owned_reconciled",
                          f"{sym}: journal says ORB-IEX holds {owned}, Alpaca holds {actual.get(sym, 0)}; "
                          f"ORB-IEX share set to {held}", symbol=sym, owned=str(owned), held=str(held))
                self.j.adjust_owned(sym, held - owned)
            if held:
                out[sym] = held
        return out


    def bot_position(self, symbol: str) -> Decimal:
        return self.bot_positions().get(symbol, Decimal(0))


    def unrelated_positions(self) -> dict[str, Decimal]:
        """Account shares that are NOT ORB-IEX's (logged, never traded)."""
        bot = self.bot_positions()
        return {s: q - bot.get(s, Decimal(0)) for s, q in self.positions().items() if q - bot.get(s, Decimal(0))}


    def open_orders(self, symbol: str, kinds=("entry", "stop", "exit")) -> list[OrderRec]:
        return [o for o in self.j.orders(self.dkey, symbol)
                if o.status not in TERMINAL and role_kind(o.role) in kinds]


    def pending_exit_signed(self, symbol: str) -> Decimal:
        return sum(((o.qty - o.filled_qty) * _side_sign(o.side) for o in self.open_orders(symbol, ("exit",))),
                   Decimal(0))

    def _trade_or_foreign(self, symbol: str, pos: Decimal) -> TradeRec:
        t = self.j.trade(self.dkey, symbol)
        if t is None:
            t = TradeRec(day=self.dkey, symbol=symbol, direction=1 if pos > 0 else -1, planned_qty=Decimal(0),
                         entry_price=Decimal(0), stop_price=Decimal(0), atr=Decimal(0),
                         entry_client_id="", closed=True, state="foreign")
            self.j.save_trade(t)
        return t


    def market_exit(self, symbol: str, role: str, qty_signed: Decimal | None, reason: str) -> OrderRec | None:
        """Request flat. Every caller uses confirmed remaining bot/broker quantity."""
        self.j.save_risk(symbol, exit_role=role, reason=reason)
        t = self._trade_or_foreign(symbol, self.bot_position(symbol))
        t.pending_flatten = True
        self.j.save_trade(t)
        return self.reconcile_symbol(symbol)


    # ------------------------------------------------------------------ entries
    def place_entries(self, plans: list[EntryPlan], assets: dict[str, dict], account: dict,
                      now: datetime) -> None:
        """Main.cs PlaceEntry for the fixed top-20 list. Skipped plans are not replaced."""
        killed = kill_switch_active(self.settings.kill_switch_file) or bool(self.entries_blocked)
        positions = self.positions()
        open_syms = {o.symbol for o in self.broker.list_orders("open")}
        buying_power = Decimal(str(account.get("buying_power") or "0"))
        used = Decimal(0)
        self._quote_fetch_error = None
        self._quote_batch = self._fetch_quote_batch([p.symbol for p in plans], now)
        self._quote_batch_at = self.clock.now()
        for plan in plans:
            sym = plan.symbol
            skip = None
            if killed:
                skip = self.entries_blocked or "kill_switch"
            elif (self.j.trade(self.dkey, sym) is not None
                  and self.j.trade(self.dkey, sym).state not in ("foreign",)):
                skip = "already_traded_today"                 # a cancelled or skipped signal is never re-entered
            elif sym in positions or sym in open_syms:
                skip = "existing_position_or_order"
            elif plan.direction < 0:
                a = assets.get(sym, {})
                if not account.get("shorting_enabled", True):
                    skip = "short_account_disabled"
                elif not (a.get("tradable") and a.get("shortable")):
                    skip = "not_shortable"
                elif "easy_to_borrow" in a and not a.get("easy_to_borrow"):
                    skip = "not_easy_to_borrow"
            if skip is None:
                notional = plan.entry_price * plan.qty
                if used + notional > buying_power:
                    skip = "buying_power"
                    log_event(log, logging.WARNING, "sizing_difference",
                              f"{sym}: notional {notional} exceeds remaining buying power {buying_power - used}; "
                              "reference model would trade, Alpaca would not", symbol=sym,
                              notional=notional, buying_power=buying_power, used=used)
            if skip:
                level = logging.WARNING if skip.startswith(("not_", "short")) else logging.INFO
                log_event(log, level, "entry_skip", f"SKIP {sym} {plan.side}: {skip}",
                          symbol=sym, reason=skip, direction=plan.direction)
                self.j.update_candidate(self.day, sym, "skipped", skip)
                continue
            cid = client_order_id(self.day, sym, "entry")
            trade = TradeRec(day=self.dkey, symbol=sym, direction=plan.direction, planned_qty=Decimal(plan.qty),
                             entry_price=plan.entry_price, stop_price=plan.stop_price, atr=plan.atr,
                             entry_client_id=cid, state="planned")
            self.j.save_trade(trade)
            used += plan.entry_price * plan.qty
            self.submit_entry(trade)
            if self.j.trade(self.dkey, sym).state in ("entry_crossed", "not_submitted"):
                used -= plan.entry_price * plan.qty          # never submitted: buying power stays free
        self._quote_batch = None
        self._quote_fetch_error = None

    def _entry_window_closed(self, now: datetime) -> str | None:
        """Why no NEW entry may be submitted or stay working right now, or None. After the cutoff time and
        once the end-of-day window has begun an entry order must not exist (it could fill minutes or
        seconds before the close, as CBRS did on 2026-09-30)."""
        if self.schedule is None:
            return None
        if now >= self.schedule.eod_exit or self.j.day(self.day)["eod_status"] is not None:
            return "eod_window"
        if now >= self.schedule.entry_cutoff:
            return "entry_cutoff"
        return None

    def _skip_window(self, trade: TradeRec, why: str) -> None:
        self.stats["entry_window_skips"] += 1
        trade = self.j.trade(self.dkey, trade.symbol) or trade
        trade.state, trade.note = "not_submitted", why
        self.j.save_trade(trade)
        self.j.update_candidate(self.day, trade.symbol, "skipped", why)
        log_event(log, logging.WARNING, "entry_skip",
                  f"SKIP {trade.symbol} {'LONG' if trade.direction > 0 else 'SHORT'}: {why} (entry window closed)",
                  symbol=trade.symbol, reason=why, direction=trade.direction)
        self._tl("skip", trade.symbol, key=f"skip:{trade.entry_client_id}", role="entry", order_type="stop",
                 client_order_id=trade.entry_client_id, status="skipped", detail={"reason": why},
                 event_time=self.clock.now().isoformat())

    def submit_entry(self, trade: TradeRec) -> None:
        guard = getattr(self, 'entry_guard', None)
        if guard is not None and guard():
            return  # preserve a recovered plan; a closed startup gate must not submit it
        if self._shutting_down or self.gate.journal_failed or self.j.breakers():
            self._skip_window(trade, 'safety_breaker')
            return
        if trade.entry_order_id or trade.state in ("entry_cutoff_pending", "entry_cutoff_canceled", "entry_crossed",
                                                   "not_submitted", "entry_fatal_canceled"):
            log_event(log, logging.ERROR, "entry_not_resubmitted",
                      f"{trade.symbol}: entry already {trade.state}; it is never re-entered the same day",
                      symbol=trade.symbol, state=trade.state)
            return
        closed = self._entry_window_closed(self.clock.now())
        if closed:
            self._skip_window(trade, closed)
            return
        trigger = _px(trade.entry_price)
        crossed = self._entry_crossed(trade, trigger)
        if crossed:
            if crossed.get("detected_by") in ("stale_quote", "quote_unavailable", "spread_too_wide"):
                self._skip_quote(trade, crossed)
            else:
                self._skip_crossed(trade, crossed)
            return
        req = OrderRequest(trade.symbol, "buy" if trade.direction > 0 else "sell", trade.planned_qty, "stop",
                           trade.entry_client_id, stop_price=trigger)
        rec = self._submit(req, "entry", "ORB entry")
        trade = self.j.trade(self.dkey, trade.symbol)
        if rec is None:
            exc = self.last_reject
            if isinstance(exc, OrderUncertain):
                # The reply was lost: the order may exist. Never resubmit blindly; the
                # next reconcile adopts it (same client_order_id) if Alpaca has it.
                self.stats["entry_unconfirmed"] += 1
                trade.state = "entry_open"
                self.j.update_candidate(self.day, trade.symbol, "submitted", "unconfirmed")
                log_event(log, logging.WARNING, "entry_unconfirmed",
                          f"{trade.symbol}: entry submit got no reply; not resubmitting, waiting for reconcile",
                          symbol=trade.symbol)
            elif exc is not None and is_crossed_stop_rejection(exc):
                self.stats["entry_crossed_422"] += 1
                self._skip_crossed(trade, {"detected_by": "alpaca_422", "trigger": str(trigger),
                                           "error": str(exc)})
                return                                  # exactly once: no retry of an invalid stop entry
            else:
                trade.state = "entry_rejected"
                self.j.update_candidate(self.day, trade.symbol, "skipped", "entry_rejected")
        else:
            trade.entry_order_id = rec.order_id
            trade.state = "entry_open"
            self.j.update_candidate(self.day, trade.symbol, "submitted")
        self.j.save_trade(trade)

    def _fetch_quote_batch(self, symbols: list[str], now: datetime) -> dict | None:
        """One IEX quote request for the whole entry batch, taken right before the first submit (a call per
        symbol would delay the last order by seconds). None = no quotes (and `_quote_fetch_error` says why):
        every entry of the run is then skipped, never sent unchecked."""
        if self.quote_source is None:
            self._quote_fetch_error = "no_quote_source"
            return None
        if not symbols:
            return None
        try:
            return dict(self.quote_source(sorted(set(symbols)), now))
        except Exception as exc:
            self._quote_fetch_error = f"fetch_failed: {type(exc).__name__}: {exc}"
            log_event(log, logging.WARNING, "quote_unavailable",
                      f"entry quote request failed ({type(exc).__name__}: {exc}); the entries are skipped, not sent")
            return None

    def _entry_quote(self, trade: TradeRec) -> tuple[object | None, datetime, str | None]:
        """(quote, receipt time, why there is none). The batch quote is used while it is still fresh enough
        for the age limit; a later symbol in a long batch gets its own request instead."""
        sym, now = trade.symbol, self.clock.now()
        if self.quote_source is None:
            return None, now, "no_quote_source"
        limit = float(self.settings.max_quote_age_seconds) * QUOTE_BATCH_REFRESH_FRACTION
        if (self._quote_batch is not None and self._quote_batch_at is not None
                and (now - self._quote_batch_at).total_seconds() <= limit):
            q = self._quote_batch.get(sym)
            return q, self._quote_batch_at, (None if q is not None else "no_quote_for_symbol")
        if self._quote_fetch_error:                 # one failure in this run: the rest fail fast, no retry storm
            return None, now, self._quote_fetch_error
        try:
            q = self.quote_source([sym], now).get(sym)
        except Exception as exc:
            self._quote_fetch_error = f"fetch_failed: {type(exc).__name__}: {exc}"
            log_event(log, logging.WARNING, "quote_unavailable",
                      f"{sym}: quote request failed ({type(exc).__name__}: {exc}); entry skipped", symbol=sym)
            return None, now, self._quote_fetch_error
        return q, self.clock.now(), (None if q is not None else "no_quote_for_symbol")

    def _quote_record(self, q, receipt: datetime, decided: datetime) -> dict:
        """Everything worth knowing about the quote an entry decision looked at (logged AND journaled)."""
        spread = None if q.bid is None or q.ask is None else Decimal(q.ask) - Decimal(q.bid)
        mid = None if spread is None else (Decimal(q.ask) + Decimal(q.bid)) / 2
        spread_pct = None if not mid or mid <= 0 else str((spread / mid * 100).quantize(Decimal("0.001")))
        age = None if q.t is None else round((decided - q.t).total_seconds(), 3)
        return {"quote_exchange_time": None if q.t is None else q.t.isoformat(),
                "received_at": receipt.isoformat(), "decision_time": decided.isoformat(),
                "quote_age_seconds": age, "max_quote_age_seconds": self.settings.max_quote_age_seconds,
                "max_quote_spread_pct": str(self.settings.max_quote_spread_pct),
                "bid": None if q.bid is None else str(q.bid), "ask": None if q.ask is None else str(q.ask),
                "spread": None if spread is None else str(spread), "spread_pct": spread_pct,
                "feed": getattr(q, "feed", "iex")}

    def _quote_record_none(self, receipt: datetime, decided: datetime, cause: str | None) -> dict:
        return {"quote_exchange_time": None, "received_at": receipt.isoformat(), "decision_time": decided.isoformat(),
                "quote_age_seconds": None, "max_quote_age_seconds": self.settings.max_quote_age_seconds,
                "max_quote_spread_pct": str(self.settings.max_quote_spread_pct),
                "bid": None, "ask": None, "spread": None, "spread_pct": None, "feed": "iex", "cause": cause}

    def _quote_stale_reason(self, rec: dict) -> str | None:
        """Why this quote may NOT be used for an entry decision, or None. An undated quote cannot be aged, so it
        is as unusable as an old one."""
        age = rec["quote_age_seconds"]
        if age is None:
            return "quote_no_timestamp"
        if age < -QUOTE_MAX_FUTURE_SECONDS:
            return "quote_from_future"
        if age > self.settings.max_quote_age_seconds:
            return "stale_quote"
        return None

    def _skip_quote(self, trade: TradeRec, info: dict) -> None:
        """No quote this entry can be judged on (too old, undated, missing, one-sided): fail closed. Alpaca's own
        order validation is NOT the fallback. Skip, journal the reason, no order, no retry, never re-entered."""
        reason = info["reason"]
        self.stats["quote_unavailable_skipped" if reason == "skip_quote_unavailable" else
                   "spread_too_wide_skipped" if reason == "skip_spread_too_wide" else
                   "candidate_liquidity_skipped" if reason.startswith("candidate_") else "stale_quote_skipped"] += 1
        trade = self.j.trade(self.dkey, trade.symbol) or trade
        trade.state, trade.note = "not_submitted", reason
        self.j.save_trade(trade)
        self.j.update_candidate(self.day, trade.symbol, "skipped", reason)
        fields = {k: v for k, v in info.items() if k not in ("detected_by", "reason")}
        log_event(log, logging.WARNING, "entry_skip",
                  f"SKIP {trade.symbol} {'LONG' if trade.direction > 0 else 'SHORT'}: {reason} "
                  f"(quote age {fields.get('quote_age_seconds')}s, limit {fields.get('max_quote_age_seconds')}s, "
                  f"bid {fields.get('bid')} ask {fields.get('ask')})",
                  symbol=trade.symbol, reason=reason, direction=trade.direction, **fields)
        self._tl("skip", trade.symbol, key=f"skip:{trade.entry_client_id}", role="entry", order_type="stop",
                 side="buy" if trade.direction > 0 else "sell", client_order_id=trade.entry_client_id,
                 status="skipped", detail={"reason": reason, **fields}, event_time=self.clock.now().isoformat())

    def _entry_crossed(self, trade: TradeRec, trigger: Decimal) -> dict | None:
        """The entry gate. None = send the order. Otherwise the skip to record.
        1. FAIL CLOSED: no quote, an undated/old/future quote, a one-sided or locked/crossed book -> skip
           ("skip_quote_unavailable" / "stale_quote"...). Alpaca's order validation is not a fallback for missing data.
        2. A long stop entry needs its trigger above the ask, a short one below the bid (by at least a cent,
           Alpaca's rule); otherwise "crossed_before_submit".
        3. FAIL CLOSED on spread: a quote wider than trading.max_quote_spread_pct (default 1% of the mid) cannot
           judge the trigger (on 2026-09-30 it wrongly flagged KOD and CNK, which Alpaca accepted) -> no order,
           skipped for the day, "skip_spread_too_wide" journaled with bid, ask, spread, quote age and feed.
        Every decision is journaled with the quote's exchange time, receipt time, age, bid, ask, spread and feed."""
        sym = trade.symbol
        q, receipt, cause = self._entry_quote(trade)
        decided = self.clock.now()
        if q is None:
            rec = self._quote_record_none(receipt, decided, cause)
            verdict = "skip_quote_unavailable"
        else:
            rec = self._quote_record(q, receipt, decided)
            verdict = self._quote_stale_reason(rec)
            if verdict is None and (q.bid is None or q.ask is None):
                verdict, rec["cause"] = "skip_quote_unavailable", "one_sided_quote"
            elif verdict is None and q.ask <= q.bid:
                verdict, rec["cause"] = "skip_quote_unavailable", "locked_or_crossed_book"
            elif verdict is None and (Decimal(q.ask) - Decimal(q.bid)) / ((Decimal(q.ask) + Decimal(q.bid)) / 2) * 100 \
                    > self.settings.max_quote_spread_pct:
                verdict = "skip_spread_too_wide"        # FAIL CLOSED: a quote this wide cannot judge the trigger
        baseline_reason = verdict
        if baseline_reason is None:
            ref = q.ask if trade.direction > 0 else q.bid
            if (trigger < ref + ENTRY_MIN_CLEARANCE if trade.direction > 0
                    else trigger > ref - ENTRY_MIN_CLEARANCE):
                baseline_reason = "crossed_before_submit"
        pair = (self.j.diagnostic(self.day, "ab_plans") or {}).get(sym, {})
        bqty = pair.get("candidate_qty")
        comparison = {"baseline_reason": baseline_reason, "baseline_qty": pair.get("baseline_qty"),
                      "candidate_reason": baseline_reason or pair.get("candidate_reason"),
                      "candidate_qty": bqty, "decision_time": decided.isoformat(),
                      "profile": self.settings.research_profile}
        if baseline_reason is None and comparison["candidate_reason"] is None:
            if bqty is None:
                comparison["candidate_reason"] = "candidate_missing_paired_plan"
            elif trade.planned_qty > bqty and self.settings.research_profile == "candidate_b":
                comparison["candidate_reason"] = "candidate_quantity_exceeds_cap"
            else:
                check = liquidity_check(q, direction=trade.direction, qty=bqty,
                                        risk=abs(trade.entry_price - trade.stop_price),
                                        commission=self.settings.commission_per_share,
                                        slippage=self.settings.slippage_per_share)
                comparison.update(check)
                comparison["candidate_reason"] = check["reason"]
        rec["ab"] = comparison
        self.j.save_diagnostic(self.day, "ab_quote:" + sym, {**rec, "baseline_reason": baseline_reason})
        if self.settings.research_profile == "candidate_b" and baseline_reason is None:
            verdict = comparison["candidate_reason"]
        log_event(log, logging.INFO if verdict is None else logging.WARNING, "entry_quote",
                  f"{sym}: quote exchange_ts={rec['quote_exchange_time']} received={rec['received_at']} "
                  f"age={rec['quote_age_seconds']}s bid={rec['bid']} ask={rec['ask']} spread={rec['spread']} "
                  f"feed={rec['feed']} -> {'USABLE' if verdict is None else verdict}"
                  f"{'' if not rec.get('cause') else ' (' + str(rec['cause']) + ')'}", symbol=sym,
                  verdict=verdict or "usable", **rec)
        self._tl("entry_quote", sym, key=f"quote:{trade.entry_client_id}", role="entry",
                 client_order_id=trade.entry_client_id, status=verdict or "usable",
                 detail=rec, event_time=decided.isoformat())
        if verdict is not None:
            kind = ("stale_quote" if verdict in ("stale_quote", "quote_no_timestamp", "quote_from_future") else
                    "spread_too_wide" if verdict == "skip_spread_too_wide" else "quote_unavailable")
            return {"detected_by": kind, "reason": verdict, "trigger": str(trigger), **rec}
        ref = Decimal(q.ask if trade.direction > 0 else q.bid)
        is_crossed = trigger < ref + ENTRY_MIN_CLEARANCE if trade.direction > 0 else trigger > ref - ENTRY_MIN_CLEARANCE
        if not is_crossed:
            return None
        return {"detected_by": "quote_precheck", "trigger": str(trigger), "ask": str(q.ask), "bid": str(q.bid),
                "quote_time": None if q.t is None else q.t.isoformat()}

    def _skip_crossed(self, trade: TradeRec, info: dict) -> None:
        """The ORB trigger is already crossed: skip the trade. No market order, no retry."""
        self.stats["entry_crossed_skipped"] += 1
        trade = self.j.trade(self.dkey, trade.symbol) or trade
        trade.state = "entry_crossed"
        trade.note = "crossed_before_submit"
        self.j.save_trade(trade)
        self.j.update_candidate(self.day, trade.symbol, "skipped", "crossed_before_submit")
        log_event(log, logging.WARNING, "entry_skip",
                  f"SKIP {trade.symbol} {'LONG' if trade.direction > 0 else 'SHORT'}: crossed_before_submit "
                  f"({info.get('detected_by')}: trigger {info.get('trigger')}, ask {info.get('ask')}, "
                  f"bid {info.get('bid')})",
                  symbol=trade.symbol, reason="crossed_before_submit", direction=trade.direction, **info)
        self._tl("skip", trade.symbol, key=f"skip:{trade.entry_client_id}", role="entry", order_type="stop",
                 side="buy" if trade.direction > 0 else "sell", client_order_id=trade.entry_client_id,
                 price=info.get("trigger"), status="skipped",
                 detail={"reason": "crossed_before_submit", **info}, event_time=self.clock.now().isoformat())

    # --------------------------------------------------------------- updates
    def _apply(self, u: OrderUpdate, side_effects: bool = True) -> tuple[OrderRec | None, Decimal, str | None]:
        with self.j.transaction():
            result = self._apply_snapshot(u)
        rec, delta, status = result
        if rec is not None:
            if delta:
                self._last_fill_at[rec.symbol] = self.clock.now()
                price = next(f['price'] for f in reversed(self.j.fills(rec.day)) if f['order_id'] == rec.order_id)
                self._tl('fill', rec.symbol, key=f'fill:{rec.order_id}:{rec.filled_qty}', role=rec.role,
                         order_type=rec.type, side=rec.side, client_order_id=rec.client_order_id,
                         order_id=rec.order_id, qty=delta, price=price, cum_qty=rec.filled_qty,
                         status=rec.status, detail={'order_qty': str(rec.qty), 'source': u.source}, event_time=u.timestamp)
            if status:
                self._tl('status', rec.symbol, key=f'status:{rec.order_id}:{status}', role=rec.role,
                         order_id=rec.order_id, client_order_id=rec.client_order_id, status=status,
                         cum_qty=rec.filled_qty, detail={'source': u.source}, event_time=u.timestamp)
            if u.event in ('fill', 'partial_fill') and u.filled_qty > 0 and u.source in ('stream', 'sim'):
                key = (u.order_id, str(u.filled_qty))
                if key in self._stream_fill_events:
                    self._tl('duplicate', rec.symbol, role=rec.role, order_id=rec.order_id, cum_qty=u.filled_qty)
                self._stream_fill_events.add(key)
        return result


    def on_update(self, u: OrderUpdate) -> None:
        self._broker_snapshot_valid = False
        rec, delta, new_status = self._apply(u)
        if rec is None or rec.day != self.dkey:
            return
        trade = self.j.trade(self.dkey, rec.symbol)
        kind = role_kind(rec.role)
        if trade is None:
            if kind == "entry" and rec.status not in TERMINAL:
                self.j.save_risk(rec.symbol, exit_role='flatten', reason='entry without plan')
            if kind == "entry" and delta > 0:
                t = self._trade_or_foreign(rec.symbol, delta * _side_sign(rec.side))
                t.pending_flatten = True
                self.j.save_trade(t)
            self.reconcile_symbol(rec.symbol)
            return
        if kind == "entry":
            if trade.entry_order_id is None:
                trade.entry_order_id = rec.order_id
                self.j.save_trade(trade)
            if delta > 0:
                self.on_entry_fill(trade, rec, delta)
            elif new_status in LOST and rec.filled_qty == 0 and trade.state == "entry_open":
                trade.state = f"entry_{new_status}"
                self.j.save_trade(trade)
        elif kind == "stop":
            if new_status == 'filled' or (delta > 0 and rec.filled_qty > 0):
                trade.closed, trade.state = True, 'stopped_out'
                trade.pending_flatten = True
                self.j.save_trade(trade)
                self.j.save_risk(rec.symbol, exit_role='flatten', reason='stop execution')
            elif new_status in LOST and not rec.cancel_requested and trade.stop_order_id == rec.order_id:
                self.stop_lost(trade, f'stop {new_status}')
        elif kind == "exit":
            if new_status in LOST and rec.filled_qty < rec.qty:
                log_event(log, logging.ERROR, "exit_failed",
                          f"exit {rec.client_order_id} ended {new_status}; will retry flatten",
                          symbol=rec.symbol, order_id=rec.order_id, status=new_status)
                trade.pending_flatten = True
                self.j.save_trade(trade)
        self.reconcile_symbol(rec.symbol)


    def fill_stop_price(self, trade: TradeRec) -> Decimal:
        """Paper: protective stop at 10% ATR from the EXECUTED entry price. Uses the
        entry's cumulative average fill price (falls back to the planned level)."""
        entry = self.j.order(trade.entry_order_id) if trade.entry_order_id else None
        avg = entry.filled_avg_price if entry else None
        if avg is None or not trade.atr:
            return trade.stop_price
        raw = avg - trade.direction * C.STOP_ATR_FRACTION * trade.atr
        return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def on_entry_fill(self, trade: TradeRec, entry: OrderRec, delta: Decimal) -> None:
        if trade.state == 'entry_cutoff_pending':
            self.stats['cutoff_race_fills'] += 1
            self.j.update_candidate(self.day, trade.symbol, 'submitted', 'cutoff_race_fill')
        risk = self.j.risk(trade.symbol)
        if risk['unprotected_since'] is None:
            self.j.save_risk(trade.symbol, unprotected_since=self.clock.now().isoformat())
        if self.session_closing or trade.closed:
            self.j.save_risk(trade.symbol, exit_role='lateexit', reason='late entry fill')
            trade.pending_flatten = True
            self.j.save_trade(trade)


    def _place_protective_stop(self, trade: TradeRec, qty: Decimal) -> None:
        self.reconcile_symbol(trade.symbol)


    # ------------------------------------------------------------- entry cutoff
    def enforce_entry_cutoff(self, now: datetime) -> None:
        """At the cutoff every unfilled ORB entry is cancelled and confirmed terminal; a cancel that
        failed or was ignored is re-sent; a fill during the cancel race is protected. A cancelled entry
        is never re-entered that day (submit_entry refuses once the window is closed)."""
        if self.schedule is None or now < self.schedule.entry_cutoff:
            return
        if not self._cutoff_adopted:                       # ORB-IEX orders the journal missed (crash)
            self._cutoff_adopted = True
            try:
                for u in self.broker.list_orders("open"):
                    parsed = parse_client_order_id(u.client_order_id)
                    if parsed is None or parsed[0] != self.day or self.j.order(u.order_id) is not None:
                        continue
                    rec, _, _ = self._apply(u, side_effects=False)
                    if rec is not None and role_kind(rec.role) == "entry":
                        self._cancel(rec, "Entry cutoff (adopted ORB-IEX order)")
            except ConnectionError as exc:
                self._cutoff_adopted = False
                log_event(log, logging.WARNING, "cutoff_adopt_failed", f"cutoff order scan failed: {exc}")
        working = []
        for o in self.j.orders(self.dkey):                  # an entry with no plan (journal lost): cannot be managed
            if role_kind(o.role) == "entry" and o.status not in TERMINAL and self.j.trade(self.dkey, o.symbol) is None:
                self._cancel(o, "Entry cutoff (no plan)")
        for trade in self.j.trades(self.dkey):
            if trade.state == "foreign" or trade.closed:
                continue
            if trade.entry_order_id:
                entry = self.j.order(trade.entry_order_id)
                if entry is not None and entry.status not in TERMINAL:
                    working.append((trade, entry))
            elif trade.state == "entry_open":              # the submit reply was lost: is the order there?
                try:
                    u = self.broker.get_order_by_client_id(trade.entry_client_id)
                except ConnectionError as exc:             # inconclusive: asked again on the next tick
                    log_event(log, logging.WARNING, "cutoff_lookup_failed",
                              f"{trade.symbol}: entry lookup at the cutoff was inconclusive: {exc}", symbol=trade.symbol)
                    continue
                if u is not None:
                    rec = self.j.order(u.order_id) or self._record(u, "entry")
                    self._apply(u, side_effects=False)
                    trade.entry_order_id = rec.order_id
                    self.j.save_trade(trade)
        if working and not self._cutoff_logged:
            self._cutoff_logged = True
            log_event(log, logging.WARNING, "entry_cutoff",
                      f"ENTRY CUTOFF {self.schedule.entry_cutoff.astimezone(timezone.utc).strftime('%H:%M')}Z: "
                      f"cancelling {len(working)} unfilled entries {[t.symbol for t, _ in working]}",
                      symbols=[t.symbol for t, _ in working])
        for trade, entry in working:
            if entry.filled_qty == 0 and trade.state == "entry_open":
                trade.state, trade.note = "entry_cutoff_pending", "entry_cutoff"
                self.j.save_trade(trade)
                self.stats["entry_cutoff_canceled"] += 1
                self.j.update_candidate(self.day, trade.symbol, "skipped", "entry_cutoff")
                self._tl("skip", trade.symbol, key=f"cutoff:{trade.entry_client_id}", role="entry",
                         order_type="stop", client_order_id=trade.entry_client_id, order_id=entry.order_id,
                         status="skipped", detail={"reason": "entry_cutoff"},
                         event_time=now.isoformat())
            self._cancel(entry, "Entry cutoff")             # re-sent every CANCEL_RETRY_SECONDS until terminal
            self._confirm_cutoff_cancel(trade, entry, now)

    def _confirm_cutoff_cancel(self, trade: TradeRec, entry: OrderRec, now: datetime) -> None:
        """Ask Alpaca for the order's own state: terminal confirms the cancel, a fill is a race fill."""
        last = self._cutoff_confirm_last.get(entry.order_id)
        if last and (now - last).total_seconds() < CUTOFF_CONFIRM_SECONDS:
            return
        self._cutoff_confirm_last[entry.order_id] = now
        try:
            u = self.broker.get_order_by_client_id(entry.client_order_id)
        except ConnectionError:
            u = None
        if u is not None:
            self.on_update(u)                              # monotonic merge; a fill is protected by on_update
        entry = self.j.order(entry.order_id)
        if entry and entry.status in TERMINAL and entry.filled_qty == 0:
            current = self.j.trade(self.dkey, trade.symbol)
            if current and current.state == 'entry_cutoff_pending':
                current.state = 'entry_cutoff_canceled'
                self.j.save_trade(current)
        if (entry is not None and entry.status not in TERMINAL
                and (now - self.schedule.entry_cutoff).total_seconds() > CUTOFF_UNCONFIRMED_ERROR_SECONDS
                and entry.order_id not in self._cutoff_unconfirmed_logged):
            self._cutoff_unconfirmed_logged.add(entry.order_id)
            log_event(log, logging.ERROR, "cutoff_cancel_unconfirmed",
                      f"{trade.symbol}: entry still {entry.status} {CUTOFF_UNCONFIRMED_ERROR_SECONDS}s after the "
                      "cutoff cancel; keeps cancelling", symbol=trade.symbol, status=entry.status)

    # ------------------------------------------------------ stop reconciliation
    def _scan_stop_mismatches(self) -> None:
        """Self-heal: queue any active trade whose live stop does not cover its entry fills."""
        for trade in self.j.trades(self.dkey):
            if trade.closed or trade.state == "foreign" or not trade.entry_order_id:
                continue
            entry = self.j.order(trade.entry_order_id)
            stop = self.current_stop(trade)
            if entry is None or entry.filled_qty <= 0:
                continue
            if stop is None:
                if self._stop_attempts.get(trade.symbol):
                    self._stop_dirty.add(trade.symbol)
            elif stop.qty != entry.filled_qty:
                self._stop_dirty.add(trade.symbol)

    def _stop_chain_ids(self, head: OrderRec | None, symbol: str | None = None) -> set[str]:
        """Order ids linked to the live stop by replaces / replaced_by (a stop being replaced is
        not a second stop)."""
        if head is None:
            return set()
        ids = {head.order_id}
        others = [o for o in self.j.orders(self.dkey, symbol or head.symbol) if role_kind(o.role) == "stop"]
        changed = True
        while changed:
            changed = False
            for o in others:
                if o.order_id not in ids and (o.replaced_by in ids or o.replaces in ids):
                    ids.add(o.order_id)
                    changed = True
            for o in others:
                if o.order_id in ids:
                    for link in (o.replaces, o.replaced_by):
                        if link and link not in ids:
                            ids.add(link)
                            changed = True
        return ids

    def _enforce_single_stop(self, trade: TradeRec) -> None:
        """At most one live protective stop per symbol: cancel any other live stop."""
        cur = self.current_stop(trade)
        if cur is None:
            return
        keep = self._stop_chain_ids(cur)
        for o in self.stop_chain(trade):
            if o.order_id in keep or o.status in TERMINAL or o.cancel_requested:
                continue
            self.stats["duplicate_stops_canceled"] += 1
            log_event(log, logging.ERROR, "duplicate_stop",
                      f"{trade.symbol}: second live stop {o.order_id} found; canceling it", symbol=trade.symbol,
                      order_id=o.order_id)
            self._cancel(o, "duplicate stop")

    def flush_stops(self) -> None:
        self._broker_snapshot_valid = False
        self._refresh()
        for sym in sorted(set(self.j.owned_qty()) | {t.symbol for t in self.j.trades(self.dkey)}):
            self.reconcile_symbol(sym)


    def _replace_stop(self, trade: TradeRec, stop: OrderRec, qty: Decimal, price: Decimal) -> None:
        self.reconcile_symbol(trade.symbol)


    def current_stop(self, trade: TradeRec) -> OrderRec | None:
        """Follow the replacement chain to the live stop. None if no open stop."""
        oid, seen = trade.stop_order_id, set()
        rec = None
        while oid and oid not in seen:
            seen.add(oid)
            rec = self.j.order(oid)
            if rec is None:
                return None
            if rec.status == "replaced" and rec.replaced_by:
                oid = rec.replaced_by
                continue
            break
        if rec is None or rec.status in TERMINAL:
            return None
        return rec

    def stop_chain(self, trade: TradeRec) -> list[OrderRec]:
        return [o for o in self.j.orders(self.dkey, trade.symbol) if role_kind(o.role) == "stop"]

    def _link_stop(self, trade: TradeRec, stop: OrderRec) -> None:
        """Adopt a live stop the trade did not know about (its submit reply was lost)."""
        log_event(log, logging.WARNING, "stop_adopted",
                  f"{trade.symbol}: adopted live stop {stop.order_id} ({stop.client_order_id}) qty {stop.qty}",
                  symbol=trade.symbol, order_id=stop.order_id)
        trade.stop_order_id = stop.order_id
        trade.protected_qty = stop.qty - stop.filled_qty
        trade.stop_price = stop.stop_price or trade.stop_price
        trade.state = "active"
        self.j.save_trade(trade)
        self._stop_attempts.pop(trade.symbol, None)
        self._stop_dirty.add(trade.symbol)

    def on_stop_update(self, trade: TradeRec, stop: OrderRec, new_status: str | None) -> None:
        if trade.stop_order_id is None and stop.status not in TERMINAL and not trade.closed \
                and not stop.cancel_requested:
            self._link_stop(trade, stop)
            return
        if new_status == "filled":
            if not trade.closed:
                trade.closed = True
                trade.state = "stopped_out"
                self.j.save_trade(trade)
                log_event(log, logging.INFO, "trade_closed", f"{trade.symbol} stop filled; trade closed",
                          symbol=trade.symbol)
            entry = self.j.order(trade.entry_order_id) if trade.entry_order_id else None
            if entry and entry.status not in TERMINAL:
                self._cancel(entry, "Stop filled")
            return
        if new_status == "replaced":
            if stop.replaced_by and trade.stop_order_id == stop.order_id:
                trade.stop_order_id = stop.replaced_by
                self.j.save_trade(trade)
            return
        if new_status and new_status not in TERMINAL:
            self._stop_dirty.add(trade.symbol)       # e.g. pending_new -> new: a queued change may now apply
        if new_status in LOST:
            current = trade.stop_order_id == stop.order_id
            if current and not stop.cancel_requested:
                self.stop_lost(trade, f"stop {new_status}")

    def stop_lost(self, trade: TradeRec, why: str) -> None:
        if self.session_closing or trade.closed:
            return
        self._tl('stop_lost', trade.symbol, role='stop', status='canceled' if 'cancel' in why else 'rejected' if 'reject' in why else why,
                 detail={'why': why}, event_time=self.clock.now().isoformat())
        trade.closed = True
        trade.state = "stop_failed"
        trade.pending_flatten = True
        self.j.save_trade(trade)
        self.j.save_risk(trade.symbol, exit_role='stopfail', reason=why)
        self.j.set_breaker('unprotected_position', trade.symbol, why)


    # ------------------------------------------------------- graceful shutdown
    def _entry_cancel_ids(self) -> dict[str, str]:
        entries = [o for o in self.j.orders() if role_kind(o.role) == 'entry' and o.status not in TERMINAL]
        counts = {o.symbol: sum(e.symbol == o.symbol for e in entries) for o in entries}
        return {o.symbol if counts[o.symbol] == 1 else f'{o.symbol}:{o.order_id}': o.order_id for o in entries}

    def graceful_shutdown(self, reason: str) -> dict:
        """Ctrl+C / SIGTERM. Nothing may be left live at Alpaca that can fill while the bot is gone:
        1. block every new entry at once (before any network call);
        2. cancel every ORB-IEX entry order of the session that is still unfilled (only `orbiex-` orders of the
           day: manual/foreign orders are never touched) and reconcile until each is confirmed terminal, or until
           `runtime.shutdown_timeout_seconds` is reached;
        3. a fill that won the cancel race is a position: a valid working protective stop is confirmed, otherwise it is
           flattened (nobody will be here to flatten it at the close) and the flatten is waited for;
        4. protective stops of positions that were already open are NOT cancelled (only a position that is being
           flattened loses its stop, because the flatten needs the shares).
        Every step is journaled (shutdown_requested, entry_cancel_requested, entry_cancel_confirmed,
        fill_won_cancel_race, position_protected_on_shutdown, position_flattened_on_shutdown, shutdown_incomplete).
        Never raises (a second Ctrl+C included); returns the report."""
        report: dict = {"reason": reason, "at": self.clock.now().isoformat(), "entries": {}, "positions": {},
                        "errors": [], "complete": False}
        self.entries_blocked = SHUTDOWN_FLAG                      # first, before anything can reach the network
        self._tl("shutdown_requested", None, detail={"reason": reason}, event_time=report["at"])
        log_event(log, logging.WARNING, "shutdown_requested",
                  f"SHUTDOWN requested ({reason}): new entries blocked; cancelling unfilled ORB-IEX entries",
                  reason=reason)
        try:
            self.j.set_breaker('shutdown_incomplete', detail=reason)
            self._graceful_shutdown_body(report)
            self.flush_stops()
            if self._entry_cancel_ids() or self.j.intents(True):
                report['errors'].append('broker entries or mutation intents remain unresolved')
        except sqlite3.Error as exc:
            report['errors'].append(f'journal failure: {exc}')
            report['emergency_flat'] = self.emergency_shutdown()
        except KeyboardInterrupt:
            report["errors"].append("interrupted again during the shutdown")
        except Exception as exc:
            report["errors"].append(f"{type(exc).__name__}: {exc}")
        problems = {s: e["action"] for s, e in report["entries"].items() if e["action"] == "cancel_unconfirmed"}
        problems.update({s: p["action"] for s, p in report["positions"].items()
                         if p["action"] in ("flatten_pending", "flatten_not_confirmed")})
        report["complete"] = not problems and not report["errors"]
        if report['complete']:
            self.j.clear_breaker('shutdown_incomplete')
            self.j.set_meta('shutdown', 'complete')
        if not report["complete"]:
            self._tl("shutdown_incomplete", None, detail={"problems": problems, "errors": report["errors"]},
                     event_time=self.clock.now().isoformat())
            log_event(log, logging.CRITICAL, "shutdown_incomplete",
                      f"SHUTDOWN INCOMPLETE: {problems or ''} {report['errors'] or ''} - check the Alpaca dashboard "
                      "for live ORB-IEX orders and open positions", problems=problems, errors=report["errors"])
        else:
            log_event(log, logging.WARNING, "shutdown_complete",
                      f"shutdown complete: entries {sorted(report['entries'])} confirmed, positions "
                      f"{ {s: p['action'] for s, p in report['positions'].items()} }")
        return report

    def _graceful_shutdown_body(self, report: dict) -> None:
        if self.day is None or self.schedule is None:
            report["errors"].append("no session started yet: nothing to cancel")
            return
        timeout = float(self.settings.shutdown_timeout_seconds)
        deadline = self.clock.now() + timedelta(seconds=timeout)
        try:
            self.reconcile(self.clock.now(), side_effects=False)       # ingest Alpaca's state (adopts unknown orbiex- orders)
        except ConnectionError as exc:
            report["errors"].append(f"Alpaca unreachable ({exc}): entries could not be cancelled")
            return
        ids = self._entry_cancel_ids()
        for sym, oid in sorted(ids.items()):
            report["entries"][sym] = {"order_id": oid, "action": "cancel_requested"}
            self._tl("entry_cancel_requested", sym, key=f"shutdown_cancel:{oid}", role="entry", order_id=oid,
                     detail={"reason": "shutdown"}, event_time=self.clock.now().isoformat())
        # ---- cancel and confirm, bounded
        while ids:
            pending = [self.j.order(oid) for oid in ids.values()]
            pending = [o for o in pending if o is not None and o.status not in TERMINAL]
            if not pending:
                break
            for o in pending:
                self._cancel(o, "Shutdown: unfilled entry cancelled")       # re-sent every CANCEL_RETRY_SECONDS
            try:
                self.reconcile(self.clock.now(), side_effects=False)
            except ConnectionError as exc:
                report["errors"].append(f"reconcile failed during the cancel: {exc}")
                break
            if self.clock.now() >= deadline:
                break
            if any(self.j.order(oid) is not None and self.j.order(oid).status not in TERMINAL for oid in ids.values()):
                self.clock.sleep(1.0)
        for sym, oid in sorted(ids.items()):
            rec = self.j.order(oid)
            if rec is None:
                continue
            if rec.filled_qty > 0:
                report["entries"][sym].update(action="fill_won_cancel_race", filled=str(rec.filled_qty), status=rec.status)
                self._tl("fill_won_cancel_race", sym, key=f"shutdown_race:{oid}", role="entry", order_id=oid,
                         qty=rec.filled_qty, status=rec.status,
                         detail={"filled": str(rec.filled_qty), "order_qty": str(rec.qty)},
                         event_time=self.clock.now().isoformat())
                log_event(log, logging.CRITICAL, "fill_won_cancel_race",
                          f"{sym}: the entry filled {rec.filled_qty} while it was being cancelled; the position is "
                          "protected or flattened before exit", symbol=sym, filled=str(rec.filled_qty))
            elif rec.status in TERMINAL:
                report["entries"][sym].update(action="entry_cancel_confirmed", status=rec.status)
                trade = self.j.trade(self.dkey, sym)
                if trade is not None and trade.state in ("entry_open", "entry_cutoff_pending"):
                    trade.state, trade.note = "entry_shutdown_canceled", "shutdown"
                    self.j.save_trade(trade)
                    self.j.update_candidate(self.day, sym, "skipped", "shutdown_entry_canceled")
                self._tl("entry_cancel_confirmed", sym, key=f"shutdown_confirmed:{oid}", role="entry", order_id=oid,
                         status=rec.status, detail={"broker_status": rec.status},
                         event_time=self.clock.now().isoformat())
            else:
                report["entries"][sym].update(action="cancel_unconfirmed", status=rec.status)
        # ---- positions: protected as they are, or flattened (and the flatten waited for)
        held = self.bot_positions()
        waiting: dict[str, str] = {}
        for sym, pos in sorted(held.items()):
            action = self._fatal_protect_one(sym, pos, self.clock.now())    # protected / exit_working / flatten_initiated
            report["positions"][sym] = {"qty": str(pos), "action": action}
            if action == "protected":
                self._tl("position_protected_on_shutdown", sym, key=f"shutdown_protected:{sym}", qty=pos,
                         detail={"qty": str(pos), "note": "a working protective stop stays at Alpaca"},
                         event_time=self.clock.now().isoformat())
                log_event(log, logging.WARNING, "position_protected_on_shutdown",
                          f"{sym}: {pos} shares stay open with their protective stop", symbol=sym, qty=str(pos))
            else:
                waiting[sym] = action
        while waiting and self.clock.now() < deadline:
            self.clock.sleep(1.0)
            try:
                self.reconcile(self.clock.now(), side_effects=False)
                now_held = self.bot_positions()
            except ConnectionError as exc:
                report["errors"].append(f"could not confirm the flatten: {exc}")
                break
            for sym in list(waiting):
                if now_held.get(sym, Decimal(0)) == 0:
                    report["positions"][sym]["action"] = "flattened"
                    self._tl("position_flattened_on_shutdown", sym, key=f"shutdown_flat:{sym}",
                             detail={"qty": report["positions"][sym]["qty"]}, event_time=self.clock.now().isoformat())
                    log_event(log, logging.WARNING, "position_flattened_on_shutdown",
                              f"{sym}: flattened on shutdown", symbol=sym)
                    waiting.pop(sym)
                else:
                    trade = self.j.trade(self.dkey, sym)
                    if trade is not None:
                        self._retry_flatten(trade, self.clock.now())
        for sym in waiting:
            report["positions"][sym]["action"] = ("flatten_pending" if waiting[sym] == "exit_working"
                                                  else "flatten_not_confirmed")

    # ------------------------------------------------------------ fatal error
    def fatal_protect(self, now: datetime) -> dict:
        """The process is about to exit on an unexpected exception. Block new entries, read Alpaca once, and for
        every ORB-IEX position either confirm a valid protective stop (or an exit already covering all of it) or
        start a safe flatten. Never raises; every step is reported. Nothing here places a new entry."""
        report: dict = {"entries_blocked": True, "reconciled": False, "entries": {}, "positions": {}, "errors": []}
        self.entries_blocked = FATAL_FLAG
        if self.day is None or self.schedule is None:
            report["errors"].append("no session started yet: recovery handles positions on the next start")
            return report
        try:
            self.reconcile(now, side_effects=False)            # ingest Alpaca's state; sends no orders
            report["reconciled"] = True
        except Exception as exc:
            report["errors"].append(f"reconcile failed: {type(exc).__name__}: {exc}")
            if isinstance(exc, ConnectionError):
                report["positions"] = None                      # Alpaca unreachable: one failure is enough
                return report
        try:                                                    # unfilled entries must not stay live while we are down
            report["entries"] = self._fatal_cancel_entries()
        except Exception as exc:
            report["entries"] = None
            report["errors"].append(f"entry cancel failed: {type(exc).__name__}: {exc}")
        try:
            held = self.bot_positions()
        except Exception as exc:
            report["errors"].append(f"position read failed: {type(exc).__name__}: {exc}")
            report["positions"] = None
            return report
        for sym, pos in sorted(held.items()):
            try:
                report["positions"][sym] = {"qty": str(pos), "action": self._fatal_protect_one(sym, pos, now)}
            except Exception as exc:
                report["positions"][sym] = {"qty": str(pos), "action": "error",
                                            "error": f"{type(exc).__name__}: {exc}"}
                report["errors"].append(f"{sym}: {type(exc).__name__}: {exc}")
                try:                                            # whatever failed, the position must not stay loose
                    t = self._trade_or_foreign(sym, pos)
                    t.pending_flatten = True
                    self.j.save_trade(t)
                except Exception:
                    pass
        return report

    def _fatal_cancel_entries(self) -> dict:
        """Cancel every ORB-IEX entry order that is still working (journal orders, which after the reconcile
        include any `orbiex-` order only Alpaca knew), then CONFIRM against Alpaca's own order state, a few rounds.
        Only orders with an ORB-IEX client_order_id are ever touched: manual/unrelated orders are not. A fill that
        wins the cancel race is captured by the reconcile, so the position step that follows protects or flattens it."""
        ids = self._entry_cancel_ids()
        out: dict[str, dict] = {s: {"order_id": oid, "action": "cancel_sent"} for s, oid in ids.items()}
        if not ids:
            return out
        log_event(log, logging.CRITICAL, "fatal_cancel_entries",
                  f"fatal error: cancelling {len(ids)} unfilled ORB-IEX entries {sorted(ids)}", symbols=sorted(ids))
        for rnd in range(FATAL_CANCEL_ROUNDS):
            for oid in ids.values():
                rec = self.j.order(oid)
                if rec is not None and rec.status not in TERMINAL:
                    self._cancel(rec, "Fatal error: unfilled entry cancelled")
            try:
                self.reconcile(self.clock.now(), side_effects=False)    # capture the states; sends no orders
            except Exception as exc:
                out["_reconcile_error"] = f"{type(exc).__name__}: {exc}"
                break
            if all((self.j.order(oid) is None or self.j.order(oid).status in TERMINAL) for oid in ids.values()):
                break
            if rnd < FATAL_CANCEL_ROUNDS - 1:
                self.clock.sleep(1.0)
        for sym, oid in ids.items():
            rec = self.j.order(oid)
            if rec is None:
                continue
            if rec.filled_qty > 0:
                out[sym].update(action="filled_during_cancel", filled=str(rec.filled_qty), status=rec.status)
            elif rec.status in TERMINAL:
                out[sym].update(action="canceled_confirmed", status=rec.status)
                self.stats["fatal_entries_canceled"] += 1
                trade = self.j.trade(self.dkey, sym)
                if trade is not None and trade.state in ("entry_open", "entry_cutoff_pending"):
                    trade.state, trade.note = "entry_fatal_canceled", "fatal_error"
                    self.j.save_trade(trade)
                    self.j.update_candidate(self.day, sym, "skipped", "fatal_error_entry_canceled")
                self._tl("skip", sym, key=f"fatal_cancel:{oid}", role="entry", order_id=oid, status="skipped",
                         detail={"reason": "fatal_error_entry_canceled", "broker_status": rec.status},
                         event_time=self.clock.now().isoformat())
            else:
                out[sym].update(action="cancel_unconfirmed", status=rec.status)
                log_event(log, logging.CRITICAL, "fatal_cancel_unconfirmed",
                          f"{sym}: entry still {rec.status} after the fatal-error cancel; recovery will cancel it",
                          symbol=sym, status=rec.status)
        return out

    def _fatal_protect_one(self, sym: str, pos: Decimal, now: datetime) -> str:
        pending = self.pending_exit_signed(sym)
        if pos + pending == 0:
            return "exit_working"                               # a working exit already covers all of it
        trade = self.j.trade(self.dkey, sym)
        if trade is not None and trade.state != "foreign" and not trade.closed and not trade.pending_flatten:
            stop = self.current_stop(trade)
            if (stop is not None and stop.status in PROTECTIVE_STOP_STATUSES and not stop.cancel_requested
                    and stop.side == ("sell" if pos > 0 else "buy")
                    and stop.qty - stop.filled_qty == abs(pos + pending)):
                return "protected"                              # a working stop for exactly the shares held
        trade = self._trade_or_foreign(sym, pos)
        trade.pending_flatten = True                            # persisted: the restarted process keeps flattening
        self.j.save_trade(trade)
        self.stats["position_guard_actions"] += 1
        log_event(log, logging.CRITICAL, "fatal_flatten",
                  f"{sym}: {pos} shares are not confirmed protected; flattening after the fatal error",
                  symbol=sym, qty=str(pos))
        self._flatten_last.pop(sym, None)
        self._retry_flatten(trade, now)                         # cancels blockers, else sends the market exit now
        return "flatten_initiated"

    # ------------------------------------------------------------------ ticks
    def tick(self, now: datetime) -> None:
        self._broker_snapshot_valid = False
        self.enforce_entry_cutoff(now)
        symbols = {t.symbol for t in self.j.trades(self.dkey)} | set(self.j.owned_qty())
        symbols.update(o.symbol for o in self.j.orders() if o.status not in TERMINAL)
        for symbol in sorted(symbols):
            self.reconcile_symbol(symbol)
        self._refresh()
        self._clear_overnight_flag()
        if not self.bot_positions() and not self.j.intents(True) and not any(o.status not in TERMINAL for o in self.j.orders()):
            self.gate.verified_flat()
            self.j.clear_breaker('shutdown_incomplete')
            self.j.clear_breaker('stuck_pending_order')
        st = self.j.day(self.day)
        if st["eod_status"] == "waiting":
            self._eod_maybe_liquidate(now)


    def guard_positions(self, now: datetime) -> None:
        """Every step: each ORB-IEX position must have a live protective stop, a working exit, or be
        on its way to being flattened. Catches what the event paths can miss (a lagging /positions
        read, a lost event, a failed cancel) so a filled position is never left with no protection."""
        if self.session_closing or now < self.schedule.open_sweep or not self.j.owned_qty():
            return
        try:
            held = self.bot_positions()
        except ConnectionError as exc:
            log_event(log, logging.WARNING, "position_read_failed", f"position guard: {exc}")
            return
        for sym, pos in sorted(held.items()):
            if pos + self.pending_exit_signed(sym) == 0:
                continue                                    # an exit is already working for all of it
            trade = self.j.trade(self.dkey, sym)
            if trade is not None and trade.pending_flatten:
                continue                                    # a flatten is already in progress
            if trade is None or trade.state == "foreign":
                trade = self._trade_or_foreign(sym, pos)
                if not trade.pending_flatten:
                    log_event(log, logging.ERROR, "orphan_position",
                              f"ORB-IEX position {sym} {pos} has no plan today; flattening", symbol=sym, qty=str(pos))
                    trade.pending_flatten = True
                    self.j.save_trade(trade)
                    self.stats["position_guard_actions"] += 1
            elif trade.closed:
                if not trade.pending_flatten:
                    log_event(log, logging.ERROR, "unprotected_position",
                              f"{sym}: trade is closed but {pos} shares are still held; flattening", symbol=sym,
                              qty=str(pos))
                    trade.pending_flatten = True
                    self.j.save_trade(trade)
                    self.stats["position_guard_actions"] += 1
            elif (stop := self.current_stop(trade)) is not None:
                if stop.qty != (pos + self.pending_exit_signed(sym)) * trade.direction:
                    self._stop_dirty.add(sym)               # the stop must follow Alpaca's position (flush_stops)
            elif self.current_stop(trade) is None and not self._stop_attempts.get(sym) and trade.entry_order_id:
                entry = self.j.order(trade.entry_order_id)
                if entry is not None and entry.filled_qty > 0 and not self.open_orders(sym, ("exit",)):
                    log_event(log, logging.ERROR, "unprotected_position",
                              f"{sym}: {pos} shares held with no live protective stop; placing it now",
                              symbol=sym, qty=str(pos))
                    self.stats["position_guard_actions"] += 1
                    self._place_protective_stop(trade, abs(pos))

    def _closing_guard(self, now: datetime) -> None:
        """After the EOD liquidation, until the close: keep cancelling any entry/stop that is still
        live and keep exiting whatever is still held. Nothing is attempted just once."""
        st = self.j.day(self.day)
        if not st["session_closing"] or st["eod_status"] not in ("waiting", "liquidated"):
            return
        for o in self.j.orders(self.dkey):
            if role_kind(o.role) in ("entry", "stop") and o.status not in TERMINAL:
                self.stats["eod_cancel_retries"] += 1
                self._cancel(o, "End of session (retry)")
        if st["eod_status"] != "liquidated":
            return                                  # the liquidation itself waits for the cancels (or its timeout)
        for sym, pos in sorted(self.bot_positions().items()):
            if self.open_orders(sym, ("exit",)):
                continue
            last = self._flatten_last.get(sym)
            if last and (now - last).total_seconds() < FLATTEN_RETRY_SECONDS:
                continue
            self._flatten_last[sym] = now
            self._trade_or_foreign(sym, pos)
            log_event(log, logging.ERROR, "eod_still_held",
                      f"{sym}: {pos} still held after the EOD exit; exiting again", symbol=sym, qty=str(pos))
            self.market_exit(sym, "eod", None, "Regular-session exit (retry)")

    def _clear_overnight_flag(self) -> None:
        if self.entries_blocked == OVERNIGHT_FLAG:
            try:
                if not self.bot_positions():
                    self.entries_blocked = None
                    log_event(log, logging.WARNING, "overnight_cleared",
                              "the overnight ORB-IEX positions are flat; new entries allowed again")
            except ConnectionError:
                pass

    # ------------------------------------------------------------------ after the close
    def _after_hours_end(self) -> datetime:
        from zoneinfo import ZoneInfo
        close = self.schedule.session.close.astimezone(ZoneInfo("America/New_York"))
        return close.replace(hour=AFTER_HOURS_END_HOUR_ET, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def post_close_step(self, now: datetime) -> bool:
        """Runs after the close until ORB-IEX is verifiably flat. Returns True when it is done:
        flat, or (fail-safe) every retry up to the end of extended hours has been used."""
        if self._pc_started is None:
            self._pc_started = now
        try:
            self.reconcile(now, side_effects=True)           # pull in any late fill or exit from Alpaca
            positions = self.bot_positions()
        except ConnectionError as exc:
            log_event(log, logging.WARNING, "post_close_read_failed", f"post-close check: {exc}")
            return now >= self._after_hours_end()
        if not positions:
            live = [o for o in self.j.orders() if o.status not in TERMINAL]
            for order in live:
                self._cancel(order, 'after-close orphan order cleanup')
            return not live and not self.j.intents(True)
        if now >= self._after_hours_end():
            self._fail_safe_overnight(positions)
            return True
        if (now - self.schedule.session.close).total_seconds() < POSTCLOSE_WAIT_SECONDS:
            return False                                      # in-flight exits may still fill
        for sym, pos in sorted(positions.items()):
            self._trade_or_foreign(sym, pos)
            last = self._pc_last.get(sym)
            waited = (now - last).total_seconds() if last else None
            stuck = [o for o in self.open_orders(sym, ("entry", "stop", "exit"))
                     if role_kind(o.role) != "exit" or o.type == "market"            # cannot fill until tomorrow
                     or (waited is not None and waited >= POSTCLOSE_RETRY_SECONDS)]  # our limit did not fill: widen it
            if stuck:
                for o in stuck:
                    self._cancel(o, "After the close")
                continue
            if self.open_orders(sym, ("exit",)):
                continue                                      # a fresh after-hours exit is working
            if last and waited < POSTCLOSE_RETRY_SECONDS:
                continue
            self._pc_last[sym] = now
            attempt = self._pc_attempts.get(sym, 0)
            self._pc_attempts[sym] = attempt + 1
            self._limit_exit(sym, pos, attempt, now)
        return False

    def _limit_exit(self, sym: str, pos: Decimal, attempt: int, now: datetime) -> OrderRec | None:
        """Extended-hours marketable limit order that closes an ORB-IEX leftover after the close.
        The price is the near side of the IEX quote (or the last fill) pushed 1%, 2%, 3%, 5% through."""
        buy = pos < 0
        ref = None
        if self.quote_source is not None:
            try:
                q = self.quote_source([sym], now).get(sym)
                if q is not None:
                    ref = (q.ask or q.bid) if buy else (q.bid or q.ask)
            except Exception:
                ref = None
        if ref is None:
            ref = self.j.last_fill_price(sym)
        if ref is None:
            log_event(log, logging.ERROR, "postclose_no_price",
                      f"{sym}: no price to build an after-hours exit for {pos}", symbol=sym)
            return None
        slack = POSTCLOSE_BUFFERS[min(attempt, len(POSTCLOSE_BUFFERS) - 1)]
        limit = _px(Decimal(ref) * (1 + slack if buy else 1 - slack))
        req = OrderRequest(sym, "buy" if buy else "sell", abs(pos), "limit",
                           client_order_id(self.day, sym, "postclose", self._seq(sym, "postclose")),
                           limit_price=limit, extended_hours=True)
        self.stats["postclose_exits"] += 1
        log_event(log, logging.ERROR, "postclose_exit",
                  f"{sym}: {pos} still held after the close; after-hours {req.side} limit {limit} (attempt {attempt + 1})",
                  symbol=sym, qty=str(pos), limit_price=str(limit), attempt=attempt + 1)
        return self._submit(req, "postclose", "After-close flatten")

    def _fail_safe_overnight(self, positions: dict[str, Decimal]) -> None:
        held = {s: str(q) for s, q in positions.items()}
        log_event(log, logging.CRITICAL, "overnight_unflattened",
                  f"FAIL SAFE: ORB-IEX positions {held} could not be closed before extended hours ended. "
                  "New entries are blocked until they are flat; the open sweep keeps trying.",
                  positions=held)
        self.entries_blocked = OVERNIGHT_FLAG
        for symbol, qty in positions.items():
            self.j.set_breaker('overnight_position', symbol, f'unflattened broker quantity {qty}')
        self.j.set_meta("overnight_positions", json.dumps({"day": self.dkey, "positions": held}))
        self._tl("overnight_unflattened", None, detail={"positions": held})

    def _retry_flatten(self, trade: TradeRec, now: datetime) -> None:
        """Flatten the symbol until it is flat. There is no attempt cap: a position must never be
        abandoned, so a rejected exit is sent again every FLATTEN_RETRY_SECONDS (until the close)."""
        sym = trade.symbol
        pos = self.bot_position(sym)
        if pos == 0:
            trade.pending_flatten = False
            self.j.save_trade(trade)
            return
        last = self._flatten_last.get(sym)
        if last and (now - last).total_seconds() < FLATTEN_RETRY_SECONDS:
            return
        self._flatten_last[sym] = now
        blocking = self.open_orders(sym, ("entry", "stop"))
        if blocking:
            for o in blocking:
                self._cancel(o, "flatten")          # re-sent every CANCEL_RETRY_SECONDS until terminal
            return
        if self.open_orders(sym, ("exit",)):
            return
        trade.flatten_attempts += 1
        self.j.save_trade(trade)
        if trade.flatten_attempts % 5 == 0:
            log_event(log, logging.ERROR, "flatten_not_working",
                      f"{sym}: {trade.flatten_attempts} flatten attempts so far, still holding {pos}; keeps trying",
                      symbol=sym, position=str(pos), attempts=trade.flatten_attempts)
        self.market_exit(sym, "flatten", None, "Flatten retry")

    # -------------------------------------------------------------------- EOD
    def begin_eod(self, now: datetime) -> None:
        """close - 5 min: session closing, cancel entries and stops, then wait."""
        self.set_session_closing(True)
        self.j.set_eod(self.day, "canceling", now.isoformat())
        log_event(log, logging.INFO, "eod", "Regular-session exit: session closing; canceling entries and stops")
        self._broker_snapshot_valid = False
        self._refresh()
        for symbol in {o.symbol for o in self.j.orders() if o.status not in TERMINAL} | set(self.j.owned_qty()):
            self.j.save_risk(symbol, exit_role='eod', reason='Regular-session exit')
            self.reconcile_symbol(symbol)
        self.j.set_eod(self.day, "waiting")
        self._eod_maybe_liquidate(now)


    def _eod_waiting_on(self) -> list[OrderRec]:
        return [o for o in self.j.orders(self.dkey) if o.cancel_requested and o.status not in TERMINAL]


    def _eod_maybe_liquidate(self, now: datetime) -> None:
        st = self.j.day(self.day)
        started = datetime.fromisoformat(st["eod_started_at"])
        waiting = self._eod_waiting_on()
        timed_out = (now - started).total_seconds() >= self.settings.eod_cancel_timeout_seconds
        if waiting and not timed_out:
            return
        if waiting:
            log_event(log, logging.ERROR, "eod_cancel_timeout",
                      f"cancel timeout: still open {[(o.symbol, o.role, o.status) for o in waiting]}; recovery continues without racing exits",
                      orders=[o.order_id for o in waiting])
            self.j.set_breaker('stuck_pending_order', detail='EOD cancellation timeout')
            return
        for sym, pos in sorted(self.bot_positions().items()):
            self._trade_or_foreign(sym, pos)
            self.market_exit(sym, "eod", None, "Regular-session exit")
        if self.bot_positions() or any(o.status not in TERMINAL for o in self.j.orders()):
            return
        self.j.clear_breaker('stuck_pending_order')
        self.j.set_eod(self.day, "liquidated")
        self.j.mark_done(self.day, "eod")


    def sweep(self, role: str, reason: str) -> None:
        """Main.cs SweepLeftovers: close ORB-IEX leftovers unless a market exit is pending."""
        for sym, pos in sorted(self.bot_positions().items()):
            self._trade_or_foreign(sym, pos)
            if self.open_orders(sym, ("exit",)):
                log_event(log, logging.ERROR, "sweep_skip", f"{reason}: {sym} still has a pending market exit",
                          symbol=sym)
                continue
            log_event(log, logging.ERROR, "sweep", f"{reason}: closing leftover {sym} qty={pos}",
                      symbol=sym, qty=str(pos))
            self.market_exit(sym, role, pos, reason)


    def post_close(self, now: datetime) -> dict:
        positions = self.bot_positions(persist=True)
        leftovers = {s: str(q) for s, q in positions.items()}
        unrelated = {s: str(q) for s, q in self.unrelated_positions().items()}
        if unrelated:
            log_event(log, logging.INFO, "unrelated_positions",
                      f"positions not opened by ORB-IEX (left untouched): {unrelated}", positions=unrelated)
        open_ours = [o for o in self.broker.list_orders("open")
                     if (parse_client_order_id(o.client_order_id) or (None,))[0] is not None]
        if leftovers:
            log_event(log, logging.ERROR, "overnight_positions",
                      f"OVERNIGHT POSITIONS after close: {leftovers} (open sweep closes them next session)",
                      positions=leftovers)
        else:
            log_event(log, logging.INFO, "flat_check", "zero overnight positions confirmed")
        if open_ours:
            log_event(log, logging.WARNING, "open_after_close",
                      f"orders still open after close: {[o.client_order_id for o in open_ours]}")
        summary = self.order_summary()
        summary["overnight_positions"] = leftovers
        summary["unrelated_positions_untouched"] = unrelated
        summary["open_orders_after_close"] = [o.client_order_id for o in open_ours]
        self.j.set_summary(self.day, summary)
        return summary

    # ----------------------------------------------------------------- summary
    def order_summary(self) -> dict:
        orders = self.j.orders(self.dkey)
        fills = self.j.fills(self.dkey)

        def count(pred):
            return sum(1 for o in orders if pred(o))

        def count_trades(state):
            return sum(1 for t in self.j.trades(self.dkey) if t.state == state)

        entries = [o for o in orders if o.role == "entry"]
        stops = [o for o in orders if o.role == "stop"]
        exits = [o for o in orders if role_kind(o.role) == "exit"]
        by_exit = {}
        for o in exits:
            by_exit[o.role] = by_exit.get(o.role, 0) + 1
        per_sym_entry = {}
        for o in entries:
            per_sym_entry[o.symbol] = per_sym_entry.get(o.symbol, 0) + 1
        commission = sum((Decimal(f["commission"]) for f in fills), Decimal(0))
        slippage = sum((Decimal(f["slippage"]) for f in fills), Decimal(0))
        # Cash flow of today's ORB-IEX fills = realized P&L when the day starts
        # and ends flat. Paper-comparable = commission only ($0.0035/sh, the
        # paper's cost model); realistic = commission + modeled slippage.
        gross = -sum((Decimal(f["qty"]) * Decimal(f["price"]) * (1 if f["side"] == "buy" else -1)
                      for f in fills if f["price"] is not None), Decimal(0))
        s = {
            "day": self.dkey, "total_orders": len(orders),
            "entry_submitted": len(entries), "entry_filled": count(lambda o: o.role == "entry" and o.status == "filled"),
            "entry_partial": count(lambda o: o.role == "entry" and Decimal(0) < o.filled_qty < o.qty),
            "entry_canceled": count(lambda o: o.role == "entry" and o.status in ("canceled", "expired")),
            "entry_rejected": count(lambda o: o.role == "entry" and o.status == "rejected"),
            "stop_created": len(stops), "stop_filled": count(lambda o: o.role == "stop" and o.status == "filled"),
            "stop_canceled": count(lambda o: o.role == "stop" and o.status in ("canceled", "expired")),
            "stop_replaced": count(lambda o: o.role == "stop" and o.status == "replaced"),
            "market_exits": len(exits), "exits_by_role": by_exit,
            "partial_fill_events": self.stats["partial_fill_events"],
            "stop_qty_updates": self.stats["stop_qty_updates"],
            "entries_crossed_skipped": count_trades("entry_crossed"),
            "entries_rejected": count_trades("entry_rejected"),
            "entry_crossed_422": self.stats["entry_crossed_422"],
            "entry_unconfirmed": self.stats["entry_unconfirmed"],
            "stop_replace_failed": self.stats["stop_replace_failed"],
            "stop_replace_deferred": self.stats["stop_replace_deferred"],
            "stop_replace_state_422": self.stats["stop_replace_state_422"],
            "duplicate_stops_canceled": self.stats["duplicate_stops_canceled"],
            "protective_stop_crossed": self.stats["protective_stop_crossed"],
            "stale_snapshots_ignored": self.stats["stale_snapshots_ignored"],
            "spread_too_wide_skipped": self.stats["spread_too_wide_skipped"],
            "stale_quote_skipped": self.stats["stale_quote_skipped"],
            "quote_unavailable_skipped": self.stats["quote_unavailable_skipped"],
            "position_guard_actions": self.stats["position_guard_actions"],
            "eod_cancel_retries": self.stats["eod_cancel_retries"],
            "postclose_exits": self.stats["postclose_exits"],
            "duplicate_entry_symbols": [s for s, n in per_sym_entry.items() if n > 1],
            "fills": len(fills), "modeled_commission": str(commission), "modeled_slippage": str(slippage),
            "gross_pnl": str(gross.quantize(Decimal("0.01"))),
            # day cash flow, not trade P&L: a carry-in exit or an overnight entry makes it differ from the
            # reconstructed trades (orb_iex.reconcile is the trade-level truth)
            "gross_pnl_basis": "day_fill_cash_flow",
            "net_pnl_paper_costs": str((gross - commission).quantize(Decimal("0.01"))),
            "net_pnl_realistic": str((gross - commission - slippage).quantize(Decimal("0.01"))),
            "duplicates_ignored": self.stats["duplicates_ignored"],
            "out_of_order_ignored": self.stats["out_of_order_ignored"],
        }
        log_event(log, logging.INFO, "order_summary",
                  "ORDER SUMMARY " + " ".join(f"{k}={v}" for k, v in s.items()), **s)
        return s

    # ---------------------------------------------------------------- recovery
    def reconcile(self, now: datetime, side_effects: bool = True) -> None:
        """REST snapshot of today's orders. With side effects = normal event
        processing (used by the poll and after a stream reconnect)."""
        self._broker_snapshot_valid = False
        self._refresh()
        if side_effects:
            self.tick(now)


    def recover(self, now: datetime) -> None:
        """Startup: ingest Alpaca state silently, then repair every trade."""
        log_event(log, logging.INFO, "recovery", f"recovery start for {self.dkey}")
        self.reconcile(now, side_effects=False)
        phase = self.schedule.phase(now)
        positions = self.bot_positions(persist=phase in ("pre_open", "after_close"))
        for symbol in positions:
            previous_entries = [o for o in self.j.orders(symbol=symbol) if o.role == 'entry'
                                and o.filled_qty and o.day < self.dkey]
            if previous_entries:
                self.entries_blocked = OVERNIGHT_FLAG
                self.j.set_breaker('overnight_position', symbol, 'carried bot fill evidence')
                self.j.save_risk(symbol, exit_role='flatten', reason='overnight recovery')
        st = self.j.day(self.day)
        if st["eod_status"] in ("canceling", "waiting"):
            self.j.set_eod(self.day, "waiting")
        planned = []
        for trade in self.j.trades(self.dkey):
            if trade.state == "foreign":
                continue
            if not trade.closed:
                self._tl('recovery', trade.symbol, key=f'recovery:{getattr(self.tlog, "run_id", "-")}:{trade.symbol}',
                         detail={'phase': phase, 'state': trade.state}, event_time=now.isoformat())
            if trade.entry_order_id is None and trade.state == "planned":
                self._recover_planned(trade, now, allow_submit=False)
                planned.append(trade.symbol)
                continue
            self.repair_trade(trade, positions, now)
        known = {t.symbol for t in self.j.trades(self.dkey) if t.state != "foreign"}
        for sym, pos in positions.items():
            if sym in known:
                continue
            # an ORB-IEX position (from its own fills) with no plan today, e.g. an overnight leftover
            if phase in ("opening_range", "active") and self.market_open(now) and now >= self.schedule.open_sweep:
                log_event(log, logging.ERROR, "orphan_position",
                          f"ORB-IEX position {sym} {pos} has no plan today; flattening", symbol=sym, qty=str(pos))
                t = self._trade_or_foreign(sym, pos)
                t.pending_flatten = True
                self.j.save_trade(t)
            else:
                log_event(log, logging.WARNING, "orphan_position",
                          f"ORB-IEX position {sym} {pos} has no plan today; the next sweep closes it",
                          symbol=sym, qty=str(pos))
        self.tick(now)  # verify every symbol before enabling a missing normal entry
        for symbol in planned:
            trade = self.j.trade(self.dkey, symbol)
            if trade.state == 'planned' and not self.j.breakers() and not self.gate.journal_failed:
                self._recover_planned(trade, now)
        unrelated = self.unrelated_positions()
        if unrelated:
            log_event(log, logging.INFO, "unrelated_positions",
                      f"positions not opened by ORB-IEX (never touched): {({s: str(q) for s, q in unrelated.items()})}")
        log_event(log, logging.INFO, "recovery", f"recovery done: phase={phase} positions={len(positions)} "
                  f"trades={len(self.j.trades(self.dkey))}")


    def _recover_planned(self, trade: TradeRec, now: datetime, allow_submit: bool = True) -> None:
        existing = self.broker.get_order_by_client_id(trade.entry_client_id)
        if existing:
            rec = self.j.order(existing.order_id) or self._record(existing, "entry")
            self._apply(existing, side_effects=False)
            trade.entry_order_id = rec.order_id
            trade.state = "entry_open"
            self.j.save_trade(trade)
            self.repair_trade(trade, self.bot_positions(), now)
        elif now < self.schedule.scan_deadline and not self.session_closing:
            if allow_submit:
                self.submit_entry(trade)
        else:
            trade.state = "not_submitted"
            trade.note = "restart after scan window"
            self.j.save_trade(trade)
            self.j.update_candidate(self.day, trade.symbol, "skipped", "restart_after_scan_window")


    def repair_trade(self, trade: TradeRec, positions: dict[str, Decimal], now: datetime) -> None:
        stops = self.stop_chain(trade)
        if stops and stops[-1].status in LOST and not stops[-1].cancel_requested:
            self.stop_lost(trade, 'stop lost while offline')
        self.reconcile_symbol(trade.symbol)


    def _apply_snapshot(self, u: OrderUpdate) -> tuple[OrderRec | None, Decimal, str | None]:
        """Merge one snapshot into the journal. Returns (order, fill delta, new status or None)."""
        rec = self.j.order(u.order_id)
        if rec is None and u.client_order_id:
            rec = self.j.order_by_client_id(u.client_order_id)
            if rec is not None and rec.order_id != u.order_id:
                rec = None
        if rec is None:
            parsed = parse_client_order_id(u.client_order_id)
            if parsed is None:
                return None, Decimal(0), None
            pday, psym, prole = parsed
            role = "stop" if prole == "stop" else prole
            rec = self._record(u, role, day=pday.isoformat())
            rec.status = "new"
            log_event(log, logging.WARNING, "order_adopted",
                      f"adopted {u.client_order_id} ({u.order_id}) found at Alpaca", order_id=u.order_id,
                      client_order_id=u.client_order_id)
        delta = Decimal(0)
        new_status = None
        stamp = datetime.fromisoformat(u.timestamp.replace('Z', '+00:00'))
        previous = datetime.fromisoformat(rec.broker_updated_at.replace('Z', '+00:00')) if rec.broker_updated_at else None
        stale = previous is not None and stamp < previous
        if stale:
            self.stats['stale_snapshots_ignored'] += 1
        if u.filled_qty > rec.filled_qty:
            delta = u.filled_qty - rec.filled_qty
            old_value = (rec.filled_avg_price or Decimal(0)) * rec.filled_qty
            new_value = (u.filled_avg_price or Decimal(0)) * u.filled_qty
            price = (new_value - old_value) / delta if u.filled_avg_price is not None else None
            rec.filled_qty = u.filled_qty
            rec.filled_avg_price = u.filled_avg_price
            commission = delta * self.settings.commission_per_share
            slippage = delta * self.settings.slippage_per_share
            if self.j.add_fill(rec, u.filled_qty, delta, price, u.timestamp, commission, slippage):
                if u.filled_qty < rec.qty:
                    self.stats["partial_fill_events"] += 1
                log_event(log, logging.INFO, "fill",
                          f"FILL {rec.role} {rec.side} {delta} {rec.symbol} @ {price} (cum {u.filled_qty}/{rec.qty}) "
                          f"modeled commission={commission} slippage={slippage}",
                          symbol=rec.symbol, role=rec.role, side=rec.side, qty=str(delta), price=price,
                          cum_qty=str(u.filled_qty), order_qty=str(rec.qty), order_id=rec.order_id,
                          modeled_commission=commission, modeled_slippage=slippage)
        elif u.filled_qty < rec.filled_qty:
            self.stats["out_of_order_ignored"] += 1
        if u.status and u.status != rec.status:
            old_rank, new_rank = status_rank(rec.status), status_rank(u.status)
            accept = not stale and (new_rank > old_rank or (new_rank == old_rank and old_rank < 3))
            # A fresh confirmation can leave pending_cancel/replace, unlike a stale REST reply.
            if not stale and rec.status in ('pending_cancel', 'pending_replace') and u.status == 'new':
                accept = True
            if old_rank == 3 and u.status == "filled" and delta > 0:
                accept = True
            if accept:
                new_status = u.status
                rec.status = u.status
            else:
                self.stats["out_of_order_ignored"] += 1
        elif delta == 0 and u.source in ("stream", "sim"):
            self.stats["duplicates_ignored"] += 1
        changed = bool(delta or new_status)
        if u.replaced_by and not rec.replaced_by:
            rec.replaced_by = u.replaced_by
            changed = True
        if u.replaces and not rec.replaces:
            rec.replaces = u.replaces
            changed = True
        if not stale and u.qty and rec.status not in TERMINAL and u.qty != rec.qty:
            rec.qty = u.qty
            changed = True
        if not stale and rec.broker_updated_at != u.timestamp:
            rec.broker_updated_at = u.timestamp
            changed = True
        if changed:
            self.j.save_order(rec)
        if new_status or delta:
            log_event(log, logging.INFO, "order_event",
                      f"EVENT {rec.symbol} {rec.role} {rec.order_id} {u.event or ''} status={rec.status} "
                      f"filled={rec.filled_qty}/{rec.qty} src={u.source}",
                      symbol=rec.symbol, role=rec.role, order_id=rec.order_id, status=rec.status,
                      stream_event=u.event, filled_qty=str(rec.filled_qty), source=u.source)
        return rec, delta, new_status


    def _protection_quote(self, symbol: str, side: str, stop_price: Decimal, *, broker_validation: bool = False) -> bool:
        self.j.save_diagnostic(self.day, f'protection_quote:{symbol}',
                               {'at': self.clock.now().isoformat(), 'quote': None,
                                'side': side, 'stop_price': str(stop_price), 'broker_validation': broker_validation})
        q = self.broker.latest_quote(symbol)
        stamp = datetime.fromisoformat(q['t'].replace('Z', '+00:00'))
        age = (self.clock.now() - stamp).total_seconds()
        bid, ask = Decimal(str(q['bp'])), Decimal(str(q['ap']))
        log_event(log, logging.INFO, 'protection_quote', f'{symbol}: validating {side} stop {stop_price}',
                  symbol=symbol, side=side, stop_price=str(stop_price), quote=q, age_seconds=age)
        self.j.save_diagnostic(self.day, f'protection_quote:{symbol}',
                               {'at': self.clock.now().isoformat(), 'quote': q, 'age_seconds': age,
                                'side': side, 'stop_price': str(stop_price), 'broker_validation': broker_validation})
        if not (-2 <= age <= self.settings.max_quote_age_seconds) or not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
            self.j.set_breaker('stale_market_data', symbol, f'unusable IEX quote {q}')
            raise ConnectionError('latest IEX quote is stale or invalid')
        self.j.clear_breaker('stale_market_data', symbol)
        spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
        if spread_pct > self.settings.max_quote_spread_pct:
            if not broker_validation:
                raise ConnectionError('wide IEX quote cannot provide synthetic protection during entry cancel')
            # USO 2026-10-26: a junk IEX bid (161.16 against ask 166.38) read as a breached 165.98 stop and flattened a
            # healthy position. A quote this wide says nothing about where the market is, so it cannot prove the stop
            # is crossed. The broker validates the stop instead: a genuinely crossed stop is rejected and the
            # stop-failed path flattens, exactly as before.
            log_event(log, logging.WARNING, 'protection_quote_wide_spread',
                      f'{symbol}: spread {spread_pct:.2f}% > {self.settings.max_quote_spread_pct}%; '
                      f'quote cannot judge the {side} stop {stop_price}, deferring to the broker',
                      symbol=symbol, bid=str(bid), ask=str(ask), spread_pct=str(spread_pct), stop_price=str(stop_price))
            return True
        return stop_price <= bid - ENTRY_MIN_CLEARANCE if side == 'sell' else stop_price >= ask + ENTRY_MIN_CLEARANCE


    def _refresh(self) -> None:
        """Ingest a complete broker snapshot before deriving any desired order."""
        if self._snapshotting or self._broker_snapshot_valid:
            return
        self._snapshotting = True
        try:
            after = (self.schedule.session.open - timedelta(days=1)).astimezone(timezone.utc).isoformat()
            snaps = {u.order_id: u for u in self.broker.list_orders('all', after=after)}
            for u in self.broker.list_orders('open'):
                snaps[u.order_id] = u
            for rec in self.j.orders():
                if rec.status not in TERMINAL and rec.order_id not in snaps:
                    found = self.broker.get_order_by_client_id(rec.client_order_id)
                    if found is None:
                        raise ConnectionError(f'known order missing: {rec.client_order_id}')
                    snaps[found.order_id] = found
            for u in snaps.values():
                if parse_client_order_id(u.client_order_id):
                    self._apply(u, side_effects=False)
            self.gate.resolve(self._apply)
            self._positions_snapshot = self.broker.list_positions()
            self._owned_snapshot = self.j.owned_qty()
            self._fallback_orders = {o.order_id: (o.symbol, o.side, o.filled_qty, o.client_order_id) for o in self.j.orders()}
            self._broker_snapshot_valid = True
            self.j.clear_breaker('reconciliation_unhealthy')
        except ConnectionError as exc:
            self.j.set_breaker('reconciliation_unhealthy', detail=str(exc))
            raise
        finally:
            self._snapshotting = False


    def emergency_shutdown(self) -> bool:
        """SQLite-independent recovery using the last durable ownership snapshot.

        Never infer ownership from the account's position alone. Refuse to exit
        when order history cannot establish the remaining bot share count.
        """
        self._shutting_down = True
        self.gate.journal_failed = True
        with self.gate.fallback.open('a') as f:
            f.write(json.dumps({'breaker': 'journal_failure', 'ts': self.clock.now().isoformat()}) + '\n')
            f.flush()
            os.fsync(f.fileno())
        deadline = self.clock.now() + timedelta(seconds=self.settings.shutdown_timeout_seconds)
        exit_seen = {}
        while self.clock.now() < deadline:
            try:
                snapshots = {u.order_id: u for u in self.broker.list_orders('all')}
                for oid, (_, _, _, cid) in self._fallback_orders.items():
                    if oid not in snapshots:
                        found = self.broker.get_order_by_client_id(cid)
                        if found is None:
                            raise ConnectionError('incomplete emergency ownership history')
                        snapshots[found.order_id] = found
                try:
                    intents = {i['intent_id']: i for i in self.j.intents(True)}
                except sqlite3.Error:
                    intents = {}
                for line in self.gate.fallback.read_text().splitlines():
                    record = json.loads(line)
                    if 'intent_id' in record:
                        intents[record['intent_id']] = record
                for intent in intents.values():
                    if intent['mutation'] == 'submit' and intent['state'] != 'TERMINAL':
                        found = self.broker.get_order_by_client_id(intent['client_order_id'])
                        if found is None:
                            raise ConnectionError('ambiguous emergency submit is not yet observable')
                        snapshots[found.order_id] = found
                owned = dict(self._owned_snapshot)
                for u in snapshots.values():
                    parsed = parse_client_order_id(u.client_order_id)
                    if not parsed:
                        continue
                    old = self._fallback_orders.get(u.order_id, (u.symbol, u.side, Decimal(0)))[2]
                    if u.filled_qty < old:
                        raise ConnectionError('stale emergency broker snapshot')
                    owned[u.symbol] = owned.get(u.symbol, Decimal(0)) + (u.filled_qty - old) * _side_sign(u.side)
                live = [u for u in snapshots.values() if parse_client_order_id(u.client_order_id)
                        and u.status not in TERMINAL]
                if live:
                    mutated = set()
                    for u in live:
                        if u.symbol in mutated:
                            continue
                        seen = exit_seen.setdefault(u.order_id, self.clock.now())
                        if role_kind(parse_client_order_id(u.client_order_id)[2]) != 'exit' or (self.clock.now() - seen).total_seconds() >= self.settings.exit_timeout_seconds:
                            rec = OrderRec(u.order_id, u.client_order_id, str(parse_client_order_id(u.client_order_id)[0]),
                                           u.symbol, parse_client_order_id(u.client_order_id)[2], u.side, u.type,
                                           u.qty, u.stop_price, u.status)
                            self.gate.run(u.symbol, rec.role, 'cancel', order=rec, emergency=True)
                            mutated.add(u.symbol)
                    self.clock.sleep(1)
                    continue
                actual = self.broker.list_positions()
                if any(q and not actual.get(s) for s, q in owned.items()):
                    raise ConnectionError('broker position and durable owned fills disagree')
                if any(q and actual.get(s) and (q > 0) != (actual[s] > 0) for s,q in owned.items()):
                    return False  # ownership mismatch cannot be reported as a clean shutdown
                positions = {s: self._clip(q, actual.get(s, Decimal(0))) for s, q in owned.items()}
                positions = {s: q for s, q in positions.items() if q}
                if not positions:
                    return True
                if self.day is None or not self.market_open(self.clock.now()):
                    return False
                for symbol, qty in positions.items():
                    seq = 1 + sum(1 for u in snapshots.values() if u.symbol == symbol and
                                  (parse_client_order_id(u.client_order_id) or (None, None, None))[2] == 'flatten')
                    req = OrderRequest(symbol, 'sell' if qty > 0 else 'buy', abs(qty), 'market',
                                       client_order_id(self.day, symbol, 'flatten', seq))
                    self.gate.run(symbol, 'flatten', 'submit', req=req, emergency=True)
            except (ConnectionError, OrderRejected, SafetyError, ValueError):
                pass
            self.clock.sleep(1)
        return False


    def reconcile_symbol(self, symbol: str) -> OrderRec | None:
        """The sole engine mutation path: cancel-confirm, then protect or exit."""
        if self._reconciling:
            return None
        self._reconciling = True
        try:
            # Three passes allow an immediately confirmed cancel to converge without
            # waiting for a stream event. Pending/ambiguous cancels return to the loop.
            resynced = False
            for _ in range(3):
                self._refresh()
                now = self.clock.now()
                t = self.j.trade(self.dkey, symbol)
                orders = [o for o in self.j.orders(symbol=symbol) if o.status not in TERMINAL]
                entries = [o for o in orders if role_kind(o.role) == 'entry']
                stops = [o for o in orders if role_kind(o.role) == 'stop']
                exits = [o for o in orders if role_kind(o.role) == 'exit']
                owned = self.j.owned_qty().get(symbol, Decimal(0))
                actual = self.positions().get(symbol, Decimal(0))
                if owned and actual and (owned > 0) != (actual > 0):
                    self.j.set_breaker('sign_flip', symbol, f'owned={owned}, broker={actual}')
                    return None  # cannot attribute opposite manual shares safely
                pos = self._clip(owned, actual)
                if owned and not actual and not resynced:
                    # The position read can be newer than the order read (USO, RPRX, CLF: a fill landed between the two
                    # REST calls). Read both again before calling it a disagreement; cancelling a filled order only churns.
                    resynced, self._broker_snapshot_valid = True, False
                    continue
                if owned and not actual:
                    self.j.set_breaker('reconciliation_unhealthy', symbol, 'owned fills disagree with broker position; no quantity guessed')
                    self.j.save_risk(symbol, unprotected_since=self.j.risk(symbol)['unprotected_since'] or now.isoformat())
                    for order in entries + stops + exits:
                        self._cancel(order, 'broker quantity mismatch: remove orders until reconciled')
                    return None
                risk = self.j.risk(symbol)
                if t and any(s.filled_qty > 0 for s in self.stop_chain(t)) and not t.closed:
                    t.closed, t.state, t.pending_flatten = True, 'stopped_out', True
                    self.j.save_trade(t)
                    self.j.save_risk(symbol, exit_role='flatten', reason='stop filled during reconcile')
                    risk = self.j.risk(symbol)
                if t and not t.closed:
                    history = self.stop_chain(t)
                    if history and history[-1].status in LOST and not history[-1].cancel_requested:
                        self.stop_lost(t, f'stop {history[-1].status} during broker reconciliation')
                        t = self.j.trade(self.dkey, symbol)
                        risk = self.j.risk(symbol)
                if t and t.state != 'foreign' and pos * t.direction < 0:
                    self.j.set_breaker('sign_flip', symbol, f'bot position {pos} opposite plan')
                    self.j.save_risk(symbol, exit_role='flatten', reason='confirmed bot sign flip')
                    risk = self.j.risk(symbol)
                if t and t.entry_order_id is None:
                    entry = self.j.order_by_client_id(t.entry_client_id)
                    if entry:
                        t.entry_order_id = entry.order_id
                        self.j.save_trade(t)
                exiting = bool(risk['exit_role'] or (t and (t.closed or t.pending_flatten)) or
                               self.session_closing or self._shutting_down or t is None)
                if exiting and t and pos and not t.pending_flatten:
                    t.pending_flatten = True
                    self.j.save_trade(t)
                if pos and risk['unprotected_since']:
                    age = (now - datetime.fromisoformat(risk['unprotected_since'])).total_seconds()
                    if age >= self.settings.protection_timeout_seconds:
                        self.j.set_breaker('unprotected_position', symbol, 'hard protection deadline reached')
                        self.j.save_risk(symbol, exit_role=risk['exit_role'] or 'flatten', reason='protection deadline')
                        risk = self.j.risk(symbol)
                        exiting = True
                cutoff_entries = [o for o in entries if o.day != self.dkey or now >= self.entry_cutoff()]
                blockers = entries if (pos or exiting) else cutoff_entries
                if blockers:
                    if pos:
                        self.j.save_risk(symbol, unprotected_since=risk['unprotected_since'] or now.isoformat())
                        self.j.set_breaker('unprotected_position', symbol, 'waiting for entry cancel confirmation')
                        if t and not exiting:
                            reason = 'synthetic_stop_breached_during_entry_cancel'
                            error = None
                            try:
                                safe = self._protection_quote(symbol, 'sell' if pos > 0 else 'buy', self.fill_stop_price(t))
                            except (ConnectionError, KeyError, ValueError, ArithmeticError) as exc:
                                self.j.set_breaker('stale_market_data', symbol, str(exc))
                                reason = 'protection_quote_unusable_during_entry_cancel'
                                error = f'{type(exc).__name__}: {exc}'
                                safe = False
                            if not safe:
                                evidence = self.j.diagnostic(self.day, f'protection_quote:{symbol}') or {}
                                self.j.save_diagnostic(self.day, f'protection_exit:{symbol}',
                                    {**evidence, 'at': now.isoformat(), 'reason': reason, 'error': error,
                                     'position': str(pos), 'entry_cancel_pending': True})
                                self.j.save_risk(symbol, exit_role='flatten', reason=reason)
                                t.pending_flatten = True
                                self.j.save_trade(t)
                    for entry in blockers:
                        self._cancel(entry, 'cancel entry remainder before protection/exit or cutoff')
                    continue
                if t and t.entry_order_id:
                    entry = self.j.order(t.entry_order_id)
                    if entry and entry.status in LOST and entry.filled_qty == 0 and t.state in ('entry_open', 'entry_cutoff_pending'):
                        t.state = 'entry_cutoff_canceled' if t.state == 'entry_cutoff_pending' else 'entry_' + entry.status
                        self.j.save_trade(t)
                if self.j.intents(True, symbol):
                    self.j.set_breaker('stuck_pending_order', symbol, 'unresolved broker mutation')
                    return None
                self.j.clear_breaker('stuck_pending_order', symbol)
                if not pos:
                    if stops or exits:
                        for o in stops + exits:
                            self._cancel(o, 'flat position: cancel remaining bot orders')
                        continue
                    if t:
                        if not t.closed:
                            observed = self.j.orders(t.day, symbol)
                            fills = [f for f in self.j.fills(t.day) if f['symbol'] == symbol]
                            filled_entry = any(f['role'] == 'entry' and Decimal(f['qty']) > 0 for f in fills)
                            net = sum((Decimal(f['qty']) * _side_sign(f['side']) for f in fills), Decimal(0))
                            if filled_entry and net == 0 and all(o.status in TERMINAL for o in observed):
                                previous = t.state
                                t.closed, t.state = True, 'closed'
                                self.j.save_diagnostic(self.day, f'flat_trade_confirmed:{symbol}',
                                    {'at': now.isoformat(), 'previous_state': previous,
                                     'basis': 'broker flat, terminal orders and offset fills'})
                        t.pending_flatten, t.protected_qty = False, Decimal(0)
                        self.j.save_trade(t)
                    self.j.save_risk(symbol, unprotected_since=None, exit_role=None, reason=None)
                    for kind in ('unprotected_position', 'overnight_position', 'duplicate_order', 'stale_market_data', 'sign_flip'):
                        self.j.clear_breaker(kind, symbol)
                    return None
                if exits:
                    side = 'sell' if pos > 0 else 'buy'
                    if len(exits) != 1 or any(o.cancel_requested or o.side != side or o.qty - o.filled_qty != abs(pos) for o in exits):
                        if not resynced:
                            resynced, self._broker_snapshot_valid = True, False  # same race as above: re-read before cancelling
                            continue
                        seen = '; '.join(f'{o.role} {o.side} qty={o.qty} filled={o.filled_qty} cancel_requested={o.cancel_requested}'
                                         for o in exits)
                        self.j.set_breaker('duplicate_order', symbol,
                                           f'exit side/count/remaining quantity disagrees with position={abs(pos)}: {seen}')
                        self.j.save_risk(symbol, exit_role=risk['exit_role'] or 'flatten',
                                         reason='reconcile conflicting recovered exits')
                        for order in stops + exits:
                            self._cancel(order, 'unsafe exit inventory; confirm all terminal before retry')
                        continue
                    if stops:
                        self.j.set_breaker('duplicate_order', symbol, 'simultaneous stop and exit')
                        for stop in stops:
                            self._cancel(stop, 'stop/exit invariant')
                    for exit_order in exits:
                        age = (now - datetime.fromisoformat(exit_order.created_at)).total_seconds()
                        if age >= self.settings.exit_timeout_seconds:
                            self.j.set_breaker('stuck_pending_order', symbol, 'exit hard timeout')
                            self._cancel(exit_order, 'exit timeout; retry only after terminal state')
                    return None
                working = len(stops) == 1 and stops[0].status == 'new' and not stops[0].cancel_requested \
                    and stops[0].qty - stops[0].filled_qty == abs(pos) \
                    and stops[0].side == ('sell' if pos > 0 else 'buy')
                if not exiting and working:
                    t.stop_order_id = stops[0].order_id
                    t.protected_qty, t.state = abs(pos), 'active'
                    self.j.save_trade(t)
                    self.j.save_risk(symbol, unprotected_since=None)
                    self.j.clear_breaker('unprotected_position', symbol)
                    return None
                since = risk['unprotected_since'] or now.isoformat()
                self.j.save_risk(symbol, unprotected_since=since)
                expired = (now - datetime.fromisoformat(since)).total_seconds() >= self.settings.protection_timeout_seconds
                self.j.set_breaker('unprotected_position', symbol, 'protection not confirmed')
                if t:
                    t.protected_qty, t.state = Decimal(0), t.state if t.closed else 'protecting'
                    self.j.save_trade(t)
                if expired:
                    exiting = True
                    self.j.save_risk(symbol, exit_role=risk['exit_role'] or 'flatten', reason='protection timeout')
                    risk = self.j.risk(symbol)
                if len(stops) > 1:
                    self.stats['duplicate_stops_canceled'] += len(stops) - 1
                    self.j.set_breaker('duplicate_order', symbol, 'multiple active stops')
                if stops:
                    if (exiting or len(stops) != 1 or stops[0].qty - stops[0].filled_qty != abs(pos)
                            or stops[0].side != ('sell' if pos > 0 else 'buy')):
                        self.stats['stop_qty_updates'] += 1
                        if not exiting:
                            self._tl('stop_update', symbol, role='stop', qty=abs(pos), price=self.fill_stop_price(t),
                                     detail={'method': 'cancel-confirm-new'}, event_time=now.isoformat())
                        for stop in stops:
                            self._cancel(stop, 'cancel-confirm-new or flatten')
                        continue
                    return None  # accepted/pending_new: bounded transition, never protected
                if not self.market_open(now):
                    if self.market_state_unknown(now):
                        return None  # retry on the next tick, after a fresh market check
                    self.j.set_breaker('overnight_position', symbol, f'broker position {pos}')
                    return None
                if exiting:
                    self.stats['position_guard_actions'] += 1
                    if now >= self.schedule.session.close - timedelta(seconds=C.NO_NEW_EXITS_BEFORE_CLOSE_SECONDS):
                        self.j.set_breaker('overnight_position', symbol, 'too late for a safe DAY exit')
                        return None
                    role = risk['exit_role'] or 'flatten'
                    reason = risk['reason'] or 'recovery flatten'
                    req = OrderRequest(symbol, 'sell' if pos > 0 else 'buy', abs(pos), 'market',
                                       client_order_id(self.day, symbol, role, self._seq(symbol, role)))
                    result = self._submit(req, role, reason)
                    if result is None:
                        self.j.save_risk(symbol, exit_role=role, reason='retry rejected exit')
                    return result
                if t is None or t.stop_price <= 0:
                    self.j.save_risk(symbol, exit_role='flatten', reason='no valid protective plan')
                    continue
                side = 'sell' if pos > 0 else 'buy'
                price = self.fill_stop_price(t)
                quote_failure = None
                try:
                    valid = self._protection_quote(symbol, side, price, broker_validation=True)
                    if not valid:
                        self.stats['protective_stop_crossed'] += 1
                        self.stop_lost(t, 'stop crossed before submit')
                except (ConnectionError, KeyError, ValueError, ArithmeticError) as exc:
                    self.j.set_breaker('stale_market_data', symbol, str(exc))
                    quote_failure = f'{type(exc).__name__}: {exc}'
                    valid = False
                if not valid:
                    reason = 'protection_quote_unusable_before_stop' if quote_failure else 'stop_crossed_before_submit'
                    self.j.save_diagnostic(self.day, f'protection_exit:{symbol}',
                        {**(self.j.diagnostic(self.day, f'protection_quote:{symbol}') or {}),
                         'at': now.isoformat(), 'reason': reason, 'error': quote_failure,
                         'position': str(pos), 'entry_cancel_pending': False})
                    if not self.j.risk(symbol)['exit_role']:
                        self.j.save_risk(symbol, exit_role='flatten', reason=reason)
                    continue
                req = OrderRequest(symbol, side, abs(pos), 'stop',
                                   client_order_id(self.day, symbol, 'stop', self._seq(symbol, 'stop')), price)
                if t.stop_order_id:
                    self.stats['position_guard_actions'] += 1
                rec = self._submit(req, 'stop', 'ATR stop after entry cancellation confirmed')
                t = self.j.trade(self.dkey, symbol)
                if rec is None or rec.status in LOST:
                    if isinstance(self.last_reject, OrderUncertain):
                        continue
                    self.stop_lost(t, 'stop submit rejected or ambiguous')
                    continue
                t.stop_order_id, t.stop_price = rec.order_id, price
                self.j.save_trade(t)
            return None
        finally:
            self._reconciling = False


    def shutdown(self) -> bool:
        """Block entries immediately; success requires confirmed flat/no bot orders."""
        self._shutting_down = True
        self._broker_snapshot_valid = False
        self.j.set_breaker('shutdown_incomplete', detail='shutdown started')
        deadline = self.clock.now() + timedelta(seconds=self.settings.shutdown_timeout_seconds)
        while self.clock.now() < deadline:
            try:
                self._broker_snapshot_valid = False
                self._refresh()
                symbols = set(self.j.owned_qty()) | {o.symbol for o in self.j.orders() if o.status not in TERMINAL}
                for symbol in symbols:
                    self.j.save_risk(symbol, exit_role='flatten', reason='graceful shutdown')
                    self.reconcile_symbol(symbol)
                self._broker_snapshot_valid = False
                self._refresh()
                live = [o for o in self.j.orders() if o.status not in TERMINAL]
                if not live and not self.bot_positions() and not self.j.intents(True):
                    self.j.clear_breaker('shutdown_incomplete')
                    self.j.clear_breaker('reconciliation_unhealthy')  # the blip it recorded is disproved by this clean read
                    self.j.set_meta('shutdown', 'complete')
                    return True
            except ConnectionError as exc:
                self.j.set_breaker('reconciliation_unhealthy', detail=str(exc))
            self.clock.sleep(1)
        self.j.set_meta('shutdown', 'incomplete')
        return False


    def entry_cutoff(self) -> datetime:
        return self.schedule.entry_cutoff
