"""Forward SHADOW collector for the 10:00 AM gap-up strategies (Top-10 and Top-5).

Rules are copied from the committed research (orb-strategy-lab d1ce4eb, run_oct2026.py STRATS/BENCH):
long only; rank <= N (and rvol >= R) in the bot's own decision-time ranking; open gap >= 100 bps over the
prior close; prior close above the 20-session SMA; stop = opening-range low; target = range high + 2 x range;
enter at 10:00; exit at 15:55. Top-10: rvol >= 1. Top-5: rvol >= 4.

Deviations from the research, forced by decision-time data (docs/GAPUP-SHADOW.md): ranking, opening bar, open and
range come from the bot's free IEX opening bar (research used SIP); fills use observed IEX quotes (buy ask, sell bid)
instead of 1-minute bar prices; sizing is the bot's own size_position.

No broker order client exists here: the data client is GET-only and fills come from ForwardShadowBroker, a local
quote simulator. Candidate B, the frozen baseline and their provenance are never read for writing or modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import queue
import sqlite3
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import constants as C
from .calendar import NY
from .journal import Journal, OrderRec
from .orders import TERMINAL, OrderRequest, client_order_id, parse_client_order_id
from .shadow_pair import CHECKPOINT, decode, dumps, encode, restore_broker
from .shadow_session import ForwardShadowBroker
from .strategy import size_position
from .history_safety import unexplained_discontinuity

log = logging.getLogger("orb_iex")

VERSION = "gapup-10am-v1"
GAP_BPS = Decimal(100)
TARGET_MULT = Decimal(2)
MA_SESSIONS = 20
MAX_SPREAD_PCT = Decimal("0.5")          # 50 bps, the research quote rule (stricter than the bot's 1%)
ENTRY_AT, EXIT_AT = dtime(10, 0), dtime(15, 55)
DECIDE_FROM = dtime(9, 37)
ENTRY_WINDOW_S = 120
PORTFOLIOS = {"gapup_top10": {"max_rank": 10, "min_relvol": Decimal(1)},
              "gapup_top5": {"max_rank": 5, "min_relvol": Decimal(4)}}
UPSTREAM = "baseline_a.sqlite"           # recorded decision-time ranking written by the existing collector
ROOT_NAME = "gapup_v1"


# ---------------------------------------------------------------- pure rules (unit tested)
def decide_symbol(row: dict, closes: list[Decimal] | None, max_rank: int, min_relvol: Decimal) -> dict:
    """One candidate row from the recorded ranking -> eligibility with the exact reason. No future data."""
    d, rank = row["details"], row.get("rank")
    out = {"symbol": row["symbol"], "rank": rank, "eligible": False, "reason": None}
    if rank is None or rank > max_rank:
        out["reason"] = f"rank>{max_rank}"
        return out
    try:
        relvol, o, hi, lo = (Decimal(str(d[k])) for k in ("relvol", "open", "high", "low"))
        opening_close = Decimal(str(d["close"]))
    except (KeyError, TypeError, ArithmeticError):
        out["reason"] = "missing_opening_fields"
        return out
    out.update(relvol=str(relvol), open=str(o), high=str(hi), low=str(lo), opening_close=str(opening_close))
    if relvol < min_relvol:
        out["reason"] = f"relvol<{min_relvol}"
    elif not o > C.MIN_PRICE:
        out["reason"] = "open<=5"
    elif closes is None or len(closes) != MA_SESSIONS:
        out["reason"] = "ma20_history_incomplete"
    else:
        prev, ma = closes[-1], sum(closes) / MA_SESSIONS
        gap = (o / prev - 1) * 10000
        out.update(prev_close=str(prev), ma20=str(ma), gap_bps=str(gap))
        if gap < GAP_BPS:
            out["reason"] = "gap<100bps"
        elif not prev > ma:
            out["reason"] = "prior_close_not_above_ma20"
        elif not hi - lo > 0:
            out["reason"] = "flat_opening_range"
        else:
            out.update(eligible=True, stop=str(lo), target=str(hi + TARGET_MULT * (hi - lo)),
                       range_size=str(hi - lo))
    return out


def entry_check(ask: Decimal, stop: Decimal, target: Decimal) -> str | None:
    if ask <= stop:
        return "stop_breached_at_entry"
    if ask >= target:
        return "target_passed_at_entry"
    return None


def exit_role(bid: Decimal, stop: Decimal, target: Decimal, local_t: dtime) -> str | None:
    if local_t >= EXIT_AT:
        return "eod"
    if bid <= stop:
        return "stop"
    if bid >= target:
        return "tgt"
    return None


def exit_time(close: datetime | None) -> dtime:
    """15:55, or five minutes before an early close."""
    return EXIT_AT if close is None else min(EXIT_AT, (close - timedelta(minutes=5)).time())


def moment(day: date, t: dtime) -> datetime:
    return datetime.combine(day, t, tzinfo=NY)


# ---------------------------------------------------------------- one independent portfolio
class TickClock:
    def __init__(self):
        self.current = datetime.now(timezone.utc)

    def now(self):
        return self.current


class Portfolio:
    def __init__(self, name, spec, path, settings, equity, clock):
        self.name, self.spec, self.settings, self.equity, self.clock = name, spec, settings, equity, clock
        self.close = None                                   # set by the collector each tick (session close, for early closes)
        self.j = Journal(path)
        self.events = queue.Queue()
        self.broker = ForwardShadowBroker(self.events.put, equity=equity, clock=clock.now,
                                          max_age=settings.max_quote_age_seconds, max_spread=MAX_SPREAD_PCT)
        restore_broker(self.j, self.broker)

    # -- persistence helpers
    def state(self, day):
        return self.j.diagnostic(day, "gapup_state") or {"decided": False, "entries": {}, "exits": {}, "quality": {}}

    def drain(self, day):
        while not self.events.empty():
            u = self.events.get()
            parsed = parse_client_order_id(u.client_order_id)
            odate, role = (parsed[0], parsed[2]) if parsed else (day, "unknown")
            rec = self.j.order(u.order_id) or OrderRec(
                order_id=u.order_id, client_order_id=u.client_order_id, day=odate.isoformat(), symbol=u.symbol,
                role=role, side=u.side, type=u.type, qty=u.qty, stop_price=None, status=u.status)
            old_cum, old_value = rec.filled_qty, (rec.filled_avg_price or Decimal(0)) * rec.filled_qty
            rec.status = u.status
            if u.filled_qty > old_cum and u.filled_avg_price is not None:
                delta = u.filled_qty - old_cum
                price = (u.filled_avg_price * u.filled_qty - old_value) / delta
                rec.filled_qty, rec.filled_avg_price = u.filled_qty, u.filled_avg_price
                self.j.add_fill(rec, u.filled_qty, delta, price, u.timestamp,
                                delta * self.settings.commission_per_share, delta * self.settings.slippage_per_share)
            self.j.save_order(rec)

    # -- the daily decision (inputs were recorded first; this is deterministic given them)
    def record_decision(self, day, inputs):
        st = self.state(day)
        if st["decided"]:
            return
        rows, hist = inputs["candidates"], inputs["history"]
        decisions = []
        for r in rows:
            closes = hist.get(r["symbol"])
            closes = [Decimal(x) for x in closes] if closes is not None else None
            decisions.append(decide_symbol(r, closes, self.spec["max_rank"], self.spec["min_relvol"]))
        st.update(decided=True, inputs_ok=inputs["ok"], reason=inputs.get("reason"),
                  decisions={d["symbol"]: d for d in decisions},
                  upstream_scan_at=inputs.get("upstream_scan_at"), read_at=inputs["read_at"])
        st["eligible"] = [d["symbol"] for d in decisions if d["eligible"]] if inputs["ok"] else []
        self.j.save_diagnostic(day, "gapup_state", st)
        self.j.save_candidates(day, [{"symbol": d["symbol"], "rank": d["rank"],
                                      "status": "eligible" if d["eligible"] else "filtered",
                                      "reason": d["reason"], "details": d} for d in decisions])
        self.j.save_diagnostic(day, "ab_plans", {d["symbol"]: d for d in decisions if d["eligible"]})
        self.j.mark_scanned(day, "ok" if inputs["ok"] else "failed")

    # -- one tick
    def step(self, tick, quotes, now):
        day = now.astimezone(NY).date()
        local = now.astimezone(NY)
        st = self.state(day)
        for sym in sorted(self.watch(day)):
            q = quotes.get(sym, "absent")
            if q == "absent":
                continue
            self.broker.on_quote(sym, q)
            reason = self.broker.quote_decisions.get(sym) or "no_quote"
            st["quality"][reason] = st["quality"].get(reason, 0) + 1
        self.drain(day)
        if st["decided"] and st.get("inputs_ok"):
            self.manage(day, local, st, now)
            self.drain(day)
        self.j.save_diagnostic(day, "gapup_state", st)
        self.j.set_meta(CHECKPOINT, dumps({"version": 1, "tick": tick, "at": now.isoformat(),
                                           "broker": self.broker.checkpoint()}))

    def watch(self, day):
        """Symbols that still need quotes: open positions, undecided eligible names, entries with a live order."""
        st = self.state(day)
        out = set(self.broker.list_positions())
        for sym in st.get("eligible", []):
            e = st["entries"].get(sym)
            if e is None or (e["status"] == "submitted" and not e.get("closed_window")):
                out.add(sym)
        return out

    def fresh(self, sym, now):
        q = self.broker.quotes.get(sym)
        if q is None or q.t is None or (now - q.t).total_seconds() > self.settings.max_quote_age_seconds:
            return None
        return q

    def manage(self, day, local, st, now):
        t, exit_at = local.time(), exit_time(self.close)
        window_end = moment(day, ENTRY_AT) + timedelta(seconds=ENTRY_WINDOW_S)
        if local >= moment(day, ENTRY_AT):
            for sym in st["eligible"]:
                e = st["entries"].get(sym)
                if e is not None:
                    continue
                d = st["decisions"][sym]
                if local >= window_end:
                    st["entries"][sym] = {"status": "rejected", "reason": "no_valid_quote_in_window"}
                    self.j.update_candidate(day, sym, "rejected", "no_valid_quote_in_window")
                    continue
                q = self.fresh(sym, now)
                if q is None:
                    continue
                bad = entry_check(q.ask, Decimal(d["stop"]), Decimal(d["target"]))
                qty, sizing = (0, {}) if bad else size_position(
                    self.equity, self.settings.max_gross_exposure, q.ask, q.ask - Decimal(d["stop"]),
                    Decimal(d["opening_close"]), self.settings.commission_per_share)
                if bad or qty <= 0:
                    why = bad or "zero_quantity"
                    st["entries"][sym] = {"status": "rejected", "reason": why, "ask": str(q.ask)}
                    self.j.update_candidate(day, sym, "rejected", why)
                    continue
                cid = client_order_id(day, sym, "entry")
                u = self.broker.submit_order(OrderRequest(sym, "buy", Decimal(qty), "market", cid))
                st["entries"][sym] = {"status": "submitted", "order_id": u.order_id, "qty": qty,
                                      "ask_at_decision": str(q.ask), "sizing": {k: str(v) for k, v in sizing.items()},
                                      "decided_at": now.isoformat()}
                self.j.update_candidate(day, sym, "submitted")
            if local >= window_end:
                for sym, e in st["entries"].items():
                    if e["status"] == "submitted" and not e.get("closed_window"):
                        o = self.broker.orders[e["order_id"]]
                        if o.status not in TERMINAL:
                            self.broker.cancel_order(o.order_id)
                        e["closed_window"] = True
                        e["filled_qty"] = str(o.filled_qty)
        for sym, pos in list(self.broker.list_positions().items()):
            live = st["exits"].get(sym)
            if live and self.broker.orders[live["order_id"]].status not in ("filled", "canceled", "rejected"):
                continue
            d = st["decisions"].get(sym)
            q = self.fresh(sym, now)
            if d is None:                                   # carried from an earlier day: flatten at the first valid quote
                role = "eod"
            else:
                role = "eod" if t >= exit_at else (q and exit_role(q.bid, Decimal(d["stop"]), Decimal(d["target"]), t))
            if not role or q is None:
                continue                                    # no fresh valid quote: nothing is invented, retried next tick
            seq = (live["seq"] + 1) if live else 0
            cid = client_order_id(day, sym, role, seq)
            u = self.broker.submit_order(OrderRequest(sym, "sell", Decimal(pos), "market", cid))
            st["exits"][sym] = {"order_id": u.order_id, "role": role, "seq": seq, "at": now.isoformat(),
                                "trigger_bid": str(q.bid) if q else None}

    def finalize(self, day):
        st = self.state(day)
        open_pos = {s: str(q) for s, q in self.broker.list_positions().items()}
        st["finalized"] = True
        st["unresolved_open"] = open_pos
        self.j.save_diagnostic(day, "gapup_state", st)
        if not open_pos:
            self.j.mark_done(day, "post_close")
        return open_pos


# ---------------------------------------------------------------- shared inputs and the loop
def read_upstream(paired: Path, day: date, max_rank=20):
    """Recorded decision-time ranking from the existing collector (read-only). None until its scan finished OK."""
    path = paired / UPSTREAM
    if not path.is_file():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        scan = db.execute("SELECT scan_status FROM days WHERE day=?", (day.isoformat(),)).fetchone()
        if not scan or scan[0] != "ok":
            return None
        rows = [{"symbol": s, "rank": r, "details": json.loads(d)} for s, r, d in db.execute(
            "SELECT symbol,rank,details FROM candidates WHERE day=? AND rank IS NOT NULL AND rank<=? ORDER BY rank",
            (day.isoformat(), max_rank))]
        plans = db.execute("SELECT body FROM diagnostics WHERE day=? AND kind='ab_plans'", (day.isoformat(),)).fetchone()
    scan_at = None
    if plans:
        times = [v.get("decision_time") for v in json.loads(plans[0]).values() if isinstance(v, dict)]
        scan_at = min([t for t in times if t], default=None)
    return {"rows": rows, "upstream_scan_at": scan_at}


def build_inputs(paired, data, calendar, day, now):
    up = read_upstream(paired, day)
    if up is None:
        return None
    top = [r for r in up["rows"] if r["rank"] <= max(p["max_rank"] for p in PORTFOLIOS.values())]
    sessions = calendar.sessions_before(day, MA_SESSIONS)
    hist, error = {}, None
    if len(sessions) == MA_SESSIONS and top:
        try:
            bars = data.rth_daily_bars([r["symbol"] for r in top], sessions)
            for r in top:
                days = bars.get(r["symbol"], {})
                if all(s.day in days for s in sessions) and unexplained_discontinuity({s.day: days[s.day] for s in sessions}) is None:
                    hist[r["symbol"]] = [str(days[s.day].c) for s in sessions]
        except Exception as exc:                            # retried by the caller until the entry time
            error = f"{type(exc).__name__}: {exc}"
    elif len(sessions) < MA_SESSIONS:
        error = "calendar_too_short"
    return {"ok": error is None, "reason": error, "candidates": up["rows"], "history": hist,
            "upstream_scan_at": up["upstream_scan_at"], "read_at": now.isoformat()}


def bind_provenance(obs: Journal, settings, equity):
    source = Path(__file__).read_bytes()
    contract = {"version": VERSION, "sha256": hashlib.sha256(source).hexdigest(),
                "rules": {"gap_bps": str(GAP_BPS), "target_mult": str(TARGET_MULT), "ma": MA_SESSIONS,
                          "spread_pct": str(MAX_SPREAD_PCT), "entry": "10:00", "exit": "15:55",
                          "portfolios": {k: {"max_rank": v["max_rank"], "min_relvol": str(v["min_relvol"])}
                                         for k, v in PORTFOLIOS.items()}},
                "settings": {k: str(getattr(settings, k)) for k in
                             ("max_gross_exposure", "commission_per_share", "slippage_per_share",
                              "max_quote_age_seconds")}}
    prev = obs.get_meta("provenance")
    if prev:
        prev = json.loads(prev)
        if prev["contract"] != json.loads(dumps(contract)):
            raise ValueError("gap-up SHADOW mechanism changed after freeze; preserve the ledger and register a new version")
        return Decimal(prev["initial_equity"])
    obs.set_meta("provenance", dumps({"contract": contract, "frozen_at": datetime.now(timezone.utc).isoformat(),
                                      "initial_equity": str(equity), "source_research": "orb-strategy-lab d1ce4eb",
                                      "qualification": "UNQUALIFIED; hypothetical; separate approval required"}))
    return equity


def phase_wait(local: datetime, day_session, active: bool) -> float:
    """Seconds to sleep. Fast only when a decision, entry, open position or exit needs watching."""
    if day_session is None or local >= day_session.close + timedelta(minutes=15):
        return 60.0
    t = local.time()
    if t < DECIDE_FROM:
        return min(60.0, max(1.0, (moment(local.date(), DECIDE_FROM) - local).total_seconds()))
    if t < ENTRY_AT:
        return 5.0
    if t < dtime(10, 2, 30) or t >= exit_time(day_session.close):
        return 2.0
    return 3.0 if active else 20.0


def main():
    from . import cli
    from .http import ReadOnlyClient
    from .marketdata import BarCache, MarketData, load_calendar
    from .ops import InstanceLock, check_disk, write_heartbeat
    from .preflight import verify_environment
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/candidate_b.toml")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()
    args.require_private_env = True
    settings, credentials, _ = cli._prepare(args)
    if settings.enable_paper_orders:
        parser.error("gap-up SHADOW requires a configuration with paper orders disabled")
    base = Path(settings.data_dir)
    paired = base / "paired-v1"
    root = base.parent / ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    check_disk(root, settings.min_free_disk_mb)
    lock = InstanceLock(root).acquire()
    cli._graceful_sigterm()
    try:
        client = ReadOnlyClient(credentials, settings.trading_calls_per_minute, min(40, settings.market_data_calls_per_minute),
                                timeout_seconds=settings.request_timeout_seconds)
        summary = verify_environment(client, settings, "shadow")
        obs = Journal(root / "observations.sqlite")
        equity = bind_provenance(obs, settings, Decimal(summary["equity"]))
        data = MarketData(client, settings, BarCache(root / "cache.sqlite"))
        clock = TickClock()
        ports = [Portfolio(n, spec, root / f"{n}.sqlite", settings, equity, clock) for n, spec in PORTFOLIOS.items()]
        calendar_day, calendar = None, None
        while True:
            now = datetime.now(timezone.utc)
            local = now.astimezone(NY)
            if calendar_day != local.date():
                calendar = load_calendar(client, local.date(), back_days=60)
                calendar_day = local.date()
            pending = obs.get_meta("tick")
            old = json.loads(pending) if pending else None
            if old and not old["complete"]:
                tick = old                                  # crash recovery: reuse the recorded observation
            else:
                tick = {"id": (old["id"] + 1 if old else 1), "at": now.isoformat(), "complete": False}
            day = datetime.fromisoformat(tick["at"]).astimezone(NY).date()
            sess = calendar.session(day)
            t_local = datetime.fromisoformat(tick["at"]).astimezone(NY)
            active = False
            if sess and DECIDE_FROM <= t_local.time() and t_local < sess.close + timedelta(minutes=15):
                rec = obs.diagnostic(day, f"tick:{tick['id']}")
                if rec is None:
                    states = [p.state(day) for p in ports]
                    inputs = None
                    if not all(s["decided"] for s in states):
                        if t_local < moment(day, ENTRY_AT):
                            inputs = build_inputs(paired, data, calendar, day, datetime.now(timezone.utc))
                        else:   # never decide after the entry time: that would use data not available at 10:00
                            inputs = {"ok": False, "reason": "no_decision_before_entry_time", "candidates": [],
                                      "history": {}, "upstream_scan_at": None, "read_at": now.isoformat()}
                        if inputs is not None and not inputs["ok"] and t_local < moment(day, dtime(9, 59, 30)):
                            inputs = None                   # transient history error: retry before 10:00
                    symbols = sorted({s for p in ports for s in p.watch(day)})
                    quotes, qerr = {}, None
                    if symbols and (t_local.time() >= ENTRY_AT or any(p.broker.list_positions() for p in ports)):
                        try:
                            quotes = data.entry_quotes(symbols, datetime.now(timezone.utc)) if symbols else {}
                        except Exception as exc:
                            qerr = f"{type(exc).__name__}: {exc}"
                    fetched = bool(symbols) and (t_local.time() >= ENTRY_AT or any(p.broker.list_positions() for p in ports))
                    rec = {"received_at": datetime.now(timezone.utc).isoformat(), "inputs": inputs,
                           "quotes": {s: encode(q) for s, q in quotes.items()},
                           "symbols": symbols if fetched else [], "quote_error": qerr}
                    obs.save_diagnostic(day, f"tick:{tick['id']}", rec)
                obs.set_meta("tick", dumps(tick))
                got_at = datetime.fromisoformat(rec["received_at"])
                clock.current = max(clock.current, got_at)
                quotes = {s: decode(v) for s, v in rec["quotes"].items()}
                for s in rec["symbols"]:
                    quotes.setdefault(s, None)
                for p in ports:
                    p.close = sess.close
                    prev = p.j.get_meta(CHECKPOINT)
                    if prev and json.loads(prev)["tick"] >= tick["id"]:
                        continue
                    with p.j.transaction():
                        if rec["inputs"] is not None:
                            p.record_decision(day, rec["inputs"])
                        p.step(tick["id"], quotes, got_at)
                    active = active or bool(p.watch(day))
                if t_local >= sess.close + timedelta(minutes=5):
                    for p in ports:
                        if not p.state(day).get("finalized"):
                            with p.j.transaction():
                                p.finalize(day)
                    ledger_session(obs, ports, day, got_at)
            tick["complete"] = True
            obs.set_meta("tick", dumps(tick))
            write_heartbeat(root, {"mode": "gapup_shadow", "version": VERSION, "bot_time": datetime.now(timezone.utc).isoformat(),
                                   "tick": tick["id"], "session": str(day) if sess else None, "health": "healthy",
                                   "health_reasons": [], "portfolios": list(PORTFOLIOS)})
            time.sleep(phase_wait(datetime.now(timezone.utc).astimezone(NY), sess, active))
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()


def ledger_session(obs: Journal, ports, day: date, now: datetime):
    """One durable record per session. INCOMPLETE if inputs/quotes were missing or exposure stayed open."""
    if obs.diagnostic(day, "gapup_session"):
        return
    gaps, status = [], "COMPLETE"
    for p in ports:
        st = p.state(day)
        if not st.get("inputs_ok"):
            gaps.append({"portfolio": p.name, "reason": st.get("reason") or "no_inputs"})
        for sym, e in st.get("entries", {}).items():
            if e.get("reason") == "no_valid_quote_in_window":
                gaps.append({"portfolio": p.name, "symbol": sym, "reason": e["reason"]})
        if st.get("unresolved_open"):
            gaps.append({"portfolio": p.name, "reason": "unresolved_open_exposure", "positions": st["unresolved_open"]})
    if gaps:
        status = "INCOMPLETE"
    obs.save_diagnostic(day, "gapup_session", {"day": str(day), "status": status, "data_gaps": gaps,
                                                "observed_at": now.isoformat(),
                                                "evidence": "OBSERVED_IEX_HYPOTHETICAL; no broker fills or depth proof"})


if __name__ == "__main__":
    raise SystemExit(main())
