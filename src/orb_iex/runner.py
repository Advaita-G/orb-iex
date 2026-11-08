"""Main loop: calendar-driven schedule, scan, diagnostics, EOD, recovery.

Modes
  signal  compute and log signals; no orders of any kind
  shadow  signals + local fill simulator (SimBroker); no Alpaca orders
  paper   real Alpaca PAPER orders through AlpacaPaperBroker (armed only
          after every startup safety check)

Every scheduled event is recorded in the journal when done, so a restart
never repeats it. A missed event is not replayed late, except:
  - the scan may still run inside scan_grace_seconds; after that the day is
    marked missed and is never scanned;
  - the close-5 min exit runs late if the bot restarts before the close;
  - post-close cleanup always runs once.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from . import constants as C
from .calendar import NY, MarketCalendar, Schedule, Session
from .clock import SystemClock
from .engine import Engine
from .journal import Journal
from .logging_setup import LOGGER_NAME, log_event
from .marketdata import Bar, MarketData, load_calendar
from .orders import OrderUpdate
from .safety import SafetyError
from .strategy import EntryPlan, compute_features, plan_entry, select_candidates
from .candidate_b import adapt_plan
from .universe import eligible_assets, prefilter

log = logging.getLogger(LOGGER_NAME)

SHUTDOWN_FILE = "health/shutdown.json"    # result of the last Ctrl+C / SIGTERM shutdown (under data_dir)
FATAL_FILE = "health/fatal.json"          # under data_dir; written even when the SQLite journal is the problem
FATAL_META_KEY = "fatal_state"
SUSPEND_SECONDS = 30.0                    # wall time beyond monotonic time that proves a host suspend


class FatalRunnerError(RuntimeError):
    """The main loop died on an unexpected exception. The fatal handling (block entries, one reconciliation,
    protect-or-flatten, persist) has already run. The process must now exit non-zero so the supervisor restarts it.
    `safety` marks a SafetyError: that keeps its own exit code (no automatic restart, a person must look)."""

    def __init__(self, original: BaseException, report: dict):
        super().__init__(f"{type(original).__name__}: {original}")
        self.original = original
        self.report = report
        self.safety = isinstance(original, SafetyError)


@dataclass
class Prep:
    day: date
    symbols: list[str]
    assets: dict[str, dict]
    daily: dict[str, dict]
    openings: dict[str, dict]
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    exclusions: dict[str, str] = field(default_factory=dict)


def _bar_json(b: Bar | None) -> dict | None:
    return None if b is None else {k: str(v) if k != "v" else v for k, v in
                                   (("o", b.o), ("h", b.h), ("l", b.l), ("c", b.c), ("v", b.v))}


def _q4(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.0001"))


def format_table(rows: list[dict], limit: int = 30) -> str:
    head = f"{'rk':>3} {'symbol':<7} {'status':<11} {'relvol':>8} {'atr':>7} {'adv':>11} {'open':>9} {'close':>9} reason"
    lines = [head]
    for r in rows[:limit]:
        d = r.get("details", {})

        def f(v, fmt):
            return format(Decimal(str(v)), fmt) if v not in (None, "") else "-"
        lines.append(f"{r.get('rank') or '':>3} {r['symbol']:<7} {r['status']:<11} {f(d.get('relvol'), '.2f'):>8} "
                     f"{f(d.get('atr'), '.2f'):>7} {f(d.get('adv'), '.0f'):>11} {f(d.get('open'), '.2f'):>9} "
                     f"{f(d.get('close'), '.2f'):>9} {r.get('reason') or ''}")
    return "\n".join(lines)


class Runner:
    def __init__(self, settings, mode: str, clock, client, data: MarketData, journal: Journal,
                 broker=None, events: queue.Queue | None = None, calendar: MarketCalendar | None = None,
                 stream=None, feed_prefetch: bool = False, account_source=None, tlog=None):
        if mode not in ("signal", "shadow", "paper"):
            raise ValueError(mode)
        if mode == "paper" and settings.research_profile == "candidate_b":
            raise SafetyError("candidate_b is unqualified; use signal/shadow only")
        self.settings = settings
        self.mode = mode
        self.clock = clock
        self.client = client
        self.data = data
        self.j = journal
        self.broker = broker
        self.events = events or queue.Queue()
        self.calendar = calendar
        self.calendar_loaded_for: date | None = None if calendar is None else date.max
        self.stream = stream
        self.engine = Engine(journal, broker, settings, clock, mode) if mode != "signal" else None
        self.schedule: Schedule | None = None
        self.prep: Prep | None = None
        self.prep_failed_at: datetime | None = None
        self.last_poll: datetime | None = None
        self.feed_prefetch = feed_prefetch
        self.feed_bars: dict[str, list[Bar]] = {}
        self.feed_cursor: datetime | None = None
        self.last_feed: datetime | None = None
        self.account_source = account_source
        self.opening_0935: dict[str, Bar] = {}
        self.tlog = tlog                 # trading journal: record only, never read for decisions
        if self.engine:
            self.engine.tlog = tlog
            self.engine.quote_source = getattr(data, "entry_quotes", None)   # fresh IEX quote before each entry
        self._journal_run_started = False
        self.last_data_update: str | None = None
        self._last_heartbeat: datetime | None = None
        self.mono = time.monotonic if isinstance(clock, SystemClock) else None          # does not advance while the host is suspended (macOS); tests override
        self._tick_wall: datetime | None = None
        self._tick_mono: float | None = None
        self.suspensions: list[tuple[datetime, datetime]] = []   # (last tick before, first tick after) a host suspend
        self.scan_defer_reason: str | None = None
        self._last_disk_check: datetime | None = None
        # History pagination can take minutes without returning to the main loop.
        if isinstance(self.data, MarketData):
            self.data.on_progress = lambda: self._ops_tick(self.clock.now())

    # ----------------------------------------------------------- calendar
    def _calendar(self, now: datetime) -> MarketCalendar:
        today = now.astimezone(NY).date()
        if self.calendar is None or (self.calendar_loaded_for not in (date.max, today)):
            self.calendar = load_calendar(self.client, today)
            self.calendar_loaded_for = today
        return self.calendar

    def _session_for(self, now: datetime) -> Session | None:
        cal = self._calendar(now)
        today = cal.today(now)
        if today and not self.j.is_done(today.day, "post_close"):
            return today
        return cal.next_session(now.astimezone(NY).date(), include_today=False)

    def _start_session(self, session: Session, now: datetime) -> None:
        s = self.settings
        self.schedule = Schedule.for_session(session, s.scan_delay_seconds, s.diagnostic_rescan_delay_seconds,
                                             s.scan_grace_seconds, s.entry_cutoff_time)
        self.prep = None
        self.feed_bars, self.feed_cursor, self.last_feed = {}, None, None
        st = self.j.day(session.day)
        closing = self.schedule.session_closing(now, st["scanned"] and st["scan_status"] == "ok") \
            or st["eod_status"] is not None
        self.j.set_session_closing(session.day, closing)
        log_event(log, logging.INFO, "session",
                  f"session {session.day} open={session.open.strftime('%H:%M')} close={session.close.strftime('%H:%M')} "
                  f"ET{' (EARLY CLOSE)' if session.is_early_close else ''}; phase={self.schedule.phase(now)} "
                  f"scanned={st['scanned']} mode={self.mode}",
                  day=session.day.isoformat(), early_close=session.is_early_close, phase=self.schedule.phase(now))
        if self.engine:
            self.engine.start_day(self.schedule)
            before = getattr(self, 'before_recovery', None)
            if before is not None:
                before(now)
            if self.mode == "shadow":
                self._abandon_shadow_state()
            self.engine.recover(now)

    def _abandon_shadow_state(self) -> None:
        # The simulator lives in memory; after a restart its open trades are gone.
        for t in self.j.trades(self.engine.dkey):
            if not t.closed and t.state not in ("entry_rejected", "entry_crossed", "not_submitted",
                                                              "entry_fatal_canceled"):
                t.closed, t.state, t.note = True, "shadow_abandoned", "simulator restarted"
                self.j.save_trade(t)
        for o in self.j.orders(self.engine.dkey):
            if o.status not in ("filled", "canceled", "expired", "replaced", "rejected"):
                o.status = "canceled"
                o.cancel_requested = True
                self.j.save_order(o)

    # ------------------------------------------------------------- prep
    def prep_ready_time(self, session: Session) -> datetime:
        prev = self.calendar.previous_session(session.day)
        base = prev.close if prev else session.open - timedelta(hours=12)
        return base + timedelta(minutes=self.settings.history_min_age_minutes + 1)

    def prepare(self, session: Session) -> Prep:
        day = session.day
        asset_rows = self.data.assets(day)
        assets = {a["symbol"]: a for a in asset_rows}
        listed = eligible_assets(asset_rows)
        daily_sessions = self.calendar.sessions_before(day, C.DAILY_HISTORY_SESSIONS)
        open_sessions = self.calendar.sessions_before(day, C.OPENING_HISTORY_SESSIONS)
        if len(open_sessions) < C.OPENING_HISTORY_SESSIONS:
            raise RuntimeError("calendar too short for opening-bar history")
        daily_all = self.data.rth_daily_bars(listed, daily_sessions)
        from .history_safety import session_history_reason
        required = daily_sessions[-(C.LOOKBACK_DAYS + 1):]
        exclusions = {sym: reason for sym in listed
                      if (reason := session_history_reason(daily_all.get(sym, {}), None, required))}
        daily_all = {sym: {s.day: daily_all[sym][s.day] for s in required}
                     for sym in listed if sym not in exclusions}
        rows = prefilter(daily_all, day)
        symbols = [r["symbol"] for r in rows]
        daily = {s: daily_all[s] for s in symbols}
        openings = self.data.opening_bars_history(symbols, open_sessions)
        self.j.save_universe(day.isoformat(), rows,
                             source_session=daily_sessions[-1].day.isoformat(), first_session=day.isoformat())
        for sym in symbols:
            reason = session_history_reason(daily[sym], openings.get(sym, {}), required)
            if reason: exclusions[sym] = reason
        # Excluded rows remain auditable at scan time, but never reach indicator math.
        openings = {sym: {s.day: openings[sym][s.day] for s in required[-C.LOOKBACK_DAYS:]}
                    for sym in symbols if sym not in exclusions}
        prep = Prep(day, symbols, assets, daily, openings, exclusions=exclusions)
        log_event(log, logging.INFO, "prep",
                  f"prepared {day}: {len(listed)} tradable NYSE/Nasdaq listings, {len(symbols)} pass "
                  f"ADV(14) >= 1M and ATR(14) > 0.50; opening history for "
                  f"{sum(1 for v in openings.values() if v)} symbols",
                  day=day.isoformat(), listed=len(listed), eligible=len(symbols))
        return prep

    def _ensure_prep(self, session: Session, now: datetime) -> bool:
        if self.prep and self.prep.day == session.day:
            return True
        if now < self.prep_ready_time(session):
            return False
        if self.prep_failed_at and (now - self.prep_failed_at).total_seconds() < 60:
            return False
        try:
            self.prep = self.prepare(session)
            self.last_data_update = datetime.now(timezone.utc).isoformat()
            return True
        except Exception as exc:          # network/data: retry in a minute, never trade on partial data
            self.prep_failed_at = now
            log_event(log, logging.ERROR, "prep_failed", f"prepare {session.day} failed: {type(exc).__name__}: {exc}")
            return False

    # ------------------------------------------------------------- scan
    def account(self) -> dict:
        if self.account_source:
            return self.account_source()
        if self.broker is not None:
            return self.broker.get_account()
        resp = self.client.trading_get("/v2/account", purpose="account")
        if not resp.ok:
            raise ConnectionError(f"/v2/account failed: HTTP {resp.status}")
        return resp.data

    def features(self, bars_today: dict[str, Bar]):
        p = self.prep
        from .history_safety import session_history_reason, unexplained_discontinuity
        from .strategy import Features
        required = self.calendar.sessions_before(p.day, C.LOOKBACK_DAYS + 1)
        features = []
        for s in p.symbols:
            reason = p.exclusions.get(s) or session_history_reason(
                p.daily.get(s, {}), p.openings.get(s, {}), required, bars_today.get(s), self.schedule.session)
            reason = reason or unexplained_discontinuity(p.daily.get(s, {}), bars_today.get(s))
            if reason:
                features.append(Features(s, bars_today.get(s), None, None, None, exclusion_reason=reason))
            else:
                features.append(compute_features(s, bars_today.get(s), p.daily[s], p.openings[s], p.day))
        return features

    def scan(self, session: Session, now: datetime) -> None:
        day = session.day
        # Mark first: a crash after this point never scans the day twice.
        self.j.mark_scanned(day, "running")
        try:
            if not self._ensure_prep(session, now) and not self.prep:
                raise RuntimeError("history not prepared")
            from .history_safety import action_exclusions
            # Fetch before the opening quote/bar decision; do not reuse a later snapshot in replay.
            snapshot = self.data.corporate_action_snapshot(self.prep.symbols, day)
            self.j.save_diagnostic(day, 'corporate_action_snapshot', snapshot)
            decision_at = self.clock.now()
            self.prep.exclusions.update(action_exclusions(snapshot, self.prep.symbols,
                self.calendar.sessions_before(day, C.LOOKBACK_DAYS + 1)[0].day, day, decision_at))
            request_started = datetime.now(timezone.utc)
            retries = 0
            bars = self.data.opening_bars(self.prep.symbols, session)
            # Alpaca builds the 5-minute bar from 1-minute bars; right after 09:35
            # it may not be published yet. Retry briefly if coverage is very low.
            for attempt in range(3):
                if len(bars) >= 0.5 * len(self.prep.symbols):
                    break
                log_event(log, logging.WARNING, "opening_bars_retry",
                          f"only {len(bars)}/{len(self.prep.symbols)} IEX opening bars at attempt {attempt + 1}; retrying")
                self.clock.sleep(2)
                retries += 1
                bars = self.data.opening_bars(self.prep.symbols, session)
            request_done = datetime.now(timezone.utc)
            account = self.account()
            now = self.clock.now()
            if now >= self.schedule.scan_deadline:
                raise RuntimeError("data acquisition exceeded scan deadline")
        except Exception as exc:
            self.j.mark_scanned(day, "failed")
            log_event(log, logging.ERROR, "scan_failed", f"scan {day} failed: {type(exc).__name__}: {exc}; "
                      "no entries today")
            return
        self.opening_0935 = bars
        self.last_data_update = datetime.now(timezone.utc).isoformat()
        self.j.save_diagnostic(day, "opening_0935", {s: _bar_json(b) for s, b in bars.items()})
        missing = sorted(set(self.prep.symbols) - set(bars))
        log_event(log, logging.WARNING if missing else logging.INFO, "missing_iex_bars",
                  f"IEX opening bars: {len(bars)}/{len(self.prep.symbols)} symbols; missing {len(missing)}: {missing[:25]}",
                  count=len(missing), symbols=missing)
        feats = self.features(bars)
        top, table = select_candidates(feats)
        present = {r['symbol'] for r in table}
        table.extend({'symbol': sym, 'status': 'filtered', 'reason': reason, 'details': {}}
                     for sym, reason in self.prep.exclusions.items() if sym not in present)
        exclusions = {r['symbol']: r['reason'] for r in table if str(r.get('reason')).startswith('history:')}
        self.j.save_diagnostic(day, 'history_exclusions', exclusions)
        for sym, reason in exclusions.items():
            log_event(log, logging.WARNING, 'history_excluded', f'{sym}: {reason}', symbol=sym, reason=reason)
        equity = Decimal(str(account.get("equity")))
        plans: list[EntryPlan] = []
        plan_results: dict = {}
        by_symbol = {r["symbol"]: r for r in table}
        paired_plans = {}
        for f in top:
            p = plan_entry(f, equity, self.settings.max_gross_exposure, self.settings.commission_per_share)
            b = p if isinstance(p, str) else adapt_plan(p, f)
            paired_plans[f.symbol] = {
                "baseline_qty": None if isinstance(p, str) else p.qty,
                "candidate_qty": None if isinstance(b, str) else b.qty,
                "baseline_reason": p if isinstance(p, str) else None,
                "candidate_reason": b if isinstance(b, str) else None,
                "opening_volume": None if f.opening is None else f.opening.v,
                "decision_time": now.isoformat(), "profile": self.settings.research_profile}
            if self.settings.research_profile == "candidate_b":
                p = b
            plan_results[f.symbol] = p
            row = by_symbol[f.symbol]
            if isinstance(p, str):
                row["status"], row["reason"] = "skipped", p
            else:
                plans.append(p)
                row["details"].update({"direction": p.direction, "entry": p.entry_price, "stop": p.stop_price,
                                       "qty": p.qty, "sizing": p.sizing})
                if p.sizing.get("reference_qty_unrounded") not in (None, p.qty):
                    log_event(log, logging.INFO, "sizing_difference",
                              f"{p.symbol}: qty {p.qty} vs {p.sizing['reference_qty_unrounded']} with unrounded "
                              "(sub-penny) levels", symbol=p.symbol)
        self.j.save_diagnostic(day, "ab_plans", paired_plans)
        self.j.save_candidates(day, table)
        self.j.mark_scanned(day, "ok")
        self.j.set_session_closing(day, False)           # Main.cs: _sessionClosing = false at the scan
        log_event(log, logging.INFO, "candidates",
                  f"CANDIDATES {day} ({len(top)} of {sum(1 for f in feats if f.ready)} ready, equity {equity}):\n"
                  + format_table([r for r in table if r.get("rank")]), day=day.isoformat(),
                  top=[f.symbol for f in top])
        for p in plans:
            log_event(log, logging.INFO, "signal",
                      f"SIGNAL {p.symbol} {'LONG' if p.direction > 0 else 'SHORT'} qty={p.qty} entry(stop)={p.entry_price} "
                      f"protective stop={p.stop_price} atr={p.atr:.4f} sizing={p.sizing['binding']}",
                      symbol=p.symbol, direction=p.direction, qty=p.qty, entry=p.entry_price, stop=p.stop_price,
                      atr=p.atr, sizing=p.sizing)
        if self.mode == "signal":
            for p in plans:
                log_event(log, logging.INFO, "order_intent", f"INTENT (signal mode, not sent) {p.side} {p.qty} "
                          f"{p.symbol} stop {p.entry_price}", symbol=p.symbol)
                self.j.update_candidate(day, p.symbol, "signal_only")
        else:
            before_entries = getattr(self, 'before_entries', None)
            if before_entries is None or before_entries():
                self.engine.place_entries(plans, self.prep.assets, account, now)
        self._journal_scan(day, feats, by_symbol, plan_results, plans, account,
                           {"feed": self.settings.opening_feed, "request_started": request_started.isoformat(),
                            "request_done": request_done.isoformat(), "symbols_requested": len(self.prep.symbols),
                            "bars_received": len(bars), "missing": len(missing), "retries": retries,
                            "scheduled_scan": self.schedule.scan.isoformat(),
                            "history_feed": self.settings.history_feed,
                            "history_last_session": self.calendar.sessions_before(day, 1)[0].day.isoformat()})

    # ------------------------------------------------------- trading journal
    def _start_journal_run(self) -> None:
        if not self.tlog or self._journal_run_started:
            return
        self._journal_run_started = True
        try:
            try:
                account = self.account()
            except Exception:
                account = {}
            try:
                positions = self.engine.bot_positions() if self.engine else {}
            except Exception:
                positions = {}
            rid = self.tlog.start_run(
                mode=self.mode, settings=self.settings, account=account, bot_positions=positions,
                now=self.clock.now(),
                feeds={"opening_feed": self.settings.opening_feed, "history_feed": self.settings.history_feed,
                       "adjustment": self.settings.adjustment,
                       "history_min_age_minutes": self.settings.history_min_age_minutes,
                       "trade_updates_stream": bool(self.stream),
                       "wall_clock_utc": datetime.now(timezone.utc).isoformat()})
            log_event(log, logging.INFO, "tradelog", f"trading journal run {rid} -> {self.tlog.path}", run_id=rid)
        except Exception as exc:
            log_event(log, logging.WARNING, "tradelog_error", f"trading journal run record failed: {exc}")

    def _journal_scan(self, day, feats, by_symbol, plan_results, plans, account, scan_meta) -> None:
        if not self.tlog:
            return
        from .tradelog import rule_checklist
        try:
            equity = Decimal(str(account.get("equity")))
            final = {c["symbol"]: c for c in self.j.candidates(day)}
            for f in feats:
                row = by_symbol.get(f.symbol, {"status": "filtered"})
                fin = final.get(f.symbol, row)
                status, reason = fin.get("status"), fin.get("reason")
                action = "trade" if status in ("submitted", "signal_only") else "skip"
                broker_reason = reason if status == "skipped" and not isinstance(plan_results.get(f.symbol), str) \
                    else None
                o = f.opening
                a = self.prep.assets.get(f.symbol, {})
                self.tlog.candidate(day, {
                    "symbol": f.symbol, "rank": row.get("rank"),
                    "direction": None if o is None else (1 if o.c > o.o else (-1 if o.c < o.o else 0)),
                    "open": o and o.o, "high": o and o.h, "low": o and o.l, "close": o and o.c,
                    "volume": o.v if o else None, "relvol": _q4(f.relvol), "atr": _q4(f.atr),
                    "adv": None if f.adv is None else f.adv.quantize(Decimal("0.01")),
                    "shortable": None if "shortable" not in a else bool(a.get("shortable")),
                    "easy_to_borrow": None if "easy_to_borrow" not in a else bool(a.get("easy_to_borrow")),
                    "rules": rule_checklist(f, row, plan_results.get(f.symbol), broker_reason),
                    "action": action, "skip_reason": None if action == "trade" else (reason or status)})
            for p in plans:
                dist = abs(p.entry_price - p.stop_price)
                risk = dist * p.qty
                self.tlog.plan(day, {
                    "symbol": p.symbol, "direction": p.direction, "entry_trigger": p.entry_price,
                    "planned_stop": p.stop_price, "planned_qty": p.qty, "planned_exposure": p.entry_price * p.qty,
                    "risk_dollars": risk, "risk_pct": (risk / equity * 100).quantize(Decimal("0.0001")),
                    "one_r_per_share": dist, "one_r_dollars": risk,
                    "sizing": {k: str(v) for k, v in {**p.sizing, "atr": p.atr, "raw_entry": p.raw_entry,
                                                        "raw_stop": p.raw_stop}.items()}})
            self.tlog.event(day, "scan", dedupe_key=f"scan:{day}", detail=scan_meta,
                            event_time=self.clock.now().isoformat())
        except Exception as exc:
            log_event(log, logging.WARNING, "tradelog_error", f"trading journal scan record failed: {exc}")

    def _journal_close(self, session: Session) -> None:
        if not self.tlog:
            return
        from .tradelog import build_outcomes, export_all
        day = session.day
        try:
            if self.engine:
                traded = sorted({f["symbol"] for f in self.j.fills(day.isoformat())})
                bars = None
                if traded:
                    try:
                        bars = self.data.minute_bars_iex(traded, session.open, session.close)
                        bars = {s: bars.get(s, []) for s in traded}
                    except Exception as exc:
                        log_event(log, logging.WARNING, "tradelog_error", f"MFE/MAE bars unavailable: {exc}")
                div = self.j.diagnostic(day, "divergence") or {}
                stale = set(div.get("changed", {})) | set(div.get("appeared_late", []))
                build_outcomes(self.j, self.tlog, day, self.settings, bars, self.schedule.scan, stale)
            if self.settings.journal_export:
                paths = export_all(self.tlog, day)
                log_event(log, logging.INFO, "tradelog",
                          f"journal exports for {day}: {paths['summary_md']}",
                          **{k: str(v) for k, v in paths.items()})
        except Exception as exc:
            log_event(log, logging.WARNING, "tradelog_error", f"trading journal close failed: {exc}")

    # ------------------------------------------------------- diagnostics
    def diagnostic(self, session: Session, now: datetime) -> None:
        day = session.day
        try:
            late = self.data.opening_bars(self.prep.symbols, session, purpose="diagnostic:opening_iex")
        except Exception as exc:
            log_event(log, logging.WARNING, "diagnostic_failed", f"09:37 diagnostic failed: {exc}")
            return
        early = self.opening_0935
        if not early:
            saved = self.j.diagnostic(day, "opening_0935") or {}
            from .marketdata import parse_bar
            early = {s: parse_bar({**b, "t": session.open.astimezone(timezone.utc).isoformat()})
                     for s, b in saved.items() if b}
        appeared = sorted(set(late) - set(early))
        vanished = sorted(set(early) - set(late))
        changed = {}
        for s in sorted(set(late) & set(early)):
            a, b = early[s], late[s]
            diff = {k: (str(getattr(a, k)), str(getattr(b, k))) for k in ("o", "h", "l", "c", "v")
                    if getattr(a, k) != getattr(b, k)}
            if diff:
                changed[s] = diff
        top_early = [f.symbol for f in select_candidates(self.features(early))[0]]
        top_late = [f.symbol for f in select_candidates(self.features(late))[0]]
        body = {"appeared_late": appeared, "vanished": vanished, "changed": changed,
                "top20_0935": top_early, "top20_0937": top_late,
                "top20_overlap": len(set(top_early) & set(top_late))}
        self.j.save_diagnostic(day, "opening_0937", {s: _bar_json(b) for s, b in late.items()})
        self.j.save_diagnostic(day, "divergence", body)
        if self.tlog:
            try:
                self.tlog.event(day, "data_divergence", dedupe_key=f"divergence:{day}", detail=body,
                                event_time=self.clock.now().isoformat())
            except Exception as exc:
                log_event(log, logging.WARNING, "tradelog_error", f"trading journal write failed: {exc}")
        log_event(log, logging.INFO, "data_divergence",
                  f"09:35 vs 09:37 IEX opening bars: {len(appeared)} appeared late, {len(changed)} changed, "
                  f"top-20 overlap {body['top20_overlap']}/{max(len(top_early), 1)} (diagnostic only; trades unchanged)",
                  **body)

    # ------------------------------------------------------- shadow feed
    def _feed_symbols(self) -> list[str]:
        syms = set(self.broker.list_positions())
        syms |= {o.symbol for o in self.broker.list_orders("open")}
        return sorted(syms)

    def _shadow_feed(self, now: datetime) -> None:
        # Only the local simulator needs prices (shadow mode, and tests driving it).
        if not hasattr(self.broker, "on_bar") or self.schedule is None:
            return
        s = self.schedule.session
        if not (s.open <= now <= s.close + timedelta(minutes=1)):
            return
        if self.last_feed and (now - self.last_feed).total_seconds() < 60:
            return
        self.last_feed = now
        syms = self._feed_symbols()
        if not syms:
            return
        upto = min(now, s.close).replace(second=0, microsecond=0)
        start = self.feed_cursor or self.schedule.scan.replace(second=0, microsecond=0)
        if self.feed_prefetch:
            need = [x for x in syms if x not in self.feed_bars]
            if need:
                got = self.data.minute_bars_iex(need, s.open, s.close)
                for x in need:
                    self.feed_bars[x] = got.get(x, [])
            batch = {x: [b for b in self.feed_bars.get(x, []) if start <= b.t < upto] for x in syms}
        else:
            if upto <= start:
                return
            try:
                batch = self.data.minute_bars_iex(syms, start, upto)
            except Exception as exc:
                log_event(log, logging.WARNING, "shadow_feed", f"IEX minute bars failed: {exc}")
                return
        events = sorted(((b.t, sym, b) for sym, bars in batch.items() for b in bars
                         if b.t + timedelta(minutes=1) <= upto), key=lambda x: (x[0], x[1]))
        for _, sym, b in events:
            self.broker.on_bar(sym, b)
            self._drain_events(self.clock.now())
        self.feed_cursor = upto

    # ------------------------------------------------------------- loop
    def _drain_events(self, now: datetime) -> None:
        while True:
            try:
                item = self.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(item, OrderUpdate):
                if self.engine and self.engine.day:
                    self.engine.on_update(item)
            elif isinstance(item, tuple) and item[:2] == ("status", "disconnected"):
                self.stream_up = False
            elif isinstance(item, tuple) and item[:2] == ("status", "connected"):
                self.stream_up = True
                if self.engine and self.engine.day:
                    self.engine.reconcile(now, side_effects=True)

    def _poll(self, now: datetime) -> None:
        if not self.engine or not self.engine.day:
            return
        s = self.schedule.session
        after_close_pending = now > s.close and not self.j.is_done(s.day, "post_close")
        if not (s.open - timedelta(minutes=5) <= now <= s.close + timedelta(minutes=5) or after_close_pending):
            return
        eod_wait = self.j.day(s.day)["eod_status"] == "waiting"
        interval = 2 if eod_wait else self.settings.order_poll_seconds
        if self.last_poll and (now - self.last_poll).total_seconds() < interval:
            return
        self.last_poll = now
        try:
            self.engine.reconcile(now, side_effects=True)
        except ConnectionError as exc:
            log_event(log, logging.WARNING, "poll_failed", f"order poll failed: {exc}")

    def _health(self, now: datetime) -> dict:
        """healthy = operating normally; degraded = alive but something needs attention (the reader adds
        stale/dead from heartbeat age and PID)."""
        reasons = []
        if self.engine and self.engine.entries_blocked:
            reasons.append(f"entries_blocked:{self.engine.entries_blocked}")
        if self.stream is not None and getattr(self, "stream_up", None) is False:
            reasons.append("order_stream_down")
        try:
            reasons += [f"breaker:{b[0]}" for b in self.j.breakers()]
        except Exception:
            reasons.append("journal_unreadable")
        return {"health": "degraded" if reasons else "healthy", "health_reasons": reasons}

    def _ops_tick(self, now: datetime) -> None:
        """Heartbeat for the health check (every 30 s) and the disk guard (every 5 min)."""
        from .ops import free_mb, write_heartbeat
        self._note_suspension(now)
        if self._last_disk_check is None or (now - self._last_disk_check).total_seconds() >= 300:
            self._last_disk_check = now
            try:
                free = free_mb(self.settings.data_dir)
            except OSError:
                free = None
            low = free is not None and free < self.settings.min_free_disk_mb
            if self.engine and low and self.engine.entries_blocked is None:
                self.engine.entries_blocked = "disk_low"
                log_event(log, logging.ERROR, "disk_low",
                          f"only {free} MB free under {self.settings.data_dir} (minimum "
                          f"{self.settings.min_free_disk_mb} MB): new entries blocked; stops and exits continue",
                          free_mb=free)
            elif self.engine and not low and self.engine.entries_blocked == "disk_low":
                self.engine.entries_blocked = None
                log_event(log, logging.WARNING, "disk_ok", f"disk space back to {free} MB; entries allowed")
            self._disk_free = free
        if self._last_heartbeat is None or (now - self._last_heartbeat).total_seconds() >= 30:
            self._last_heartbeat = now
            sch = self.schedule
            write_heartbeat(self.settings.data_dir, {
                "mode": self.mode, "phase": sch.phase(now) if sch else None,
                "session": sch.session.day.isoformat() if sch else None,
                "journal_path": self.j.path,
                "bot_time": now.isoformat(), "last_data_update": self.last_data_update,
                "scan_status": self.j.day(sch.session.day)["scan_status"] if sch else None,
                "entries_blocked": self.engine.entries_blocked if self.engine else None,
                "disk_free_mb": getattr(self, "_disk_free", None), **self._health(now)})

    def _note_suspension(self, now: datetime) -> None:
        """The loop ticks at least every 60 s. If wall time moved much more than the monotonic clock, the whole
        process was suspended (host sleep), so everything scheduled in that gap was slept through."""
        if self.mono is None:
            return                      # fake clocks (tests, replays) have no host to suspend
        mono = self.mono()
        if self._tick_wall is not None:
            slept = (now - self._tick_wall).total_seconds() - (mono - self._tick_mono)
            if slept >= SUSPEND_SECONDS:
                self.suspensions.append((self._tick_wall, now))
                log_event(log, logging.WARNING, "host_suspended",
                          f"the process was suspended for ~{int(slept)}s ({self._tick_wall.astimezone(NY):%H:%M:%S} -> "
                          f"{now.astimezone(NY):%H:%M:%S} ET), most likely host sleep; scheduled work in that gap "
                          "was missed", suspended_s=int(slept))
        self._tick_wall, self._tick_mono = now, mono

    def _scan_missed_reason(self, sch) -> str:
        if any(a < sch.scan_deadline and b > sch.scan for a, b in self.suspensions):
            return "the process was suspended (host sleep) across the scan window"
        if self.scan_defer_reason:
            return f"the scan could not run before its deadline: {self.scan_defer_reason}"
        return "the bot was not running"

    def step(self) -> float:
        now = self.clock.now()
        self._ops_tick(now)
        self._drain_events(now)
        session = self._session_for(now)
        if session is None:
            self.calendar = None
            return 300.0
        if self.schedule is None or self.schedule.session.day != session.day:
            self._start_session(session, now)
        sch, day, j = self.schedule, session.day, self.j
        if now < sch.scan:
            self._ensure_prep(session, now)
        if sch.open_sweep <= now < sch.scan and not j.is_done(day, "open_sweep"):
            if self.engine:
                self.engine.sweep("opensweep", "Open sweep")
            j.mark_done(day, "open_sweep")
        st = j.day(day)
        if not st["scanned"]:
            if sch.scan <= now < sch.scan_deadline:
                self.scan(session, now)
            elif now >= sch.scan_deadline:
                j.mark_scanned(day, "missed")
                log_event(log, logging.WARNING, "scan_missed",
                          f"scan window for {day} passed: {self._scan_missed_reason(sch)}; no entries today")
        st = j.day(day)
        if (st["scan_status"] == "ok" and sch.diagnostic <= now < sch.eod_exit
                and not j.is_done(day, "diagnostic") and self.prep):
            self.diagnostic(session, now)
            j.mark_done(day, "diagnostic")
        if sch.eod_exit <= now < session.close and st["eod_status"] is None:
            if self.engine:
                self.engine.begin_eod(now)
            else:
                j.set_eod(day, "liquidated", now.isoformat())
                j.set_session_closing(day, True)
                j.mark_done(day, "eod")
        self._shadow_feed(now)
        self._poll(now)
        if self.engine:
            self.engine.tick(now)
        if sch.close_sweep <= now < session.close and not j.is_done(day, "close_sweep"):
            if self.engine:
                self.engine.sweep("closesweep", "Close sweep")
            j.mark_done(day, "close_sweep")
        if now >= session.close and not j.is_done(day, "post_close"):
            if self.engine:
                if not self.engine.post_close_step(now):
                    return 3.0               # ORB-IEX is not verifiably flat yet: keep working on it
                self.engine.post_close(now)
            else:
                log_event(log, logging.INFO, "post_close", f"signal mode: session {day} done")
            self._journal_close(session)
            j.mark_done(day, "post_close")
            return 1.0
        return self._next_wait(now)

    def _next_wait(self, now: datetime) -> float:
        sch = self.schedule
        s = sch.session
        if s.open - timedelta(minutes=2) <= now <= s.close + timedelta(minutes=1):
            # wake for the next scheduled event, poll or feed tick; at most 5 s apart
            points = [p for p in (sch.open_sweep, sch.scan, sch.diagnostic, sch.entry_cutoff, sch.eod_exit,
                                  sch.close_sweep, s.close) if p > now]
            if self.engine and self.last_poll:
                eod_wait = self.j.day(s.day)["eod_status"] == "waiting"
                points.append(self.last_poll + timedelta(
                    seconds=2 if eod_wait else self.settings.order_poll_seconds))
            if hasattr(self.broker, "on_bar") and self.last_feed:
                points.append(self.last_feed + timedelta(seconds=60))
            wait = min([5.0] + [(p - now).total_seconds() for p in points])
            return max(0.2, wait)
        points = [p for p in (s.open - timedelta(minutes=2), self.prep_ready_time(s)) if p > now]
        return max(1.0, min([60.0] + [(p - now).total_seconds() for p in points]))

    def _sleep(self, wait: float) -> None:
        """Real clock: wake as soon as the order stream delivers an event (a fill needs its protective
        stop within milliseconds, not at the next 5 s tick). Fake clock: just advance time."""
        if isinstance(self.clock, SystemClock):
            q = self.events
            with q.not_empty:
                if not q.queue:
                    q.not_empty.wait(wait)
            return
        self.clock.sleep(wait)

    # ------------------------------------------------------------ fatal error
    def _fatal_shutdown(self, exc: BaseException) -> dict:
        """Last act of a process that is dying on an unexpected exception. Every step is independent and none
        may raise: log CRITICAL with the traceback, block new entries, ONE broker reconciliation, protect or
        flatten every ORB-IEX position, persist the failure. Returns the report that was persisted."""
        try:
            now = self.clock.now()
        except Exception:
            now = datetime.now(timezone.utc)
        sch = self.schedule
        report: dict = {"at": now.isoformat(), "error": f"{type(exc).__name__}: {exc}",
                        "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                        "mode": self.mode, "pid": os.getpid(),
                        "session": sch.session.day.isoformat() if sch else None,
                        "phase": None, "entries_blocked": False, "protect": None, "persisted": []}
        try:
            report["phase"] = sch.phase(now) if sch else None
        except Exception:
            pass
        try:
            log.log(logging.CRITICAL, f"FATAL: {report['error']}; blocking new entries, reconciling once, "
                    "protecting or flattening every ORB-IEX position, then exiting non-zero",
                    exc_info=(type(exc), exc, exc.__traceback__),
                    extra={"event": "fatal_error", "fields": {"error": report["error"], "mode": self.mode,
                                                              "phase": report["phase"]}})
        except Exception:
            pass
        if self.engine is not None:
            try:                                           # first thing, before any network call
                self.engine.entries_blocked = "fatal_error"
                report["entries_blocked"] = True
            except Exception:
                pass
            if self.broker is not None:
                try:
                    if isinstance(exc, sqlite3.Error):
                        report['protect'] = {'emergency_flat': self.engine.emergency_shutdown()}
                    else:
                        report["protect"] = self.engine.fatal_protect(now)
                except Exception as inner:                 # fatal_protect should not raise; belt and braces
                    report["protect"] = {"errors": [f"{type(inner).__name__}: {inner}"]}
        self._persist_fatal(report)
        try:
            log.log(logging.CRITICAL, "FATAL state persisted: " + json.dumps(
                {k: report[k] for k in ("error", "entries_blocked", "protect", "persisted")}, default=str),
                extra={"event": "fatal_state", "fields": {"persisted": report["persisted"]}})
        except Exception:
            pass
        return report

    def _persist_fatal(self, report: dict) -> None:
        body = json.dumps(report, default=str)
        try:                                               # 1. the SQLite journal
            self.j.set_meta(FATAL_META_KEY, body)
            report["persisted"].append("journal")
        except Exception:
            pass
        try:                                               # 2. a plain file, independent of SQLite
            from pathlib import Path
            path = Path(self.settings.data_dir) / FATAL_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(report, default=str))
            os.replace(tmp, path)
            report["persisted"].append("file")
        except Exception:
            pass
        try:                                               # 3. the trading journal
            if self.tlog is not None:
                self.tlog.event(report["session"] or "-", "fatal_error", None,
                                detail={k: report[k] for k in ("error", "phase", "entries_blocked", "protect")},
                                event_time=report["at"])
                report["persisted"].append("tradelog")
        except Exception:
            pass
        try:                                               # 4. the heartbeat (health shows it)
            from .ops import write_heartbeat
            write_heartbeat(self.settings.data_dir, {"mode": self.mode, "fatal": report["error"],
                                                     "entries_blocked": "fatal_error",
                                                     "bot_time": report["at"]})
        except Exception:
            pass

    def _note_previous_fatal(self) -> None:
        """A previous process ended on a fatal error: say so loudly. Recovery (not this note) decides what to do
        with positions; entries are NOT blocked just because the last run died (a transient error must not cost
        the whole day), the scan is single-shot and the cutoff and recovery rules still apply."""
        try:
            raw = self.j.get_meta(FATAL_META_KEY)
            if not raw:
                return
            prev = json.loads(raw)
            log_event(log, logging.CRITICAL, "previous_fatal",
                      f"the previous process ended on a FATAL error at {prev.get('at')}: {prev.get('error')}; "
                      f"protect/flatten outcome: {json.dumps(prev.get('protect'), default=str)}; recovery follows",
                      at=prev.get("at"), error=prev.get("error"))
            if self.tlog is not None and prev.get("session"):
                self.tlog.event(prev["session"], "previous_fatal", None, dedupe_key=f"previous_fatal:{prev.get('at')}",
                                detail={"error": prev.get("error"), "at": prev.get("at")})
        except Exception as exc:
            log_event(log, logging.WARNING, "previous_fatal", f"could not read the previous fatal record: {exc}")

    def _graceful_shutdown(self, reason: str) -> dict | None:
        if self.engine is None or self.broker is None:
            return None                                    # signal mode: no orders exist
        try:
            report = self.engine.graceful_shutdown(reason)
        except BaseException as exc:                       # cleanup itself failed: use independent fatal recovery
            log_event(log, logging.CRITICAL, "shutdown_incomplete", f"shutdown handler failed: {type(exc).__name__}: {exc}")
            report = {"complete": False, "reason": reason,
                      "errors": [f"shutdown handler failed: {type(exc).__name__}: {exc}"]}
            self.engine.entries_blocked = 'shutdown_incomplete'
            try:
                self.j.set_breaker('shutdown_incomplete', detail=report['errors'][0])
            except Exception:
                pass                                      # fatal recovery has independent file persistence
            report['fatal_recovery'] = self._fatal_shutdown(exc)
        try:
            from pathlib import Path
            path = Path(self.settings.data_dir) / SHUTDOWN_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(report, default=str))
            os.replace(tmp, path)
        except OSError:
            pass
        if not report.get('complete'):
            raise FatalRunnerError(RuntimeError('shutdown incomplete; broker recovery required'), report)
        return report

    def run(self, until: datetime | None = None) -> None:
        if self.mode != 'paper':
            return self._run(until)
        import fcntl
        # ponytail: journal-scoped lock; account ownership remains enforced by startup configuration.
        with open(self.j.path + '.run.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SafetyError('another paper runner owns this journal') from exc
            return self._run(until)

    def _run(self, until: datetime | None = None) -> None:
        self._start_journal_run()
        self._note_previous_fatal()
        if self.stream:
            self.stream.start()
        try:
            while until is None or self.clock.now() < until:
                wait = self.step()
                if until is not None:
                    wait = min(wait, max((until - self.clock.now()).total_seconds(), 0.001))
                self._sleep(wait)
        except KeyboardInterrupt:
            # Ctrl+C/SIGTERM are clean only after reconciliation; unresolved cleanup exits as fatal.
            self._graceful_shutdown("ctrl_c_or_sigterm")
            raise
        except Exception as exc:
            # `except Exception` does not catch KeyboardInterrupt / SystemExit.
            raise FatalRunnerError(exc, self._fatal_shutdown(exc)) from exc
        else:
            if self.mode == 'paper':
                self._graceful_shutdown('normal_return')
        finally:
            if self.stream:
                self.stream.stop()
