"""JSON API for the journal dashboard.

Pure assembly over the adapters: every endpoint is a deterministic function of the
journals on disk. There is no order path, no configuration write and no network
call anywhere in this module.

The investigation queue and the findings are computed by explicit rules over the
recorded evidence and cite the trades and artifacts they came from. They are
labelled ``deterministic`` on purpose: no model rewrites a metric, changes a
strategy rule, changes risk, or submits an order, and none of this text feeds back
into the bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import threading
from typing import Any, Iterable, Sequence

from . import comparison as comparison_mod
from . import funnel as funnel_mod
from . import metrics
from . import reviews as reviews_mod
from . import safety as safety_mod
from . import sources as sources_mod
from .trades import LOSS_CLASSES, ZERO, Trade, TradeSet, q

TRADE_SORTS = {"entry_time": lambda t: t.raw["entry_time"] or "",
               "day": lambda t: (t.day, t.raw["entry_time"] or ""),
               "symbol": lambda t: t.symbol,
               "realized_pnl": lambda t: t.realized or ZERO,
               "modeled_net_pnl": lambda t: t.net or ZERO,
               "r": lambda t: t.r or ZERO,
               "slippage": lambda t: t.entry_slippage or ZERO,
               "cost": lambda t: t.raw["modeled_cost"],
               "qty": lambda t: t.raw["qty"]}


@dataclass
class Filters:
    symbol: str | None = None
    side: str | None = None
    day_from: str | None = None
    day_to: str | None = None
    exit_reason: str | None = None
    loss_class: str | None = None
    defect: bool | None = None
    relvol_bucket: str | None = None
    strategy_version: str | None = None
    search: str | None = None
    min_net: Decimal | None = None
    max_net: Decimal | None = None
    only_open: bool = False
    sort: str = "entry_time"
    order: str = "desc"
    limit: int = 500
    offset: int = 0

    @classmethod
    def from_query(cls, query: dict[str, list[str]]) -> "Filters":
        def one(name: str) -> str | None:
            values = query.get(name) or []
            value = values[0] if values else None
            return value or None

        def flag(name: str) -> bool | None:
            """Tri-state, strict: true / false / not-set. Anything else is not a filter.

            A typo'd value must not silently become "false" and narrow the table to the
            opposite set of trades.
            """
            value = one(name)
            if value is None:
                return None
            lowered = value.lower()
            if lowered in ("1", "true", "yes", "on"):
                return True
            if lowered in ("0", "false", "no", "off"):
                return False
            return None

        def dec(name: str) -> Decimal | None:
            value = one(name)
            if value is None:
                return None
            try:
                return Decimal(value)
            except Exception:  # noqa: BLE001
                return None

        def integer(name: str, default: int) -> int:
            try:
                return int(one(name) or default)
            except ValueError:
                return default

        return cls(symbol=one("symbol"), side=one("side"), day_from=one("from"), day_to=one("to"),
                   exit_reason=one("exit_reason"), loss_class=one("loss_class"), defect=flag("defect"),
                   relvol_bucket=one("relvol_bucket"), strategy_version=one("strategy_version"),
                   search=one("search"), min_net=dec("min_net"), max_net=dec("max_net"),
                   only_open=bool(flag("only_open")), sort=one("sort") or "entry_time",
                   order=one("order") or "desc", limit=max(1, min(5000, integer("limit", 500))),
                   offset=max(0, integer("offset", 0)))

    def describe(self) -> list[dict[str, Any]]:
        active = []
        for name, value in (("symbol", self.symbol), ("side", self.side), ("from", self.day_from),
                            ("to", self.day_to), ("exit_reason", self.exit_reason),
                            ("loss_class", self.loss_class), ("defect", self.defect),
                            ("relvol_bucket", self.relvol_bucket),
                            ("strategy_version", self.strategy_version), ("search", self.search),
                            ("min_net", self.min_net), ("max_net", self.max_net),
                            ("only_open", self.only_open or None)):
            if value is not None:
                active.append({"name": name, "value": str(value)})
        return active


class Journal:
    """Read-only facade. One instance per server; caches until a source file changes."""

    def __init__(self, sources: sources_mod.Sources, settings):
        self.sources = sources
        self.settings = settings
        self._tradeset: TradeSet | None = None
        self._fingerprint: tuple | None = None
        self._tradeset_lock = threading.Lock()

    # -- cache ---------------------------------------------------------------
    def _source_fingerprint(self) -> tuple:
        marks = []
        databases = (self.sources.state_journal.path, self.sources.trade_journal.path)
        for path in (*databases, *(Path(str(p) + "-wal") for p in databases)):
            try:
                stat = Path(path).stat()
                marks.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                marks.append((str(path), None, None))
        return tuple(marks)

    def tradeset(self, refresh: bool = False) -> TradeSet:
        with self._tradeset_lock:
            fingerprint = self._source_fingerprint()
            if refresh or self._tradeset is None or self._fingerprint != fingerprint:
                self._tradeset = _build_trades(self.sources, self.settings)
                self._fingerprint = fingerprint
            return self._tradeset

    def all_trades(self) -> list[Trade]:
        tradeset = self.tradeset()
        return tradeset.trades + tradeset.open_trades

    def day_list(self) -> list[str]:
        found = {t.day for t in self.all_trades()}
        found |= {d["day"] for d in self.sources.state_journal.days()}
        return sorted(found, reverse=True)

    # -- endpoints -----------------------------------------------------------
    def meta(self) -> dict[str, Any]:
        tradeset = self.tradeset()
        return {
            "variant": self.settings.variant,
            "mode": self.sources.mode,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "read_only_note": ("this dashboard opens every database with mode=ro and imports no order path; "
                              "it cannot place, cancel or replace an order and cannot change strategy or risk"),
            "sources": {
                "data_dir": str(self.sources.data_dir),
                "journal_dir": str(self.sources.journal_dir),
                "research_dir": str(self.sources.research_dir),
                "log_dir": str(self.sources.log_dir),
                "state_journal": str(self.sources.state_journal.path),
                "trade_journal": str(self.sources.trade_journal.path),
            },
            "config": self.sources.config,
            "settings_source": self.sources.settings_source,
            "availability": self.sources.available(),
            "limitations": tradeset.notes,
            "accounting_rules": ACCOUNTING_RULES,
            "filters": list(_FILTER_DOCS),
        }

    def overview(self, *, as_of: str | None = None) -> dict[str, Any]:
        tradeset = self.tradeset()
        trades = tradeset.trades
        today = as_of or date.today().isoformat()
        d = date.fromisoformat(today)
        week_start, week_end = reviews_mod.week_bounds(d)
        month = today[:7]
        return {
            "as_of": today,
            "status": _status_strip(self.sources, self),
            "periods": {
                "today": metrics.perf([t for t in trades if t.day == today], label="today"),
                "week": metrics.perf(_window(trades, week_start.isoformat(), week_end.isoformat()),
                                     label=f"{week_start} .. {week_end}"),
                "month": metrics.perf(_window(trades, month + "-01",
                                              reviews_mod.month_bounds(month)[1].isoformat()),
                                      label=month),
                "all_time": metrics.perf(trades, label="all recorded sessions"),
            },
            "equity": metrics.equity_curve(trades, tradeset.starting_equity, tradeset.equity_points),
            "ratios": metrics.ratios(trades, tradeset.starting_equity),
            "costs": metrics.stress(trades),
            "slippage": metrics.dist([float(t.entry_slippage) for t in trades
                                      if t.entry_slippage is not None]),
            "positions": self._positions(),
            "orders": self._open_orders(),
            "investigation": self.investigation(),
            "findings": self.findings(),
            "availability": self.sources.available(),
            "limitations": tradeset.notes,
        }

    def _positions(self) -> dict[str, Any]:
        tradeset = self.tradeset()
        open_rows = [t.to_dict(include_raw=True) for t in tradeset.open_trades]
        paper_auto = self.sources.health.paper_auto() or {}
        return {"open_trades": open_rows,
                "open_count": len(open_rows),
                "broker_snapshot_positions": paper_auto.get("final_positions"),
                "broker_snapshot_at": paper_auto.get("updated_at"),
                "journal_owned_qty": self.sources.state_journal.owned_qty(),
                "note": "a trade still open in the record is not the same as a live position; the broker "
                        "snapshot is the only broker truth and it has an age"}

    def _open_orders(self) -> dict[str, Any]:
        state = self.sources.state_journal
        orders = [o for o in state.orders() if o.get("status") not in
                  ("filled", "canceled", "cancelled", "rejected", "expired", "done_for_day")]
        paper_auto = self.sources.health.paper_auto() or {}
        return {"journal_nonterminal": [
            {"day": o["day"], "symbol": o["symbol"], "role": o["role"], "status": o["status"],
             "client_order_id": o["client_order_id"], "qty": o["qty"], "filled_qty": o["filled_qty"]}
            for o in orders],
            "broker_open_orders": paper_auto.get("final_open_orders"),
            "broker_snapshot_at": paper_auto.get("updated_at"),
            "note": "non-terminal journal rows are history by definition (they belong to past sessions); "
                    "only the broker snapshot can say what is live now"}

    def trades(self, filters: Filters) -> dict[str, Any]:
        rows = self.all_trades()
        # The table is about performance, so a still-open position is excluded by default:
        # its P&L is not realized yet. "open only" is the inverse, never "everything".
        if filters.only_open:
            rows = [t for t in rows if t.raw["open_qty"] != ZERO]
        else:
            rows = [t for t in rows if t.raw["open_qty"] == ZERO]
        if filters.symbol:
            rows = [t for t in rows if t.symbol.upper() == filters.symbol.upper()]
        if filters.side:
            rows = [t for t in rows if t.side == filters.side]
        if filters.day_from:
            rows = [t for t in rows if t.day >= filters.day_from]
        if filters.day_to:
            rows = [t for t in rows if t.day <= filters.day_to]
        if filters.exit_reason:
            rows = [t for t in rows if t.exit_reason == filters.exit_reason]
        if filters.loss_class:
            rows = [t for t in rows if t.loss_class == filters.loss_class]
        if filters.defect is not None:
            rows = [t for t in rows if bool(t.execution_defects) is filters.defect]
        if filters.relvol_bucket:
            rows = [t for t in rows if t.relvol_bucket() == filters.relvol_bucket]
        if filters.strategy_version:
            rows = [t for t in rows if t.strategy_version == filters.strategy_version]
        if filters.search:
            needle = filters.search.lower()
            rows = [t for t in rows if needle in t.symbol.lower() or needle in t.trade_id.lower()
                    or needle in (t.state or "").lower() or needle in (t.exit_reason or "").lower()]
        if filters.min_net is not None:
            rows = [t for t in rows if t.net is not None and t.net >= filters.min_net]
        if filters.max_net is not None:
            rows = [t for t in rows if t.net is not None and t.net <= filters.max_net]
        key = TRADE_SORTS.get(filters.sort) or TRADE_SORTS["entry_time"]
        rows = sorted(rows, key=key, reverse=(filters.order == "desc"))
        total = len(rows)
        page = rows[filters.offset:filters.offset + filters.limit]
        return {
            "total": total,
            "returned": len(page),
            "offset": filters.offset,
            "limit": filters.limit,
            "filters": filters.describe(),
            "options": self._filter_options(),
            "aggregate": metrics.perf(rows, label="filtered"),
            "rows": [t.to_dict() for t in page],
        }

    def _filter_options(self) -> dict[str, Any]:
        rows = self.all_trades()
        return {
            "exit_reason": sorted({t.exit_reason for t in rows if t.exit_reason}),
            "loss_class": list(LOSS_CLASSES),
            "side": ["long", "short"],
            "relvol_bucket": sorted({t.relvol_bucket() for t in rows if t.relvol_bucket()}),
            "strategy_version": sorted({t.strategy_version for t in rows if t.strategy_version}),
            "symbol": sorted({t.symbol for t in rows}),
            "day_from": min((t.day for t in rows), default=None),
            "day_to": max((t.day for t in rows), default=None),
        }

    def trade(self, trade_id: str) -> dict[str, Any] | None:
        trade = self.tradeset().by_id(trade_id)
        if trade is None:
            return None
        return self.lifecycle(trade)

    def lifecycle(self, trade: Trade) -> dict[str, Any]:
        """The complete lifecycle: signal -> candidate -> quote -> intent -> order -> ack ->
        fills -> partial handling -> protection -> stop changes -> exit -> reconciliation,
        with the raw evidence for each step kept underneath."""
        state = self.sources.state_journal
        tlog = self.sources.trade_journal
        day, symbol = trade.day, trade.symbol
        orders = state.orders(day, symbol)
        repeated = sum(t.day == day and t.symbol == symbol for t in self.all_trades()) > 1
        trip_ids = set(trade.raw['order_ids'])
        if repeated:
            orders = [o for o in orders if o['order_id'] in trip_ids]
        order_ids = {o["order_id"] for o in orders}
        fills = [f for f in state.fills() if f['order_id'] in trip_ids]
        # An intent is this trade's when it carries the trade id, or when it points at one
        # of the trade's orders. An unresolved stop intent whose order never reached the
        # broker is the single best piece of evidence for an unprotected fill, so it must
        # stay in the walkthrough.
        intents = [i for i in state.intents() if i["symbol"] == symbol and
                   ((not repeated and i.get("trade_id") == trade.trade_id) or i.get("order_id") in order_ids or
                    any(o["client_order_id"] == i.get("client_order_id") for o in orders))]
        events = tlog.events(day, symbol)
        quotes = [e for e in self.sources.logs.events(day, kinds=("entry_quote",))
                  if e.get("symbol") == symbol]
        log_events = [e for e in self.sources.logs.events(day, kinds=(
            "signal", "order_intent", "order_event", "fill", "order_rejected", "order_cancel",
            "stop", "stop_failed", "stop_update_failed", "trade_closed", "recovery", "orphan_stop",
            "flatten_not_working", "sizing_difference", "position_guard", "sweep", "overnight"))
            if e.get("symbol") == symbol or f"-{symbol}-" in str(e.get("msg") or "")]
        candidate = next((c for c in tlog.candidates(day, ) if c["symbol"] == symbol), None)
        state_candidate = next((c for c in state.candidates(day) if c["symbol"] == symbol), None)
        plan = next((p for p in tlog.plans(day) if p["symbol"] == symbol), None)
        return {
            "trade": trade.to_dict(include_raw=True),
            "shared_day_context": ('Repeated symbol/day: signal, quote and unfilled-order attribution is unresolved; '
                                   'fill rows are restricted to this trip.' if repeated else None),
            "steps": _lifecycle_steps(trade, candidate, state_candidate, quotes, intents, orders, fills,
                                      events, log_events),
            "plan": plan,
            "candidate": candidate or state_candidate,
            "candidate_source": "trading_journal" if candidate else
                                ("engine_candidate_table" if state_candidate else None),
            "quote_evidence": quotes,
            "orders": [dict(o) for o in orders],
            "fills": fills,
            "intents": intents,
            "exec_events": events,
            "log_events": log_events,
            "reconciliation": {
                "journal_owned_qty": state.owned_qty().get(symbol),
                "journal_owned_qty_note": ("null means the journal's fills net to zero for this symbol, "
                                           "which is the expected state for a closed trade"),
                "entry_qty": str(trade.raw["qty"]),
                "exit_qty": str(trade.raw["exit_qty"]),
                "balanced": trade.raw["qty"] == trade.raw["exit_qty"],
                "broker_orders_in_snapshot": self._snapshot_orders_for(symbol),
                "note": ("raw broker fills are the strongest accounting evidence; the state journal's own "
                         "fills are the local mirror of them"),
            },
            "limitations": [gap for gap in trade.record_gaps] +
                           ["no intra-trade 1-minute bars are journaled: MFE/MAE use fill prices only"],
        }

    def _snapshot_orders_for(self, symbol: str) -> list[dict[str, Any]]:
        out = []
        for snap in self.sources.broker_snapshots.list():
            for o in self.sources.broker_snapshots.raw_orders(snap["name"]):
                if o.get("symbol") == symbol:
                    out.append({"snapshot": snap["name"], "observed_at": snap["observed_at"],
                                "client_order_id": o.get("client_order_id"), "status": o.get("status"),
                                "qty": o.get("qty"), "filled_qty": o.get("filled_qty"),
                                "filled_avg_price": o.get("filled_avg_price")})
        return out

    def execution(self, days: Sequence[str] | None = None) -> dict[str, Any]:
        return safety_mod.build(self.sources, self.all_trades(), days=days)

    def opportunities(self, day: str | None = None) -> dict[str, Any]:
        day = day or self._default_funnel_day()
        payload = funnel_mod.build(self.sources, day)
        payload["available_days"] = self.day_list()
        payload["selected"] = day
        return payload

    def _default_funnel_day(self) -> str:
        """The newest session the funnel can actually say something about.

        The engine pre-creates a day row for every session it starts, so the newest row is
        often a day with no candidates at all — opening on it would answer "nothing is
        recorded" to the page's own question. Prefer the newest day with candidate rows,
        then the newest day with a trade, then the newest day.
        """
        days = self.day_list()
        with_candidates = set(self.sources.trade_journal.candidate_days())
        with_candidates |= {c["day"] for c in self.sources.state_journal.candidates()}
        with_trades = {t.day for t in self.all_trades()}
        for source in (with_candidates, with_trades):
            picked = [d for d in days if d in source]
            if picked:
                return picked[0]                     # day_list() is newest first
        return days[0] if days else date.today().isoformat()

    def analytics(self, filters: Filters | None = None, days: Sequence[str] | None = None) -> dict[str, Any]:
        filters = filters or Filters()
        rows = self.trades(filters)
        trade_objs = [t for t in self.all_trades() if t.raw["open_qty"] == ZERO]
        if filters.day_from:
            trade_objs = [t for t in trade_objs if t.day >= filters.day_from]
        if filters.day_to:
            trade_objs = [t for t in trade_objs if t.day <= filters.day_to]
        quality = safety_mod.data_quality(self.sources, trade_objs,
                                         days or sorted({t.day for t in trade_objs}))
        spreads = {k: Decimal(v) for k, v in (quality.get("spread_by_trade") or {}).items()}
        delays = quality.get("delays_by_trade") or {}
        return {
            "window": {"from": filters.day_from, "to": filters.day_to,
                       "trades": len(trade_objs),
                       "days": sorted({t.day for t in trade_objs})},
            "overall": metrics.perf(trade_objs, label="window"),
            "ratios": metrics.ratios(trade_objs, self.tradeset().starting_equity),
            "equity": metrics.equity_curve(trade_objs, self.tradeset().starting_equity),
            "breakdowns": metrics.breakdowns(trade_objs, spreads=spreads, delays=delays),
            "incidents_by_day": metrics.incidents_by_day(trade_objs),
            "sample_note": (f"every breakdown shows its own sample size; buckets with fewer than "
                            f"{metrics.MIN_TRADES_FOR_STATS} trades are marked invalid"),
            "regime_unavailable": "no market/volatility regime is journaled per session",
        }

    def comparison(self, day: str | None = None) -> dict[str, Any]:
        tradeset = self.tradeset()
        return comparison_mod.build(self.sources, tradeset.trades,
                                    base_equity=tradeset.starting_equity,
                                    recorded_points=tradeset.equity_points)

    def calendar(self, month: str | None = None, span: int = 3) -> dict[str, Any]:
        tradeset = self.tradeset()
        months_ = reviews_mod.months(self.sources, tradeset.trades) or [date.today().isoformat()[:7]]
        selected = month or months_[0]
        return {"selected": selected, "months": months_,
                "calendar": reviews_mod.calendar(self.sources, selected, tradeset.trades),
                "recent": [reviews_mod.calendar(self.sources, m, tradeset.trades) for m in months_[:span]]}

    def reviews(self, day: str | None = None) -> dict[str, Any]:
        tradeset = self.tradeset()
        incidents = metrics.incidents_by_day(tradeset.trades)
        payload = reviews_mod.build(self.sources, tradeset.trades, day=day, incidents_by_day=incidents)
        payload["available_days"] = self.day_list()
        return payload

    def mistakes(self) -> dict[str, Any]:
        """The loss journal joined to the trades it judges.

        Every closed losing trade appears exactly once: under ``reviewed`` when the daily
        review has judged it, otherwise under ``awaiting_review`` with the deterministic
        classifier's suggestion (``normal_strategy`` suggests unlucky, anything else a
        mistake). Reviewed entries without a trade id are bot incidents (no trade lost).
        """
        book = self.sources.loss_journal.read()
        # Append-only file: a later line for the same trade (or, for an incident, the same day and
        # title) supersedes the earlier one.
        latest: dict[tuple, dict] = {}
        for e in book["entries"]:
            key = ("trade", e["trade_id"]) if e.get("trade_id") else ("incident", e["day"], e["title"])
            latest.pop(key, None)
            latest[key] = e
        book["entries"] = list(latest.values())
        by_trade = {e["trade_id"]: e for e in book["entries"] if e.get("trade_id")}
        trades = {t.trade_id: t for t in self.all_trades()}
        reviewed = []
        for entry in book["entries"]:
            t = trades.get(entry.get("trade_id"))
            reviewed.append({**entry, "fix": entry.get("fix") or {"status": "none"},
                             "trade": t.to_dict() if t else None})
        awaiting = []
        for tid, t in trades.items():
            pnl = t.raw["realized_pnl"]
            if tid in by_trade or t.raw["open_qty"] != ZERO or pnl is None or pnl >= ZERO:
                continue
            awaiting.append({"trade_id": tid, "day": t.day, "symbol": t.symbol,
                             "suggested": "unlucky" if t.loss_class == "normal_strategy" else "mistake",
                             "loss_class": t.loss_class, "loss_reasons": t.loss_reasons,
                             "realized_pnl": str(pnl)})
        awaiting.sort(key=lambda r: (r["day"], r["symbol"]), reverse=True)
        reviewed.sort(key=lambda r: r["day"], reverse=True)
        mistakes = [e for e in reviewed if e["verdict"] == "mistake"]
        lessons = _lessons(book["entries"])
        return {"lessons": lessons,"state": book["state"], "path": book["path"], "errors": book["errors"],
                "reviewed": reviewed, "awaiting_review": awaiting,
                "summary": {"mistakes": len(mistakes),
                            "unlucky": sum(e["verdict"] == "unlucky" for e in reviewed),
                            "unknown": sum(e["verdict"] == "unknown" for e in reviewed),
                            "pre_hardening": sum(e.get("era") == "pre-hardening" for e in mistakes),
                            "current": sum(e.get("era") != "pre-hardening" for e in mistakes),
                            "fixed": sum(e["fix"]["status"] == "shipped" for e in mistakes),
                            "fix_waiting_approval": sum(e["fix"]["status"] == "proposed" for e in mistakes),
                            "no_fix_yet": sum(e["fix"]["status"] == "none" for e in mistakes),
                            "awaiting_review": len(awaiting),
                            "repeated": sum(1 for x in lessons if x["times"] > 1),
                            "recurred_after_fix": sum(1 for x in lessons if x["recurred_after_fix"])}}

    def health(self) -> dict[str, Any]:
        return {"status": _status_strip(self.sources, self), "safety": safety_mod.safety_state(self.sources),
                "availability": self.sources.available()}

    # -- investigation queue -------------------------------------------------
    def investigation(self) -> list[dict[str, Any]]:
        trades = self.all_trades()
        queue: list[dict[str, Any]] = []

        def add(severity: str, title: str, detail: str, rows: Sequence[Trade], action: str,
                source: str):
            if not rows:
                return
            queue.append({"severity": severity, "title": title,
                          "detail": detail.replace("{n}", str(len(rows))),
                          "count": len(rows),
                          "trade_ids": [t.trade_id for t in rows[:25]],
                          "symbols": sorted({t.symbol for t in rows})[:25],
                          "days": sorted({t.day for t in rows}),
                          "suggested_action": action, "evidence_source": source,
                          "deterministic": True})

        add("critical", "Entries ran without full stop coverage",
            "{n} trade(s) filled with no protective stop or with a stop smaller than the position",
            [t for t in trades if {"entry_unprotected", "stop_coverage_short"} & set(t.execution_defects)],
            "check the protection path after partial fills and the stop-resize logic", "journal.sqlite:orders")
        add("critical", "Protective stop was rejected",
            "{n} trade(s) where the broker refused the stop order",
            [t for t in trades if "stop_rejected" in t.execution_defects],
            "inspect the reject body and the stop price used", "journal.sqlite:orders")
        add("critical", "Position closed by a flatten with no stop fill",
            "{n} trade(s) left to the bot's own flatten instead of its protection",
            [t for t in trades if "flatten_without_stop" in t.execution_defects],
            "find out why no protective stop existed before the flatten", "journal.sqlite:orders")
        add("high", "Position flattened after the close",
            "{n} trade(s) were closed after 16:00 ET",
            [t for t in trades if "after_close_flatten" in t.execution_defects],
            "the overnight rule was enforced late; check the EOD sequencing", "journal.sqlite:orders")
        add("high", "Stop change failed",
            "{n} trade(s) had a stop update fail",
            [t for t in trades if "stop_update_failed" in t.execution_defects],
            "look at the failed replace: broker error, race, or replaced-order chain", "journal.sqlite:orders")
        add("high", "Signal was crossed before submission",
            "{n} trade(s) had their entry refused because the trigger was already through",
            [t for t in trades if "entry_crossed_skip" in t.execution_defects],
            "measure how much of the entry edge is lost to the 09:35:03 delay", "journal.sqlite:orders")
        add("high", "Position was still open past a session boundary",
            "{n} trade(s) carried across days instead of closing same-day",
            [t for t in trades if t.raw["carry"]],
            "confirm the close sweep and the overnight fallback both ran", "journal.sqlite:orders")
        add("medium", "Sign flip inside one order",
            "{n} trade(s) reversed sign inside a single fill sequence",
            [t for t in trades if "sign_flip" in t.execution_defects],
            "check for a duplicate opposite order", "journal.sqlite:fills")
        add("medium", "Opening bar was not reproducible",
            "{n} trade(s) were built on a 09:30-09:35 bar the 09:37 re-read disagreed with",
            [t for t in trades if "bar_diverged" in t.data_flags],
            "compare 09:35 and 09:37 IEX bars for these symbols", "journal.sqlite:diagnostics")
        add("medium", "Quote was stale at the entry decision",
            "{n} trade(s) whose entry used a quote flagged stale by the gate",
            [t for t in trades if "quote_stale" in t.data_flags],
            "review the IEX quote age distribution by symbol", "runtime/logs entry_quote events")
        add("low", "Trades with an incomplete paper trail",
            "{n} trade(s) lack a plan or candidate record",
            [t for t in trades if t.record_gaps],
            "accept as history for pre-2026-10-12 sessions, or extend the export", "trade_journal.sqlite:plans")

        for snapshot in self.sources.broker_snapshots.list():
            if snapshot.get("flat_gate") is False:
                queue.append({
                    "severity": "medium",
                    "title": f"Historical broker acceptance snapshot failed its flat gate ({snapshot['name']})",
                    "detail": f"observed {snapshot.get('observed_at')}: blocked reasons "
                              f"{snapshot.get('blocked_reasons')}",
                    "count": 1, "trade_ids": [], "symbols": list((snapshot.get("positions") or {}).keys()),
                    "days": [(snapshot.get("observed_at") or "")[:10]],
                    "suggested_action": "historical record only; confirm the conditions are gone now",
                    "evidence_source": f"research/{snapshot['name']}", "deterministic": True})

        ratios = metrics.ratios(self.tradeset().trades, self.tradeset().starting_equity)
        if not ratios["valid"]:
            queue.append({"severity": "info", "title": "Risk ratios are not statistically valid yet",
                          "detail": ratios["reason"], "count": ratios["days"], "trade_ids": [],
                          "symbols": [], "days": [t.day for t in self.tradeset().trades][-5:],
                          "suggested_action": "keep the same configuration and collect more sessions",
                          "evidence_source": "trade_journal.sqlite:runs", "deterministic": True})

        availability = self.sources.available()
        for name, state in (("state_journal", availability["state_journal"]),
                            ("trade_journal", availability["trade_journal"])):
            if state["state"] != "ok":
                queue.append({"severity": "high", "title": f"{name} is {state['state']}",
                              "detail": state.get("detail") or state["state"], "count": 1,
                              "trade_ids": [], "symbols": [], "days": [],
                              "suggested_action": "the dashboard is reading a degraded source",
                              "evidence_source": name, "deterministic": True})
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        return sorted(queue, key=lambda item: (order.get(item["severity"], 9), -item["count"]))

    # -- deterministic findings ---------------------------------------------
    def findings(self) -> dict[str, Any]:
        """Hypotheses worth testing, each with the evidence it came from.

        Deterministic rule-based text. No model, no rewriting of history: this is
        arithmetic over the journal with a citation attached.
        """
        trades = self.tradeset().trades
        out: list[dict[str, Any]] = []
        days = sorted({t.day for t in trades})

        def add(title: str, detail: str, evidence: Any, source: str):
            out.append({"title": title, "detail": detail, "evidence": evidence,
                        "evidence_source": source, "deterministic": True})

        if trades:
            perf = metrics.perf(trades, label="all")
            add("Cost drag against a zero-edge strategy",
                f"modeled cost is {perf['cost_per_trade']} per trade against a modeled-net expectancy of "
                f"{perf['expectancy_per_trade']} per trade over {perf['trades']} trades",
                metrics.stress(trades), "trade_journal.sqlite + journal.sqlite:fills")
            defects = [t for t in trades if t.execution_defects]
            if defects:
                lost = sum((t.net for t in defects if t.net is not None), ZERO)
                add("Execution defects carry real money",
                    f"{len(defects)} of {len(trades)} trades recorded an execution defect, totaling "
                    f"{q(lost)} of modeled net P&L",
                    {"defect_trades": [t.trade_id for t in defects]},
                    "journal.sqlite:orders + runtime/logs")
            short = [t for t in trades if t.raw["direction"] < 0]
            long_ = [t for t in trades if t.raw["direction"] > 0]
            add("Long vs short split",
                f"long {metrics.perf(long_)['modeled_net_pnl']} over {len(long_)} trades; "
                f"short {metrics.perf(short)['modeled_net_pnl']} over {len(short)} trades",
                {"long": metrics.perf(long_), "short": metrics.perf(short)},
                "trade_journal.sqlite:outcomes")
            night = [t for t in trades if t.raw["carry"]]
            if night:
                add("Carry-over trades are an operational risk",
                    f"{len(night)} trade(s) were carried across a session boundary: "
                    f"{sorted({t.symbol for t in night})}",
                    {"carry_trades": [t.trade_id for t in night]}, "journal.sqlite:fills")
            worst = min(trades, key=lambda t: t.realized if t.realized is not None else ZERO)
            add("Worst single outcome",
                f"{worst.trade_id} lost {q(worst.realized)} ({q(worst.r, '0.01')}R), exit reason "
                f"{worst.exit_reason}, class {worst.loss_class}",
                worst.to_dict(), f"journal.sqlite:{worst.trade_id}")
            if len(days) < metrics.MIN_DAYS_FOR_RATIO:
                add("Not enough sessions for a ratio",
                    f"{len(days)} session(s) of trades; {metrics.MIN_DAYS_FOR_RATIO} are needed before "
                    "Sharpe or Sortino is quoted",
                    {"days": days}, "trade_journal.sqlite:outcomes")
        else:
            add("No closed trades on record",
                "the journal holds no position that went flat to flat; the funnel, not this list, is where "
                "the answer lives",
                {"funnel_days": self.day_list()[:5]}, "journal.sqlite:fills")
        return {
            "generated_by": "deterministic rules over the journal (no language model, no history rewritten)",
            "restrictions": ["never rewrites history", "never changes strategy rules", "never changes risk",
                             "never submits broker orders"],
            "findings": out,
        }

    def improvement(self) -> dict[str, Any]:
        """The daily improvement pipeline: ranked backlog, experiment ledger, newest daily report."""
        book = self.sources.research_ledger.read()
        reports = sorted(self.sources.data_dir.glob("reports/daily-review-????-??-??.md"))
        latest = reports[-1] if reports else None
        book["latest_report"] = ({"day": latest.stem[-10:], "text": latest.read_text(encoding="utf-8")[:6000]}
                                 if latest else None)
        book["experiments"] = sorted(book["experiments"], key=lambda e: e["day"], reverse=True)
        return book

    def snapshot(self) -> dict[str, Any]:
        """The whole dashboard as one JSON document (used by `journal-snapshot`).

        Uses the same defaults as the pages, so a snapshot and the UI never disagree about
        which session is being shown.
        """
        return {"meta": self.meta(), "overview": self.overview(), "trades": self.trades(Filters()),
                "execution": self.execution(), "opportunities": self.opportunities(),
                "analytics": self.analytics(), "comparison": self.comparison(),
                "calendar": self.calendar(), "reviews": self.reviews(), "mistakes": self.mistakes(),
                "improvement": self.improvement()}


def _lessons(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per mistake category: how often it happened, and whether it came back after a fix shipped."""
    out: dict[str, dict[str, Any]] = {}
    for e in sorted((e for e in entries if e["verdict"] == "mistake"), key=lambda e: e["day"]):
        row = out.setdefault(e["category"], {"category": e["category"], "times": 0, "days": [], "titles": [],
                                             "shipped_on": None, "recurred_after_fix": False, "tests": []})
        row["times"] += 1
        row["days"].append(e["day"])
        if e["title"] not in row["titles"]:
            row["titles"].append(e["title"])
        fix = e.get("fix") or {}
        if row["shipped_on"] and e["day"] > row["shipped_on"]:
            row["recurred_after_fix"] = True
        if fix.get("status") == "shipped" and not row["shipped_on"]:
            row["shipped_on"] = fix.get("shipped_on") or e["day"]   # a fix that predates the review is dated by its commit
        if fix.get("test") and fix["test"] not in row["tests"]:
            row["tests"].append(fix["test"])
    return sorted(out.values(), key=lambda r: (not r["recurred_after_fix"], -r["times"], r["category"]))


