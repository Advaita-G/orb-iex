"""Trading journal: an append-only record of every run, candidate, plan,
execution event and trade outcome, plus daily/weekly/monthly summaries.

This is a RECORD, not a decision maker. Nothing here feeds back into the
strategy: the engine and strategy never read it.

Storage
  <journal.dir>/trade_journal[_<mode>].sqlite   append-only (UPDATE/DELETE blocked by triggers)
  <journal.dir>/strategy_changes.sqlite          append-only strategy change log
  <journal.dir>/exports/daily/   trades_/candidates_/events_<day>.csv, summary_<day>.md
  <journal.dir>/exports/weekly/  summary_<YYYY>-W<ww>.md
  <journal.dir>/exports/monthly/ summary_<YYYY-MM>.md
Exports are regenerated from the SQLite file; the SQLite file is the source.

Restarts: writes use INSERT OR IGNORE on natural keys, so a restart that sees
the same event again adds nothing (it never updates or deletes).
"""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
import subprocess
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from . import __version__
from . import constants as C

DEVIATION_TAGS = (
    "DATA_STALE", "DATA_MISSING", "ENTRY_LATE", "ENTRY_SLIPPAGE", "PARTIAL_FILL",
    "STOP_REPLACED", "STOP_REJECTED", "EOD_EXIT", "SHORT_UNAVAILABLE", "RECOVERY_EVENT",
    "DUPLICATE_EVENT", "BROKER_REJECT", "MANUAL_INTERFERENCE",
)
# Tags that make execution "not clean" / data "not complete" / rules "not followed".
EXECUTION_TAGS = {"ENTRY_LATE", "ENTRY_SLIPPAGE", "PARTIAL_FILL", "STOP_REJECTED", "BROKER_REJECT",
                  "DUPLICATE_EVENT", "RECOVERY_EVENT"}
DATA_TAGS = {"DATA_STALE", "DATA_MISSING"}
RULE_BREAK_TAGS = {"MANUAL_INTERFERENCE"}
# ENTRY_LATE: entry order submitted this long after the scheduled scan.
ENTRY_LATE_SECONDS = 30
# ENTRY_SLIPPAGE: adverse entry slippage above this fraction of 1R.
ENTRY_SLIPPAGE_R = Decimal("0.10")
RELVOL_BUCKETS = ((Decimal(1), Decimal(2)), (Decimal(2), Decimal(3)), (Decimal(3), Decimal(5)),
                  (Decimal(5), Decimal(10)), (Decimal(10), None))

