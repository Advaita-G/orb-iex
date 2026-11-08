"""SQLite journal: everything needed to restart safely at any time.

Tables
  days        per-session markers: scanned, session_closing, done events, EOD status
  universe    daily eligible list (day, symbol, rank, ATR, ADV); legacy column
              names month/price/dollar_volume hold day/ATR/ADV
  universe_meta  when each day's list was built
  candidates  daily candidate table (all ranked names and why they were skipped)
  trades      one row per planned trade (entry/stop plan, order ids, flags)
  orders      every order the bot created or adopted; replaces/replaced_by chain
  fills       fill deltas (idempotent key: order_id + cumulative filled qty)
  owned_adjust  correction so bot-owned qty never exceeds what Alpaca holds
  diagnostics 09:35 vs 09:37 opening-bar comparison
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, scanned INTEGER DEFAULT 0,
    scan_status TEXT, session_closing INTEGER DEFAULT 1, done TEXT DEFAULT '{}',
    eod_status TEXT, eod_started_at TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS universe(month TEXT, symbol TEXT, rank INTEGER, price TEXT,
    dollar_volume TEXT, PRIMARY KEY(month, symbol));
CREATE TABLE IF NOT EXISTS universe_meta(month TEXT PRIMARY KEY, built_at TEXT,
    source_session TEXT, first_session TEXT, count INTEGER);
CREATE TABLE IF NOT EXISTS candidates(day TEXT, symbol TEXT, rank INTEGER, status TEXT,
    reason TEXT, details TEXT, PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS trades(day TEXT, symbol TEXT, direction INTEGER, planned_qty TEXT,
    entry_price TEXT, stop_price TEXT, atr TEXT, entry_client_id TEXT, entry_order_id TEXT,
    stop_order_id TEXT, stop_seq INTEGER DEFAULT 0, protected_qty TEXT DEFAULT '0',
    closed INTEGER DEFAULT 0, pending_flatten INTEGER DEFAULT 0, flatten_attempts INTEGER DEFAULT 0,
    exit_seq INTEGER DEFAULT 0, state TEXT, note TEXT, PRIMARY KEY(day, symbol));
CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, client_order_id TEXT UNIQUE,
    day TEXT, symbol TEXT, role TEXT, side TEXT, type TEXT, qty TEXT, stop_price TEXT,
    status TEXT, filled_qty TEXT DEFAULT '0', filled_avg_price TEXT, replaces TEXT,
    replaced_by TEXT, cancel_requested INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS fills(order_id TEXT, cum_qty TEXT, day TEXT, symbol TEXT, role TEXT,
    side TEXT, qty TEXT, price TEXT, ts TEXT, commission TEXT, slippage TEXT,
    PRIMARY KEY(order_id, cum_qty));
CREATE TABLE IF NOT EXISTS owned_adjust(symbol TEXT PRIMARY KEY, qty TEXT);
CREATE TABLE IF NOT EXISTS diagnostics(day TEXT, kind TEXT, body TEXT, PRIMARY KEY(day, kind));
CREATE TABLE IF NOT EXISTS intents(intent_id TEXT PRIMARY KEY, ts TEXT, symbol TEXT,
    trade_id TEXT, role TEXT, client_order_id TEXT, order_id TEXT, mutation TEXT,
    desired_qty TEXT, desired_price TEXT, state TEXT, response TEXT, attempt INTEGER);
CREATE TABLE IF NOT EXISTS breakers(kind TEXT, symbol TEXT, detail TEXT, ts TEXT,
    PRIMARY KEY(kind, symbol));
CREATE TABLE IF NOT EXISTS risk(symbol TEXT PRIMARY KEY, unprotected_since TEXT,
    exit_role TEXT, reason TEXT);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(v) -> Decimal | None:
    return None if v is None or v == "" else Decimal(v)


@dataclass
class OrderRec:
    order_id: str
    client_order_id: str
    day: str
    symbol: str
    role: str
    side: str
    type: str
    qty: Decimal
    stop_price: Decimal | None
    status: str
    filled_qty: Decimal = Decimal(0)
    filled_avg_price: Decimal | None = None
    replaces: str | None = None
    replaced_by: str | None = None
    cancel_requested: bool = False
    created_at: str | None = None
    updated_at: str | None = None
    broker_updated_at: str | None = None


@dataclass
class TradeRec:
    day: str
    symbol: str
    direction: int
    planned_qty: Decimal
    entry_price: Decimal
    stop_price: Decimal
    atr: Decimal
    entry_client_id: str
    entry_order_id: str | None = None
    stop_order_id: str | None = None
    stop_seq: int = 0
    protected_qty: Decimal = Decimal(0)
    closed: bool = False
    pending_flatten: bool = False
    flatten_attempts: int = 0
    exit_seq: int = 0
    state: str = "planned"
    note: str | None = None


def _to_db(v):
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, Decimal):
        return str(v)
    return v


class Journal:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(orders)")}
        if "broker_updated_at" not in cols:
            self.db.execute("ALTER TABLE orders ADD COLUMN broker_updated_at TEXT")
        self.lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("SAVEPOINT journal_write")
            try:
                yield
                self.db.execute("RELEASE journal_write")
            except BaseException:
                self.db.execute("ROLLBACK TO journal_write")
                self.db.execute("RELEASE journal_write")
                raise

    def set_breaker(self, kind: str, symbol: str = "", detail: str = "") -> None:
        self.db.execute("INSERT INTO breakers VALUES(?,?,?,?) ON CONFLICT(kind,symbol) "
                        "DO UPDATE SET detail=excluded.detail, ts=excluded.ts", (kind, symbol, detail, _now()))

    def clear_breaker(self, kind: str, symbol: str = "") -> None:
        self.db.execute("DELETE FROM breakers WHERE kind=? AND symbol=?", (kind, symbol))

    def breakers(self) -> list[tuple]:
        return list(self.db.execute("SELECT kind,symbol,detail,ts FROM breakers"))

    def risk(self, symbol: str) -> dict:
        r = self.db.execute("SELECT unprotected_since,exit_role,reason FROM risk WHERE symbol=?", (symbol,)).fetchone()
        return dict(zip(("unprotected_since", "exit_role", "reason"), r or (None, None, None)))

    def save_risk(self, symbol: str, **values) -> None:
        state = self.risk(symbol)
        state.update(values)
        self.db.execute("INSERT INTO risk VALUES(?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                        "unprotected_since=excluded.unprotected_since,exit_role=excluded.exit_role,reason=excluded.reason",
                        (symbol, state['unprotected_since'], state['exit_role'], state['reason']))

    def intents(self, unresolved: bool = False, symbol: str | None = None) -> list[dict]:
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(intents)")]
        where, args = [], []
        if unresolved:
            where.append("state != 'TERMINAL'")
        if symbol is not None:
            where.append("symbol=?")
            args.append(symbol)
        sql = "SELECT * FROM intents" + (" WHERE " + " AND ".join(where) if where else "")
        return [dict(zip(cols, r)) for r in self.db.execute(sql + " ORDER BY ts,intent_id", args)]

    def save_intent(self, intent: dict) -> None:
        cols = list(intent)
        self.db.execute(f"INSERT INTO intents({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                        "ON CONFLICT(intent_id) DO UPDATE SET " +
                        ','.join(f"{c}=excluded.{c}" for c in cols if c != 'intent_id'),
                        [intent[c] for c in cols])

    # -- meta / days --------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _ensure_day(self, day: date) -> None:
        self.db.execute("INSERT OR IGNORE INTO days(day) VALUES(?)", (day.isoformat(),))

    def day(self, day: date) -> dict[str, Any]:
        self._ensure_day(day)
        row = self.db.execute("SELECT scanned, scan_status, session_closing, done, eod_status, "
                              "eod_started_at, summary FROM days WHERE day=?", (day.isoformat(),)).fetchone()
        return {"scanned": bool(row[0]), "scan_status": row[1], "session_closing": bool(row[2]),
                "done": json.loads(row[3] or "{}"), "eod_status": row[4], "eod_started_at": row[5],
                "summary": json.loads(row[6]) if row[6] else None}

    def is_done(self, day: date, event: str) -> bool:
        return event in self.day(day)["done"]

    def mark_done(self, day: date, event: str, info: str = "ok") -> None:
        with self.lock:
            done = self.day(day)["done"]
            done[event] = {"at": _now(), "info": info}
            self.db.execute("UPDATE days SET done=? WHERE day=?", (json.dumps(done), day.isoformat()))

    def mark_scanned(self, day: date, status: str) -> None:
        self._ensure_day(day)
        self.db.execute("UPDATE days SET scanned=1, scan_status=? WHERE day=?", (status, day.isoformat()))

    def set_session_closing(self, day: date, closing: bool) -> None:
        self._ensure_day(day)
        self.db.execute("UPDATE days SET session_closing=? WHERE day=?", (int(closing), day.isoformat()))

    def set_eod(self, day: date, status: str, started_at: str | None = None) -> None:
        self._ensure_day(day)
        if started_at:
            self.db.execute("UPDATE days SET eod_status=?, eod_started_at=? WHERE day=?",
                            (status, started_at, day.isoformat()))
        else:
            self.db.execute("UPDATE days SET eod_status=? WHERE day=?", (status, day.isoformat()))

    def set_summary(self, day: date, summary: dict) -> None:
        self._ensure_day(day)
        self.db.execute("UPDATE days SET summary=? WHERE day=?", (json.dumps(summary, default=str), day.isoformat()))

    # -- universe -------------------------------------------------------------
    def save_universe(self, month: str, rows: list[dict], source_session: str, first_session: str) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM universe WHERE month=?", (month,))
            self.db.executemany("INSERT INTO universe VALUES(?,?,?,?,?)",
                                [(month, r["symbol"], r["rank"], str(r["atr"]), str(r["adv"]))
                                 for r in rows])
            self.db.execute("INSERT INTO universe_meta VALUES(?,?,?,?,?) ON CONFLICT(month) DO UPDATE SET "
                            "built_at=excluded.built_at,source_session=excluded.source_session,"
                            "first_session=excluded.first_session,count=excluded.count",
                            (month, _now(), source_session, first_session, len(rows)))

    def universe(self, month: str) -> list[str] | None:
        if not self.db.execute("SELECT 1 FROM universe_meta WHERE month=?", (month,)).fetchone():
            return None
        return [r[0] for r in self.db.execute(
            "SELECT symbol FROM universe WHERE month=? ORDER BY rank", (month,))]

    def universe_first_session(self, month: str) -> str | None:
        row = self.db.execute("SELECT first_session FROM universe_meta WHERE month=?", (month,)).fetchone()
        return row[0] if row else None

    # -- candidates / diagnostics -------------------------------------------------
    def save_candidates(self, day: date, rows: list[dict]) -> None:
        with self.transaction():
            for r in rows:
                self.db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?) ON CONFLICT(day,symbol) DO UPDATE SET "
                                "rank=excluded.rank,status=excluded.status,reason=excluded.reason,details=excluded.details",
                                (day.isoformat(), r["symbol"], r.get("rank"), r["status"], r.get("reason"),
                                 json.dumps(r.get("details", {}), default=str)))

    def update_candidate(self, day: date, symbol: str, status: str, reason: str | None = None) -> None:
        self.db.execute("UPDATE candidates SET status=?, reason=? WHERE day=? AND symbol=?",
                        (status, reason, day.isoformat(), symbol))

    def candidates(self, day: date) -> list[dict]:
        return [{"symbol": r[0], "rank": r[1], "status": r[2], "reason": r[3], "details": json.loads(r[4])}
                for r in self.db.execute("SELECT symbol, rank, status, reason, details FROM candidates "
                                         "WHERE day=? ORDER BY rank", (day.isoformat(),))]

    def save_diagnostic(self, day: date, kind: str, body: dict) -> None:
        self.db.execute("INSERT INTO diagnostics VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET body=excluded.body",
                        (day.isoformat(), kind, json.dumps(body, default=str)))

    def diagnostic(self, day: date, kind: str) -> dict | None:
        row = self.db.execute("SELECT body FROM diagnostics WHERE day=? AND kind=?",
                              (day.isoformat(), kind)).fetchone()
        return json.loads(row[0]) if row else None

    # -- trades -------------------------------------------------------------
    def save_trade(self, t: TradeRec) -> None:
        cols = [f.name for f in fields(TradeRec)]
        self.db.execute(f"INSERT INTO trades({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                        "ON CONFLICT(day,symbol) DO UPDATE SET " +
                        ','.join(f"{c}=excluded.{c}" for c in cols if c not in ('day','symbol')),
                        [_to_db(getattr(t, c)) for c in cols])

    def _trade_from_row(self, row) -> TradeRec:
        cols = [f.name for f in fields(TradeRec)]
        d = dict(zip(cols, row))
        for k in ("planned_qty", "entry_price", "stop_price", "atr", "protected_qty"):
            d[k] = _d(d[k])
        d["closed"] = bool(d["closed"])
        d["pending_flatten"] = bool(d["pending_flatten"])
        return TradeRec(**d)

    def trade(self, day: str, symbol: str) -> TradeRec | None:
        cols = ",".join(f.name for f in fields(TradeRec))
        row = self.db.execute(f"SELECT {cols} FROM trades WHERE day=? AND symbol=?", (day, symbol)).fetchone()
        return self._trade_from_row(row) if row else None

    def trades(self, day: str) -> list[TradeRec]:
        cols = ",".join(f.name for f in fields(TradeRec))
        return [self._trade_from_row(r) for r in
                self.db.execute(f"SELECT {cols} FROM trades WHERE day=? ORDER BY symbol", (day,))]

    # -- orders -------------------------------------------------------------
    def save_order(self, o: OrderRec) -> None:
        o.updated_at = _now()
        o.created_at = o.created_at or o.updated_at
        cols = [f.name for f in fields(OrderRec)]
        self.db.execute(f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
                        "ON CONFLICT(order_id) DO UPDATE SET " +
                        ','.join(f"{c}=excluded.{c}" for c in cols if c not in ('order_id','created_at')),
                        [_to_db(getattr(o, c)) for c in cols])

    def _order_from_row(self, row) -> OrderRec:
        cols = [f.name for f in fields(OrderRec)]
        d = dict(zip(cols, row))
        for k in ("qty", "stop_price", "filled_qty", "filled_avg_price"):
            d[k] = _d(d[k])
        d["filled_qty"] = d["filled_qty"] or Decimal(0)
        d["cancel_requested"] = bool(d["cancel_requested"])
        return OrderRec(**d)

    def order(self, order_id: str) -> OrderRec | None:
        cols = ",".join(f.name for f in fields(OrderRec))
        row = self.db.execute(f"SELECT {cols} FROM orders WHERE order_id=?", (order_id,)).fetchone()
        return self._order_from_row(row) if row else None

    def order_by_client_id(self, cid: str) -> OrderRec | None:
        cols = ",".join(f.name for f in fields(OrderRec))
        row = self.db.execute(f"SELECT {cols} FROM orders WHERE client_order_id=?", (cid,)).fetchone()
        return self._order_from_row(row) if row else None

    def orders(self, day: str | None = None, symbol: str | None = None) -> list[OrderRec]:
        cols = ",".join(f.name for f in fields(OrderRec))
        q, args = f"SELECT {cols} FROM orders WHERE 1=1", []
        if day:
            q += " AND day=?"
            args.append(day)
        if symbol:
            q += " AND symbol=?"
            args.append(symbol)
        return [self._order_from_row(r) for r in self.db.execute(q + " ORDER BY created_at", args)]

    # -- fills --------------------------------------------------------------
    def add_fill(self, order: OrderRec, cum_qty: Decimal, qty: Decimal, price: Decimal | None,
                 ts: str, commission: Decimal, slippage: Decimal) -> bool:
        """Returns False if this exact fill (order, cumulative qty) was already recorded."""
        cur = self.db.execute("INSERT OR IGNORE INTO fills VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (order.order_id, str(cum_qty), order.day, order.symbol, order.role, order.side,
                               str(qty), None if price is None else str(price), ts, str(commission),
                               str(slippage)))
        return cur.rowcount == 1

    def fills(self, day: str) -> list[dict]:
        return [dict(zip(("order_id", "cum_qty", "symbol", "role", "side", "qty", "price", "ts",
                          "commission", "slippage"), r))
                for r in self.db.execute("SELECT order_id, cum_qty, symbol, role, side, qty, price, ts, "
                                         "commission, slippage FROM fills WHERE day=? ORDER BY ts", (day,))]

    # -- bot-owned positions -------------------------------------------------------
    def owned_qty(self) -> dict[str, Decimal]:
        """Signed shares ORB-IEX holds per symbol: net of every fill of an
        ORB-IEX order (all days), plus corrections. Unrelated positions in the
        same account never appear here."""
        out: dict[str, Decimal] = {}
        for sym, side, qty in self.db.execute("SELECT symbol, side, qty FROM fills"):
            q = Decimal(qty)
            out[sym] = out.get(sym, Decimal(0)) + (q if side == "buy" else -q)
        for sym, qty in self.db.execute("SELECT symbol, qty FROM owned_adjust"):
            out[sym] = out.get(sym, Decimal(0)) + Decimal(qty)
        return {s: q for s, q in out.items() if q != 0}

    def adjust_owned(self, symbol: str, delta: Decimal) -> None:
        row = self.db.execute("SELECT qty FROM owned_adjust WHERE symbol=?", (symbol,)).fetchone()
        prev = Decimal(row[0]) if row else Decimal(0)
        self.db.execute("INSERT INTO owned_adjust VALUES(?,?) ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty", (symbol, str(prev + delta)))

    def last_fill_price(self, symbol: str) -> Decimal | None:
        row = self.db.execute('SELECT price FROM fills WHERE symbol=? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1', (symbol,)).fetchone()
        return Decimal(row[0]) if row and row[0] else None