ACCOUNTING_RULES = {
    "realized_pnl": "cash result of the real PAPER fill prices; real slippage is already inside those prices",
    "recorded_fees": "actual broker fees. Alpaca paper charges none, so this is null, never zero",
    "modeled_cost": "the bot's own per-share cost model summed from the values recorded on each fill",
    "modeled_net_pnl": "realized_pnl - modeled_cost",
    "backtest_pnl": "from runtime/research/orb_results.json; never mixed into the PAPER figures",
    "r": "realized_pnl / initial_risk, where initial_risk is frozen at plan time "
         "(one_r_per_share from the plan, else STOP_ATR_FRACTION x ATR)",
    "never_added": "modeled slippage is never added to the slippage already inside the fill prices",
    "missing": "a missing value is reported as missing and never rendered as zero",
}

_FILTER_DOCS = (
    {"name": "symbol", "type": "string"},
    {"name": "side", "type": "long|short"},
    {"name": "from / to", "type": "YYYY-MM-DD"},
    {"name": "exit_reason", "type": "string"},
    {"name": "loss_class", "type": "normal_strategy|execution_defect|data_issue|broker_issue|mixed|unknown"},
    {"name": "defect", "type": "true|false"},
    {"name": "relvol_bucket", "type": "string"},
    {"name": "strategy_version", "type": "string"},
    {"name": "search", "type": "string"},
    {"name": "min_net / max_net", "type": "decimal"},
    {"name": "only_open", "type": "true|false"},
    {"name": "sort", "type": "|".join(TRADE_SORTS)},
    {"name": "order", "type": "asc|desc"},
    {"name": "limit / offset", "type": "int"},
)