TABLES = ("runs", "candidates", "plans", "exec_events", "outcomes")
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, started_at TEXT, timezone TEXT, mode TEXT,
    git_commit TEXT, bot_version TEXT, config_version TEXT, config TEXT, equity TEXT, cash TEXT,
    bot_positions TEXT, max_gross_exposure TEXT, max_positions INTEGER, per_position_cap TEXT,
    feeds TEXT, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS candidates(day TEXT, symbol TEXT, run_id TEXT, rank INTEGER, direction INTEGER,
    open TEXT, high TEXT, low TEXT, close TEXT, volume INTEGER, relvol TEXT, atr TEXT, adv TEXT,
    shortable INTEGER, easy_to_borrow INTEGER, rules TEXT, action TEXT, skip_reason TEXT, recorded_at TEXT,
    PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS plans(day TEXT, symbol TEXT, run_id TEXT, direction INTEGER, entry_trigger TEXT,
    planned_stop TEXT, planned_qty INTEGER, planned_exposure TEXT, risk_dollars TEXT, risk_pct TEXT,
    one_r_per_share TEXT, one_r_dollars TEXT, sizing TEXT, recorded_at TEXT, PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS exec_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT UNIQUE,
    run_id TEXT, day TEXT, symbol TEXT, kind TEXT, role TEXT, order_type TEXT, side TEXT,
    client_order_id TEXT, order_id TEXT, qty TEXT, price TEXT, cum_qty TEXT, status TEXT,
    detail TEXT, event_time TEXT, recorded_at TEXT);
CREATE TABLE IF NOT EXISTS outcomes(day TEXT, symbol TEXT, run_id TEXT, status TEXT, direction INTEGER,
    entry_trigger TEXT, planned_stop TEXT, actual_stop TEXT, planned_qty INTEGER, filled_qty TEXT,
    entry_time TEXT, entry_price TEXT, entry_slippage_per_share TEXT, exit_time TEXT, exit_price TEXT,
    exit_reason TEXT, exit_slippage_per_share TEXT, high_while_held TEXT, low_while_held TEXT,
    mfe_per_share TEXT, mae_per_share TEXT, mfe_r TEXT, mae_r TEXT, gross_pnl TEXT,
    modeled_commission TEXT, modeled_slippage TEXT, actual_slippage TEXT, net_pnl_paper_costs TEXT,
    net_pnl_modeled TEXT, return_pct TEXT, realized_r TEXT, holding_seconds INTEGER, relvol TEXT,
    rules_followed INTEGER, execution_clean INTEGER, data_complete INTEGER, deviation_tags TEXT,
    detail TEXT, recorded_at TEXT, PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS day_summaries(day TEXT, run_id TEXT, body TEXT, recorded_at TEXT,
    PRIMARY KEY(day, run_id));
"""

CHANGELOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_changes(seq INTEGER PRIMARY KEY AUTOINCREMENT, change_date TEXT,
    git_commit TEXT, rule_changed TEXT, old_value TEXT, new_value TEXT, reason TEXT, test_evidence TEXT,
    recorded_at TEXT);
"""


def journal_paths(journal_dir: str | Path, mode: str) -> tuple[Path, Path]:
    """The trading journal file and its export folder for a mode.

    One definition, used by the writer and by every reader: paper is the unqualified
    ``trade_journal.sqlite``, the replay modes carry their own mode in the name. A
    reader that guesses this name reads the wrong database or nothing at all.
    """
    base = Path(journal_dir)
    if mode == "paper":
        return base / "trade_journal.sqlite", base / "exports"
    return base / f"trade_journal_{mode}.sqlite", base / f"exports_{mode}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, default=str, sort_keys=True)
    return str(v)


def _d(v) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


def _append_only(db: sqlite3.Connection, tables: Iterable[str]) -> None:
    for t in tables:
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")
        db.execute(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
                   f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END")


def git_commit(repo_dir: Path | None = None) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo_dir or Path(__file__).parent,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def config_version(settings) -> tuple[str, dict]:
    """Hash of every strategy-relevant setting (paths excluded). Same hash = same config."""
    body = {k: str(v) for k, v in asdict(settings).items()
            if k not in ("data_dir", "kill_switch_file", "journal_dir", "source")}
    body["strategy_constants"] = {k: str(getattr(C, k)) for k in (
        "MAX_POSITIONS", "LOOKBACK_DAYS", "OPENING_RANGE_MINUTES", "MIN_PRICE", "MIN_ATR",
        "MIN_AVERAGE_VOLUME", "MIN_RELATIVE_VOLUME", "STOP_ATR_FRACTION", "RISK_FRACTION_OF_ALLOCATION")}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return digest, body


class TradeLog:
    def __init__(self, path: str | Path, export_dir: str | Path | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.export_dir = Path(export_dir) if export_dir else self.path.parent / "exports"
        self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        _append_only(self.db, TABLES + ("day_summaries",))
        self.run_id: str | None = None

    # -- writes (INSERT OR IGNORE only) ------------------------------------------
    def _insert(self, table: str, row: dict) -> bool:
        cols = list(row)
        def conv(v):
            if v is None:
                return None
            if isinstance(v, bool):
                return int(v)
            if isinstance(v, int):
                return v
            return _s(v)
        cur = self.db.execute(f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                              [conv(row[c]) for c in cols])
        return cur.rowcount == 1

    def start_run(self, *, mode: str, settings, account: dict, bot_positions: dict, now: datetime,
                  feeds: dict) -> str:
        self.run_id = f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{mode}-{uuid.uuid4().hex[:6]}"
        cver, cbody = config_version(settings)
        self._insert("runs", {
            "run_id": self.run_id, "started_at": now.isoformat(), "timezone": C.MARKET_TZ, "mode": mode,
            "git_commit": git_commit(), "bot_version": __version__, "config_version": cver, "config": cbody,
            "equity": account.get("equity"), "cash": account.get("cash"),
            "bot_positions": {k: str(v) for k, v in bot_positions.items()},
            "max_gross_exposure": settings.max_gross_exposure, "max_positions": C.MAX_POSITIONS,
            "per_position_cap": settings.max_gross_exposure / C.MAX_POSITIONS,
            "feeds": feeds, "recorded_at": _now()})
        return self.run_id

    def candidate(self, day: date, row: dict) -> None:
        self._insert("candidates", {"day": day.isoformat(), "run_id": self.run_id, "recorded_at": _now(), **row})

    def plan(self, day: date, row: dict) -> None:
        self._insert("plans", {"day": day.isoformat(), "run_id": self.run_id, "recorded_at": _now(), **row})

    def event(self, day: date | str, kind: str, symbol: str | None = None, dedupe_key: str | None = None,
              event_time: str | None = None, **fields) -> bool:
        detail = fields.pop("detail", None)
        row = {"dedupe_key": dedupe_key, "run_id": self.run_id, "day": str(day), "symbol": symbol, "kind": kind,
               "event_time": event_time or _now(), "recorded_at": _now(), "detail": detail}
        for k in ("role", "order_type", "side", "client_order_id", "order_id", "qty", "price", "cum_qty", "status"):
            if k in fields:
                row[k] = fields[k]
        return self._insert("exec_events", row)

    def outcome(self, day: date, row: dict) -> bool:
        return self._insert("outcomes", {"day": day.isoformat(), "run_id": self.run_id, "recorded_at": _now(), **row})

    def save_day_summary(self, day: date, body: dict) -> None:
        self._insert("day_summaries", {"day": day.isoformat(), "run_id": self.run_id or "-", "body": body,
                                       "recorded_at": _now()})

    # -- reads ----------------------------------------------------------------
    def rows(self, table: str, where: str = "", args: Iterable = ()) -> list[dict]:
        cur = self.db.execute(f"SELECT * FROM {table} {where}", tuple(args))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def events(self, day: str, symbol: str | None = None) -> list[dict]:
        if symbol:
            return self.rows("exec_events", "WHERE day=? AND symbol=? ORDER BY seq", (day, symbol))
        return self.rows("exec_events", "WHERE day=? ORDER BY seq", (day,))

    def has_outcomes(self, day: date) -> bool:
        return bool(self.db.execute("SELECT 1 FROM outcomes WHERE day=? LIMIT 1", (day.isoformat(),)).fetchone())


class StrategyChangeLog:
    """Append-only log of deliberate rule changes. Written by a person (CLI), never by the bot."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.executescript(CHANGELOG_SCHEMA)
        _append_only(self.db, ("strategy_changes",))

    def add(self, *, change_date: str, git_commit: str, rule_changed: str, old_value: str, new_value: str,
            reason: str, test_evidence: str) -> int:
        cur = self.db.execute("INSERT INTO strategy_changes(change_date, git_commit, rule_changed, old_value, "
                              "new_value, reason, test_evidence, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                              (change_date, git_commit, rule_changed, old_value, new_value, reason,
                               test_evidence, _now()))
        return cur.lastrowid

    def entries(self) -> list[dict]:
        cur = self.db.execute("SELECT * FROM strategy_changes ORDER BY seq")
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Candidate rule checklist
# ---------------------------------------------------------------------------

BROKER_SKIPS = {"disk_low": "disk_space_ok", "already_traded_today": "first_signal_of_the_day", "kill_switch": "kill_switch_off", "existing_position_or_order": "no_existing_position_or_order",
                "short_account_disabled": "short_account_enabled", "not_shortable": "shortable",
                "not_easy_to_borrow": "easy_to_borrow", "buying_power": "buying_power_ok",
                "entry_cutoff": "before_entry_cutoff", "eod_window": "before_eod_window",
                "skip_quote_unavailable": "quote_usable", "skip_spread_too_wide": "spread_within_limit", "stale_quote": "quote_fresh",
                "quote_no_timestamp": "quote_fresh", "quote_from_future": "quote_fresh",
                "crossed_before_submit": "entry_not_crossed", "entry_rejected": "broker_accepted"}


def rule_checklist(features, table_row: dict, plan_or_reason, final_reason: str | None) -> dict:
    """Every entry rule as True/False. None = not evaluated (an earlier rule failed,
    or the rule does not apply, e.g. shortability for a long)."""
    f = features
    o = f.opening
    rules: dict[str, bool | None] = {
        "opening_bar_present": o is not None,
        "relvol_ready": f.relvol is not None,
        "atr_ready": f.atr is not None,
        "adv_ready": f.adv is not None,
        "open_gt_5": None if o is None else o.o > C.MIN_PRICE,
        "relvol_ge_1": None if f.relvol is None else f.relvol >= C.MIN_RELATIVE_VOLUME,
        "atr_gt_0_50": None if f.atr is None else f.atr > C.MIN_ATR,
        "adv_ge_1m": None if f.adv is None else f.adv >= C.MIN_AVERAGE_VOLUME,
    }
    status = table_row.get("status")
    rules["in_top20"] = None if status == "filtered" else status != "below_top20"
    for k in ("not_flat_bar", "stop_distance_positive", "qty_positive") + tuple(BROKER_SKIPS.values()):
        rules[k] = None
    if not rules["in_top20"]:
        return rules
    reason = plan_or_reason if isinstance(plan_or_reason, str) else None
    rules["not_flat_bar"] = reason != "flat_bar"
    if not rules["not_flat_bar"]:
        return rules
    rules["stop_distance_positive"] = reason != "zero_stop_distance"
    if not rules["stop_distance_positive"]:
        return rules
    rules["qty_positive"] = reason != "zero_quantity"
    if reason is not None:
        return rules
    # Reached the broker checks, in engine order; the first failing one is the skip reason.
    failed = BROKER_SKIPS.get(final_reason or "")
    is_long = getattr(plan_or_reason, "direction", 1) > 0
    for k in dict.fromkeys(BROKER_SKIPS.values()):
        if is_long and k in ("short_account_enabled", "shortable", "easy_to_borrow"):
            continue                                   # not applicable to longs
        if k == failed:
            rules[k] = False
            break
        rules[k] = True
    return rules


# ---------------------------------------------------------------------------
# Outcome math
# ---------------------------------------------------------------------------

def mfe_mae(direction: int, entry: Decimal, high: Decimal, low: Decimal) -> tuple[Decimal, Decimal]:
    """Per-share max favourable / adverse excursion while held (both >= 0)."""
    if direction > 0:
        return max(high - entry, Decimal(0)), max(entry - low, Decimal(0))
    return max(entry - low, Decimal(0)), max(high - entry, Decimal(0))


def r_multiple(pnl_per_share: Decimal, one_r_per_share: Decimal) -> Decimal | None:
    if not one_r_per_share:
        return None
    return pnl_per_share / one_r_per_share


def _q(v: Decimal | None, places: str = "0.0001") -> str | None:
    if v is None:
        return None
    q = Decimal(v).quantize(Decimal(places))
    return str(q.copy_abs() if q == 0 else q)          # never "-0.00"


EXIT_REASON = {"stop": "stop", "eod": "eod", "closesweep": "eod", "lateexit": "late_fill_exit",
               "stopupd": "stop_update_failed_exit", "stopfail": "stop_failed_exit", "flatten": "flatten",
               "opensweep": "open_sweep", "postclose": "after_close_flatten"}


def build_outcomes(state_journal, tlog: TradeLog, day: date, settings, bars: dict[str, list] | None,
                   scan_time: datetime | None, data_stale: set[str] = frozenset()) -> list[dict]:
    """One outcome per traded symbol from the state journal's orders/fills (the broker
    truth), the trade plan and 1-minute bars for MFE/MAE. Written once (append-only)."""
    dkey = day.isoformat()
    plans = {p["symbol"]: p for p in tlog.rows("plans", "WHERE day=?", (dkey,))}
    cands = {c["symbol"]: c for c in tlog.rows("candidates", "WHERE day=?", (dkey,))}
    fills = state_journal.fills(dkey)
    out = []
    for trade in state_journal.trades(dkey):
        sym = trade.symbol
        orders = state_journal.orders(dkey, sym)
        sym_fills = [f for f in fills if f["symbol"] == sym]
        entry_fills = [f for f in sym_fills if f["role"] == "entry"]
        exit_fills = [f for f in sym_fills if f["role"] != "entry"]
        if not entry_fills and trade.state != "foreign":
            continue                                   # never filled: not a trade (candidate record says why)
        tags: set[str] = set()
        events = tlog.events(dkey, sym)
        direction = trade.direction
        plan = plans.get(sym)
        cand = cands.get(sym)
        carry = trade.state == "foreign"
        carried_costs = (Decimal(0), Decimal(0))
        if carry:
            prev = tlog.db.execute("SELECT entry_price, filled_qty, direction, planned_stop, modeled_commission, "
                                   "modeled_slippage, detail FROM outcomes "
                                   "WHERE symbol=? AND day<? AND status='open_at_close' ORDER BY day DESC LIMIT 1",
                                   (sym, dkey)).fetchone()
            entry_qty = sum(Decimal(f["qty"]) for f in exit_fills)
            entry_price = _d(prev[0]) if prev else None
            exact = (json.loads(prev[6]) if isinstance(prev[6], str) else (prev[6] or {})).get("entry_price_exact") \
                if prev else None
            if exact:                              # the stored column is rounded; keep P&L exact across days
                entry_price = Decimal(exact)
            if prev:
                direction = prev[2]
                # the entry-side modeled costs were booked on the open_at_close row, which has no P&L
                share = min(Decimal(1), entry_qty / Decimal(prev[1])) if prev[1] else Decimal(1)
                carried_costs = ((_d(prev[4]) or Decimal(0)) * share, (_d(prev[5]) or Decimal(0)) * share)
            else:
                tags.add("DATA_MISSING")
            entry_time = None
        else:
            entry_qty = sum(Decimal(f["qty"]) for f in entry_fills)
            entry_price = sum(Decimal(f["qty"]) * Decimal(f["price"]) for f in entry_fills if f["price"]) / entry_qty
            entry_time = min(f["ts"] for f in entry_fills)
            if len(entry_fills) > 1:
                tags.add("PARTIAL_FILL")
        exit_qty = sum(Decimal(f["qty"]) for f in exit_fills)
        exit_price = (sum(Decimal(f["qty"]) * Decimal(f["price"]) for f in exit_fills if f["price"]) / exit_qty
                      if exit_qty else None)
        exit_time = max(f["ts"] for f in exit_fills) if exit_fills else None
        by_role: dict[str, Decimal] = {}
        for f in exit_fills:
            by_role[f["role"]] = by_role.get(f["role"], Decimal(0)) + Decimal(f["qty"])
        exit_role = max(by_role, key=by_role.get) if by_role else None
        exit_reason = EXIT_REASON.get(exit_role, exit_role)
        status = "closed" if exit_qty and exit_qty >= entry_qty else ("open_at_close" if not carry else "closed")
        if exit_reason == "eod":
            tags.add("EOD_EXIT")
        stops = [o for o in orders if o.role == "stop"]
        if any(o.replaces for o in stops) or (len(stops) > 1 and any(o.cancel_requested and o.status == 'canceled' for o in stops)):
            tags.add("STOP_REPLACED")
        if any(o.status == "rejected" for o in stops) or any(e["kind"] == "stop_lost" and
                                                              (e["status"] == "rejected") for e in events):
            tags.add("STOP_REJECTED")
        if any(e["kind"] == "reject" for e in events) or any(o.status == "rejected" for o in orders):
            tags.add("BROKER_REJECT")
        if any(e["kind"] == "stop_lost" and e["status"] == "canceled" for e in events) or \
                any(e["kind"] == "manual_interference" for e in events):
            tags.add("MANUAL_INTERFERENCE")
        if any(e["kind"] == "recovery" for e in events):
            tags.add("RECOVERY_EVENT")
        if any(e["kind"] == "duplicate" for e in events):
            tags.add("DUPLICATE_EVENT")
        if sym in data_stale:
            tags.add("DATA_STALE")
        submit = next((e for e in events if e["kind"] == "submit" and e["role"] == "entry"), None)
        if submit and scan_time and datetime.fromisoformat(submit["event_time"]) > \
                scan_time + timedelta(seconds=ENTRY_LATE_SECONDS):
            tags.add("ENTRY_LATE")
        trigger = _d(plan["entry_trigger"]) if plan else (trade.entry_price or None)
        one_r = _d(plan["one_r_per_share"]) if plan else (C.STOP_ATR_FRACTION * trade.atr if trade.atr else None)
        entry_slip = None
        if trigger and entry_price is not None and not carry:
            entry_slip = direction * (entry_price - trigger)
            if one_r and entry_slip > ENTRY_SLIPPAGE_R * one_r:
                tags.add("ENTRY_SLIPPAGE")
        actual_stop = trade.stop_price
        exit_slip = None
        if exit_reason == "stop" and actual_stop and exit_price is not None:
            exit_slip = direction * (actual_stop - exit_price)
        # MFE / MAE from 1-minute bars between entry and exit, plus the fills themselves
        high = low = None
        prices = [Decimal(f["price"]) for f in sym_fills if f["price"]]
        if prices:
            high, low = max(prices), min(prices)
        sym_bars = (bars or {}).get(sym)
        if sym_bars is None:
            tags.add("DATA_MISSING")
        else:
            t0 = datetime.fromisoformat(entry_time) if entry_time else None
            t1 = datetime.fromisoformat(exit_time) if exit_time else None
            held = [b for b in sym_bars if (t0 is None or b.t + timedelta(minutes=1) > t0)
                    and (t1 is None or b.t <= t1)]
            for b in held:
                high = b.h if high is None else max(high, b.h)
                low = b.l if low is None else min(low, b.l)
            if not held and not carry:
                tags.add("DATA_MISSING")
        mfe = mae = mfe_r = mae_r = None
        if entry_price is not None and high is not None:
            mfe, mae = mfe_mae(direction, entry_price, high, low)
            mfe_r, mae_r = r_multiple(mfe, one_r) if one_r else None, r_multiple(mae, one_r) if one_r else None
        matched = min(entry_qty, exit_qty)
        gross = None
        if entry_price is not None and exit_price is not None:
            gross = direction * (exit_price - entry_price) * matched
        commission = sum((Decimal(f["commission"]) for f in sym_fills), Decimal(0)) + carried_costs[0]
        slippage_m = sum((Decimal(f["slippage"]) for f in sym_fills), Decimal(0)) + carried_costs[1]
        actual_slip = None
        if entry_slip is not None or exit_slip is not None:
            actual_slip = (entry_slip or 0) * entry_qty + (exit_slip or 0) * exit_qty
        net_paper = None if gross is None else gross - commission
        net = None if gross is None else gross - commission - slippage_m
        notional = entry_price * matched if entry_price is not None and matched else None
        ret = None if net is None or not notional else net / notional * 100
        realized_r = None
        if gross is not None and one_r and matched:
            realized_r = gross / (one_r * matched)
        hold = None
        if entry_time and exit_time:
            hold = int((datetime.fromisoformat(exit_time) - datetime.fromisoformat(entry_time)).total_seconds())
        rules_ok = bool(cand is None or cand["action"] == "trade") and not (tags & RULE_BREAK_TAGS) \
            and exit_reason in ("stop", "eod")
        row = {
            "symbol": sym, "status": status, "direction": direction,
            "entry_trigger": _q(trigger, "0.01"), "planned_stop": plan and plan["planned_stop"],
            "actual_stop": _q(actual_stop, "0.01"), "planned_qty": plan and plan["planned_qty"],
            "filled_qty": str(entry_qty), "entry_time": entry_time, "entry_price": _q(entry_price),
            "entry_slippage_per_share": _q(entry_slip), "exit_time": exit_time, "exit_price": _q(exit_price),
            "exit_reason": exit_reason, "exit_slippage_per_share": _q(exit_slip),
            "high_while_held": _q(high), "low_while_held": _q(low), "mfe_per_share": _q(mfe),
            "mae_per_share": _q(mae), "mfe_r": _q(mfe_r, "0.01"), "mae_r": _q(mae_r, "0.01"),
            "gross_pnl": _q(gross, "0.01"), "modeled_commission": _q(commission, "0.0001"),
            "modeled_slippage": _q(slippage_m, "0.01"), "actual_slippage": _q(actual_slip, "0.01"),
            "net_pnl_paper_costs": _q(net_paper, "0.01"), "net_pnl_modeled": _q(net, "0.01"),
            "return_pct": _q(ret, "0.0001"), "realized_r": _q(realized_r, "0.01"), "holding_seconds": hold,
            "relvol": cand and cand["relvol"],
            "rules_followed": int(rules_ok), "execution_clean": int(not (tags & EXECUTION_TAGS)),
            "data_complete": int(not (tags & DATA_TAGS)), "deviation_tags": sorted(tags),
            "detail": {"exit_qty_by_role": {k: str(v) for k, v in by_role.items()},
                       "entry_fills": len(entry_fills), "exit_fills": len(exit_fills),
                       "stop_orders": len(stops), "carryover": carry,
                       "entry_price_exact": None if entry_price is None else str(entry_price),
                       "mfe_mae_source": "iex_1min_bars+fills" if sym_bars is not None else "fills_only"},
        }
        tlog.outcome(day, row)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def _dec_list(rows, key) -> list[Decimal]:
    return [Decimal(r[key]) for r in rows if r.get(key) not in (None, "")]


def _mean(xs: list[Decimal]) -> Decimal | None:
    return sum(xs, Decimal(0)) / len(xs) if xs else None


def _perf(rows: list[dict]) -> dict:
    nets = _dec_list(rows, "net_pnl_modeled")
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    return {"trades": len(rows), "wins": len(wins), "losses": len(losses),
            "win_rate": _q(Decimal(len(wins)) / len(nets), "0.0001") if nets else None,
            "net_pnl": _q(sum(nets, Decimal(0)), "0.01")}


def max_drawdown(pnls: list[Decimal]) -> Decimal:
    peak = cum = dd = Decimal(0)
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def summarize(outcomes: list[dict], candidates: list[dict], label: str,
              starting_equity: Decimal | None = None) -> dict:
    trades = [o for o in outcomes if o.get("gross_pnl") not in (None, "")]
    gross = _dec_list(trades, "gross_pnl")
    nets = _dec_list(trades, "net_pnl_modeled")
    nets_paper = _dec_list(trades, "net_pnl_paper_costs")
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    rs = _dec_list(trades, "realized_r")
    skipped: dict[str, int] = {}
    for c in candidates:
        if c.get("action") == "skip":
            skipped[c.get("skip_reason") or "unknown"] = skipped.get(c.get("skip_reason") or "unknown", 0) + 1
    by_bucket = {}
    for lo, hi in RELVOL_BUCKETS:
        name = f"{lo}-{hi}" if hi else f"{lo}+"
        rows = [o for o in trades if o.get("relvol") and Decimal(o["relvol"]) >= lo
                and (hi is None or Decimal(o["relvol"]) < hi)]
        by_bucket[name] = _perf(rows)
    ordered = sorted(trades, key=lambda o: o.get("exit_time") or "")
    dd = max_drawdown([Decimal(o["net_pnl_modeled"]) for o in ordered if o.get("net_pnl_modeled")])
    total_gross = sum(gross, Decimal(0))
    total_net = sum(nets, Decimal(0))
    cost = total_gross - total_net
    commission = sum(_dec_list(trades, "modeled_commission"), Decimal(0))
    modeled_slip = sum(_dec_list(trades, "modeled_slippage"), Decimal(0))
    s = {
        "period": label,
        "candidates_scanned": len(candidates),
        "candidates_passed_filters": sum(1 for c in candidates if c.get("rank") is not None),
        "candidates_top20": sum(1 for c in candidates if c.get("rank") is not None and c["rank"] <= C.MAX_POSITIONS),
        "trades": len(trades),
        "open_at_close": sum(1 for o in outcomes if o.get("status") == "open_at_close"),
        "skipped_by_reason": dict(sorted(skipped.items(), key=lambda kv: -kv[1])),
        "wins": len(wins), "losses": len(losses),
        "win_rate": _q(Decimal(len(wins)) / len(nets), "0.0001") if nets else None,
        "gross_pnl": _q(total_gross, "0.01"),
        # gross_pnl is the broker-fill result (real slippage already inside the prices); the modeled costs below
        # are hypothetical and additional, never a restatement of the slippage already realized.
        "actual_realized_pnl": _q(total_gross, "0.01"),
        "modeled_cost_total": _q(commission + modeled_slip, "0.01"),
        "net_pnl_paper_costs": _q(sum(nets_paper, Decimal(0)), "0.01"),
        "net_pnl_modeled": _q(total_net, "0.01"),
        "average_r": _q(_mean(rs), "0.01"),
        "expectancy_per_trade": _q(_mean(nets), "0.01"),
        "expectancy_r": _q(_mean(rs), "0.01"),
        "profit_factor": _q(sum(wins, Decimal(0)) / -sum(losses, Decimal(0)), "0.01") if losses else None,
        "average_win": _q(_mean(wins), "0.01"), "average_loss": _q(_mean(losses), "0.01"),
        "average_mfe_r": _q(_mean(_dec_list(trades, "mfe_r")), "0.01"),
        "average_mae_r": _q(_mean(_dec_list(trades, "mae_r")), "0.01"),
        "stop_outs": sum(1 for o in trades if o.get("exit_reason") == "stop"),
        "eod_exits": sum(1 for o in trades if o.get("exit_reason") == "eod"),
        "total_actual_slippage": _q(sum(_dec_list(trades, "actual_slippage"), Decimal(0)), "0.01"),
        "total_modeled_slippage": _q(modeled_slip, "0.01"),
        "total_modeled_commission": _q(commission, "0.01"),
        "cost_drag": _q(cost, "0.01"),
        "cost_drag_pct_of_gross": _q(cost / abs(total_gross) * 100, "0.01") if total_gross else None,
        "long": _perf([o for o in trades if int(o["direction"]) > 0]),
        "short": _perf([o for o in trades if int(o["direction"]) < 0]),
        "by_relvol_bucket": by_bucket,
        "max_drawdown": _q(dd, "0.01"),
        "max_drawdown_pct_of_equity": _q(dd / starting_equity * 100, "0.0001") if starting_equity else None,
        "rules_followed": sum(int(o.get("rules_followed") or 0) for o in trades),
        "execution_clean": sum(int(o.get("execution_clean") or 0) for o in trades),
        "data_complete": sum(int(o.get("data_complete") or 0) for o in trades),
        "deviation_tag_counts": _tag_counts(trades),
    }
    return s


def _tag_counts(rows) -> dict:
    counts: dict[str, int] = {}
    for o in rows:
        tags = o.get("deviation_tags")
        for t in (json.loads(tags) if isinstance(tags, str) else tags or []):
            counts[t] = counts.get(t, 0) + 1
    return dict(sorted(counts.items()))


def summary_markdown(s: dict, title: str) -> str:
    def v(x):
        return "-" if x is None else x
    lines = [f"# {title}", "", f"Variant: {C.VARIANT}. Record only; nothing here changes strategy rules.", "",
             "| Metric | Value |", "|---|---|"]
    for k in ("candidates_scanned", "candidates_passed_filters", "candidates_top20", "trades", "open_at_close",
              "wins", "losses", "win_rate", "gross_pnl", "net_pnl_paper_costs", "net_pnl_modeled", "average_r",
              "expectancy_per_trade", "expectancy_r", "profit_factor", "average_win", "average_loss",
              "average_mfe_r", "average_mae_r", "stop_outs", "eod_exits", "total_actual_slippage",
              "total_modeled_slippage", "total_modeled_commission", "cost_drag", "cost_drag_pct_of_gross",
              "max_drawdown", "max_drawdown_pct_of_equity", "rules_followed", "execution_clean", "data_complete"):
        lines.append(f"| {k} | {v(s.get(k))} |")
    lines += ["", "## Skipped by reason", "", "| Reason | Count |", "|---|---|"]
    lines += [f"| {r} | {n} |" for r, n in s["skipped_by_reason"].items()] or ["| - | 0 |"]
    lines += ["", "## Long vs short", "", "| Side | Trades | Wins | Losses | Win rate | Net P&L |", "|---|---|---|---|---|---|"]
    for side in ("long", "short"):
        p = s[side]
        lines.append(f"| {side} | {p['trades']} | {p['wins']} | {p['losses']} | {v(p['win_rate'])} | {p['net_pnl']} |")
    lines += ["", "## By relative volume", "", "| RelVol | Trades | Wins | Losses | Win rate | Net P&L |",
              "|---|---|---|---|---|---|"]
    for b, p in s["by_relvol_bucket"].items():
        lines.append(f"| {b} | {p['trades']} | {p['wins']} | {p['losses']} | {v(p['win_rate'])} | {p['net_pnl']} |")
    lines += ["", "## Deviation tags", "", "| Tag | Trades |", "|---|---|"]
    lines += [f"| {t} | {n} |" for t, n in s["deviation_tag_counts"].items()] or ["| - | 0 |"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(rows[0]) if rows else ["(no rows)"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _equity(tlog: TradeLog, start: str, end: str) -> Decimal | None:
    row = tlog.db.execute("SELECT equity FROM runs WHERE substr(started_at,1,10) BETWEEN ? AND ? "
                          "ORDER BY started_at LIMIT 1", (start, end)).fetchone()
    return _d(row[0]) if row and row[0] else None


def export_day(tlog: TradeLog, day: date) -> dict[str, Path]:
    dkey = day.isoformat()
    base = tlog.export_dir / "daily"
    outcomes = tlog.rows("outcomes", "WHERE day=? ORDER BY exit_time", (dkey,))
    cands = tlog.rows("candidates", "WHERE day=? ORDER BY CASE WHEN rank IS NULL THEN 1 ELSE 0 END, rank, symbol",
                      (dkey,))
    paths = {"trades_csv": base / f"trades_{dkey}.csv", "candidates_csv": base / f"candidates_{dkey}.csv",
             "events_csv": base / f"events_{dkey}.csv", "summary_md": base / f"summary_{dkey}.md",
             "summary_json": base / f"summary_{dkey}.json"}
    _write_csv(paths["trades_csv"], outcomes)
    _write_csv(paths["candidates_csv"], cands)
    _write_csv(paths["events_csv"], tlog.events(dkey))
    s = summarize(outcomes, cands, dkey, _equity(tlog, dkey, dkey))
    tlog.save_day_summary(day, s)
    paths["summary_md"].write_text(summary_markdown(s, f"ORB-IEX daily summary {dkey}"), encoding="utf-8")
    paths["summary_json"].write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")
    return paths


def export_period(tlog: TradeLog, start: date, end: date, kind: str, label: str) -> Path:
    outcomes = tlog.rows("outcomes", "WHERE day BETWEEN ? AND ? ORDER BY exit_time",
                         (start.isoformat(), end.isoformat()))
    cands = tlog.rows("candidates", "WHERE day BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))
    s = summarize(outcomes, cands, label, _equity(tlog, start.isoformat(), end.isoformat()))
    s["days"] = len({o["day"] for o in outcomes} | {c["day"] for c in cands})
    path = tlog.export_dir / kind / f"summary_{label}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(summary_markdown(s, f"ORB-IEX {kind} summary {label}") + f"\nTrading days: {s['days']}\n",
                    encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(s, indent=2, default=str), encoding="utf-8")
    return path


def export_all(tlog: TradeLog, day: date) -> dict[str, Path]:
    paths = export_day(tlog, day)
    iso = day.isocalendar()
    wk_start = day - timedelta(days=day.weekday())
    paths["weekly_md"] = export_period(tlog, wk_start, wk_start + timedelta(days=6), "weekly",
                                       f"{iso[0]}-W{iso[1]:02d}")
    m_start = day.replace(day=1)
    m_end = (m_start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    paths["monthly_md"] = export_period(tlog, m_start, m_end, "monthly", f"{day.year:04d}-{day.month:02d}")
    return paths