def _window(trades: Sequence[Trade], start: str, end: str) -> list[Trade]:
    return [t for t in trades if start <= t.day <= end]


def _status_strip(sources: sources_mod.Sources, journal: "Journal") -> dict[str, Any]:
    state = safety_mod.safety_state(sources)
    tradeset = journal.tradeset()
    ratios = metrics.ratios(tradeset.trades, tradeset.starting_equity)
    return {
        "checks": state["checks"],
        "blocking": state["blocking"],
        "safe": not state["blocking"],
        "heartbeat_state": state["heartbeat_state"],
        "mode": (state["heartbeat"] or {}).get("mode"),
        "phase": (state["heartbeat"] or {}).get("phase"),
        "session": (state["heartbeat"] or {}).get("session"),
        "last_data_update": (state["heartbeat"] or {}).get("last_data_update"),
        "entries_blocked": (state["heartbeat"] or {}).get("entries_blocked"),
        "breakers": state["breakers"],
        "unresolved_intents": len(state["unresolved_intents"]),
        "flat_gate": state["runtime_report"].get("flat_gate"),
        "external_kill_latched": state["runtime_report"].get("external_kill_latched"),
        "entry_gate_open": state["runtime_report"].get("entry_gate_open"),
        "release_status": state["release_status"],
        "ratios_valid": ratios["valid"],
        "ratios_reason": ratios["reason"],
    }


def _lifecycle_steps(trade: Trade, candidate, state_candidate, quotes, intents, orders, fills, events,
                     log_events) -> list[dict[str, Any]]:
    """The eleven lifecycle steps the trade actually passed through, each with its evidence.

    A step with no evidence is reported as ``unavailable`` (the bot did not record it),
    which is different from a step that happened and had no content.
    """
    def step(key: str, label: str, state: str, detail: Any, evidence: Any) -> dict[str, Any]:
        return {"step": key, "label": label, "state": state, "detail": detail, "evidence": evidence}

    entry_orders = [o for o in orders if o["role"] == "entry"]
    stop_orders = [o for o in orders if o["role"] == "stop"]
    # A stop change is an explicit stopupd/stopfail order, or either side of a replace
    # chain: a replaced stop IS a stop change and must never read as "none".
    relabels = [o for o in orders if o["role"] in ("stopupd", "stopfail")
                or o.get("replaces") or o.get("replaced_by")]
    exit_orders = [o for o in orders if o["role"] not in ("entry", "stop", "stopupd", "stopfail")]
    entry_fills = [f for f in fills if f["role"] == "entry"]
    exit_fills = [f for f in fills if f["role"] != "entry"]
    acked = [o for o in entry_orders if o.get("status") not in (None, "rejected")]
    return [
        step("signal", "Signal", "recorded" if candidate else "unavailable",
             {"direction": (candidate or {}).get("direction"), "action": (candidate or {}).get("action"),
              "relvol": (candidate or {}).get("relvol"), "atr": (candidate or {}).get("atr")} if candidate
             else None,
             {"source": "trade_journal.sqlite:candidates" if candidate else "journal.sqlite:candidates",
              "row": candidate or state_candidate}),
        step("candidate", "Candidate ranking", "recorded" if (candidate or state_candidate) else "unavailable",
             {"rank": (candidate or state_candidate or {}).get("rank"),
              "status": (candidate or state_candidate or {}).get("status")},
             candidate or state_candidate),
        step("quote", "Pre-submit quote", "recorded" if quotes else "unavailable",
             quotes[-1] if quotes else None, {"source": "runtime/logs entry_quote", "rows": quotes}),
        step("intent", "Order intent (durable mutation gate)", "recorded" if intents else "unavailable",
             [{"mutation": i.get("mutation"), "state": i.get("state"), "attempt": i.get("attempt"),
               "ts": i.get("ts")} for i in intents], intents),
        step("order", "Entry order submitted", "recorded" if entry_orders else "unavailable",
             [{"client_order_id": o["client_order_id"], "status": o["status"], "qty": o["qty"],
               "type": o["type"], "stop_price": o["stop_price"]} for o in entry_orders], entry_orders),
        step("ack", "Broker acknowledgement", "recorded" if acked else "unavailable",
             {"status": entry_orders[0]["status"] if entry_orders else None,
              "rejected": [o["client_order_id"] for o in entry_orders if o["status"] == "rejected"]},
             [e for e in log_events if e.get("event") == "order_event"]),
        step("fills", "Fills", "recorded" if entry_fills else "not_filled",
             {"entry_fill_count": len(entry_fills), "exit_fill_count": len(exit_fills),
              "entry_vwap": trade.to_dict()["entry_vwap"], "exit_vwap": trade.to_dict()["exit_vwap"]},
             {"entry_fills": entry_fills, "exit_fills": exit_fills}),
        step("partial_fill_handling", "Partial-fill handling",
             "recorded" if (len(entry_fills) > 1 or len(exit_fills) > 1) else "not_needed",
             {"entry_partial": len(entry_fills) > 1, "exit_partial": len(exit_fills) > 1},
             {"events": [e for e in events if e.get("kind") == "fill"]}),
        step("protection", "Protective stop", "recorded" if stop_orders else "missing",
             [{"client_order_id": o["client_order_id"], "qty": o["qty"], "stop_price": o["stop_price"],
               "status": o["status"], "filled_qty": o["filled_qty"], "replaces": o["replaces"]}
              for o in stop_orders], stop_orders),
        step("stop_changes", "Stop changes", "recorded" if relabels else "none",
             [{"client_order_id": o["client_order_id"], "role": o["role"], "status": o["status"],
               "qty": o["qty"]} for o in relabels], relabels),
        step("exit", "Exit", "recorded" if exit_fills else "still_open",
             {"exit_reason": trade.exit_reason, "exit_reason_dominant": trade.exit_reason_dominant,
              "roles": trade.raw["exit_roles"], "qty": str(trade.raw["exit_qty"])},
             exit_fills),
        step("reconciliation", "Reconciliation",
             "balanced" if trade.raw["qty"] == trade.raw["exit_qty"] else "imbalanced",
             {"entry_qty": str(trade.raw["qty"]), "exit_qty": str(trade.raw["exit_qty"]),
              "order_count": len(orders)},
             {"order_ids": [o["order_id"] for o in orders],
              "raw_fills": [f["order_id"] for f in fills]}),
    ]


def _build_trades(sources, settings):
    from .trades import build as build_trades
    return build_trades(sources, settings, sources.mode)
