"""A real, deterministic journal fixture for the dashboard tests.

The dashboard is read-only, so this fixture never mocks the readers: it writes the
ENGINE'S OWN schemas (imported from ``orb_iex.journal`` / ``orb_iex.tradelog``) into a
temp root and lets the real adapters read them. If a column ever moves, this fixture
breaks with it instead of silently drifting away from the live contract.

Every session below is a different state on purpose, so one fixture covers the whole
product surface:

============================  ==========================================
2026-10-01  carry-over        a short entered Thursday, closed the next morning
2026-10-02  no plan           fills with no plan and no engine trade row
2026-10-05  clean win         two partial entry fills, one stop exit
2026-10-06  execution defect  stop replaced then rejected, flattened, bar divergence
2026-10-07  healthy no-trade  every candidate filtered out by the rules
2026-10-08  scan failed       no entry decision was ever made
2026-10-09  open risk         a filled entry with no protective stop, breaker, intent
2026-10-10  weekend           a market-closed cell
2026-10-12  no candidates     a session row with an empty candidate table
============================  ==========================================
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path

from orb_iex import journal as journal_mod
from orb_iex import tradelog as tradelog_mod
from orb_iex.config import parse_settings
from orb_iex.dashboard.funnel import GATE_KEYS
from orb_iex.dashboard.sources import Sources

# -- sessions ---------------------------------------------------------------
CARRY_DAY = "2026-10-01"          # Thursday
NO_PLAN_DAY = "2026-10-02"        # Friday
CLEAN_DAY = "2026-10-05"          # Monday
DEFECT_DAY = "2026-10-06"         # Tuesday
FLAT_DAY = "2026-10-07"           # Wednesday
SCAN_FAILED_DAY = "2026-10-08"    # Thursday
OPEN_DAY = "2026-10-09"           # Friday
MARKET_CLOSED_DAY = "2026-10-10"  # Saturday
NO_DATA_DAY = "2026-10-12"        # Monday: a session row with no candidates
SESSION_DAYS = (CARRY_DAY, NO_PLAN_DAY, CLEAN_DAY, DEFECT_DAY, FLAT_DAY, SCAN_FAILED_DAY, OPEN_DAY)
MONTH = "2026-10"

# The cost model the settings carry, repeated here so the expected numbers are visible.
COMMISSION = D("0.0035")
SLIPPAGE = D("0.01")

# What the fixture is worth, written down once so the tests assert the same arithmetic
# the fixture was built from rather than whatever the code happens to print.
EXPECTED = {
    "closed_trades": 4,
    "open_trades": 1,
    "realized_pnl": "42.00",          # 10.00 - 4.00 + 86.00 - 50.00
    "modeled_cost": "5.40",           # 0.0135/share on (entry qty + exit qty) of every trade
    "modeled_net_pnl": "36.60",
    "r_values": {"2026-10-01:IWM": "2.00", "2026-10-05:AAPL": "1.72", "2026-10-06:MSFT": "-1.00"},
    "trades_without_r": 1,             # 2026-10-02:PLTR has no plan, so R is None, never 0
}


def rules(*failing: str, ranked: bool = True) -> dict[str, bool | None]:
    """A journaled rule checklist in the engine's own semantics.

    Every gate is True until one fails; the gates after it (and ``in_top20`` for a
    filtered candidate, which is never ranked) are ``None``: never evaluated. The
    dashboard must treat that as unknown, not as a pass and not as a zero.
    """
    out: dict[str, bool | None] = {}
    stopped = False
    for key in GATE_KEYS:
        if not ranked and key == "in_top20":
            out[key] = None
            continue
        if stopped:
            out[key] = None
            continue
        out[key] = key not in failing
        if key in failing:
            stopped = True
    return out


# ---------------------------------------------------------------------------
# state journal (runtime/journal.sqlite): what the engine itself persisted
# ---------------------------------------------------------------------------

DAYS = [
    # day, scanned, scan_status, session_closing, eod_status, summary
    (CARRY_DAY, 1, "ok", 0, "done", {"positions_closed": 1, "flattened": 0}),
    (NO_PLAN_DAY, 1, "ok", 0, "done", {"positions_closed": 1, "flattened": 0}),
    (CLEAN_DAY, 1, "ok", 0, "done", {"positions_closed": 1, "flattened": 0}),
    (DEFECT_DAY, 1, "ok", 0, "done", {"positions_closed": 1, "flattened": 1}),
    (FLAT_DAY, 1, "ok", 1, "done", {"positions_closed": 0, "flattened": 0}),
    (SCAN_FAILED_DAY, 0, "failed", 1, "skipped", {"positions_closed": 0, "flattened": 0}),
    (OPEN_DAY, 1, "ok", 0, "done", {"positions_closed": 0, "flattened": 0}),
    (NO_DATA_DAY, 1, "ok", 1, "done", {"positions_closed": 0, "flattened": 0}),
]

CANDIDATES = [
    # day, symbol, rank, status, reason, details
    (CARRY_DAY, "IWM", 1, "submitted", None,
     {"open": "29.95", "high": "30.10", "low": "29.80", "close": "29.90", "volume": 800_000,
      "relvol": "2.4", "atr": "0.80", "adv": 6_000_000, "direction": -1,
      "entry": "29.90", "stop": "30.40", "qty": 10}),
    (NO_PLAN_DAY, "SPY", None, "filtered", "relvol<1",
     {"open": "600.00", "high": "601.00", "low": "599.00", "close": "600.50", "volume": 40_000_000,
      "relvol": "0.7", "atr": "3.00", "adv": 90_000_000}),
    (NO_PLAN_DAY, "PLTR", 2, "submitted", None,
     {"open": "25.05", "high": "25.20", "low": "24.95", "close": "25.00", "volume": 5_000_000,
      "relvol": "1.8", "atr": "0.90", "adv": 40_000_000, "direction": 1,
      "entry": "25.02", "stop": "24.60", "qty": 40}),
    (CLEAN_DAY, "AAPL", 1, "submitted", None,
     {"open": "50.00", "high": "50.50", "low": "49.90", "close": "50.40", "volume": 900_000,
      "relvol": "2.1", "atr": "1.00", "adv": 20_000_000, "direction": 1,
      "entry": "50.10", "stop": "49.60", "qty": 100}),
    (CLEAN_DAY, "TSLA", 21, "below_top20", "rank>20",
     {"open": "300.00", "high": "302.00", "low": "299.00", "close": "301.00", "volume": 2_000_000,
      "relvol": "1.4", "atr": "8.00", "adv": 100_000_000, "direction": 1,
      "entry": "301.00", "stop": "297.00", "qty": 10}),
    (DEFECT_DAY, "MSFT", 1, "submitted", None,
     {"open": "199.50", "high": "200.20", "low": "199.00", "close": "200.00", "volume": 1_200_000,
      "relvol": "1.9", "atr": "1.20", "adv": 25_000_000, "direction": -1,
      "entry": "199.95", "stop": "200.95", "qty": 50}),
    (DEFECT_DAY, "NVDA", None, "filtered", "adv<1M",
     {"open": "800.00", "high": "805.00", "low": "798.00", "close": "803.00", "volume": 120_000,
      "relvol": "1.6", "atr": "12.00", "adv": 900_000}),
    (DEFECT_DAY, "ORCL", 24, "below_top20", "rank>20",
     {"open": "180.00", "high": "181.00", "low": "179.00", "close": "180.50", "volume": 700_000,
      "relvol": "1.3", "atr": "2.40", "adv": 30_000_000}),
    (FLAT_DAY, "KO", None, "filtered", "relvol<1",
     {"open": "60.00", "high": "60.20", "low": "59.90", "close": "60.10", "volume": 300_000,
      "relvol": "0.82", "atr": "0.60", "adv": 14_000_000}),
    (FLAT_DAY, "PEP", None, "filtered", "atr<=0.50",
     {"open": "150.00", "high": "150.20", "low": "149.90", "close": "150.10", "volume": 300_000,
      "relvol": "1.7", "atr": "0.35", "adv": 9_000_000}),
    # The failed session: bars were never loaded, so there are no details to check.
    (SCAN_FAILED_DAY, "QQQ", None, "filtered", "not_ready:opening_bar,relvol,atr,adv", {}),
    (SCAN_FAILED_DAY, "AMD", None, "filtered", "not_ready:opening_bar,relvol,atr,adv", {}),
    (OPEN_DAY, "AMD", 1, "submitted", None,
     {"open": "99.80", "high": "100.10", "low": "99.50", "close": "100.00", "volume": 1_500_000,
      "relvol": "2.6", "atr": "0.90", "adv": 35_000_000, "direction": 1,
      "entry": "100.00", "stop": "99.50", "qty": 25}),
    (OPEN_DAY, "GOOG", 2, "skipped", "stale_quote",
     {"open": "210.00", "high": "211.00", "low": "209.50", "close": "210.50", "volume": 900_000,
      "relvol": "1.5", "atr": "2.00", "adv": 28_000_000, "direction": 1,
      "entry": "210.20", "stop": "209.20", "qty": 20}),
]

DIAGNOSTICS = [
    # day, kind, body -- the 09:37 re-read disagreed with the 09:35 bar for MSFT.
    (DEFECT_DAY, "divergence",
     {"changed": {"MSFT": {"before": {"o": "199.50", "h": "200.20", "l": "199.00", "c": "200.00", "v": 1_200_000},
                           "after": {"o": "199.50", "h": "200.35", "l": "199.00", "c": "200.00", "v": 1_180_000}}}}),
    (SCAN_FAILED_DAY, "scan", {"error": "IEX opening bars unavailable", "bars_received": 0}),
]

TRADES = [
    # day, symbol, direction, planned_qty, entry_price, stop_price, atr, entry_client_id,
    # entry_order_id, stop_order_id, protected_qty, closed, flatten_attempts, state
    (CARRY_DAY, "IWM", -1, "10", "29.90", "30.40", "0.80", "orbiex-20261001-IWM-entry",
     "o-iwm-entry", "o-iwm-stop", "10", 1, 0, "closed"),
    (CLEAN_DAY, "AAPL", 1, "100", "50.10", "49.60", "1.00", "orbiex-20261005-AAPL-entry",
     "o-aapl-entry", "o-aapl-stop", "100", 1, 0, "closed"),
    (DEFECT_DAY, "MSFT", -1, "50", "199.95", "200.95", "1.20", "orbiex-20261006-MSFT-entry",
     "o-msft-entry", "o-msft-stop-2", "0", 1, 2, "closed"),
    (OPEN_DAY, "AMD", 1, "25", "100.00", "99.50", "0.90", "orbiex-20261009-AMD-entry",
     "o-amd-entry", None, "0", 0, 0, "open"),
]

ORDERS = [
    # order_id, client_order_id, day, symbol, role, side, type, qty, stop_price, status,
    # filled_qty, filled_avg_price, replaces, replaced_by
    ("o-iwm-entry", "orbiex-20261001-IWM-entry", CARRY_DAY, "IWM", "entry", "sell", "limit", "10",
     None, "filled", "10", "30.00", None, None),
    ("o-iwm-stop", "orbiex-20261001-IWM-stop", CARRY_DAY, "IWM", "stop", "buy", "stop", "10",
     "30.40", "canceled", "0", None, None, None),
    ("o-iwm-exit", "orbiex-20261002-IWM-eod", NO_PLAN_DAY, "IWM", "eod", "buy", "market", "10",
     None, "filled", "10", "29.00", None, None),
    ("o-pltr-entry", "orbiex-20261002-PLTR-entry", NO_PLAN_DAY, "PLTR", "entry", "buy", "limit", "40",
     None, "filled", "40", "25.00", None, None),
    ("o-pltr-stop", "orbiex-20261002-PLTR-stop", NO_PLAN_DAY, "PLTR", "stop", "sell", "stop", "40",
     "24.60", "canceled", "0", None, None, None),
    ("o-pltr-exit", "orbiex-20261002-PLTR-eod", NO_PLAN_DAY, "PLTR", "eod", "sell", "market", "40",
     None, "filled", "40", "24.90", None, None),
    ("o-aapl-entry", "orbiex-20261005-AAPL-entry", CLEAN_DAY, "AAPL", "entry", "buy", "limit", "100",
     None, "filled", "100", "50.09", None, None),
    ("o-aapl-stop", "orbiex-20261005-AAPL-stop", CLEAN_DAY, "AAPL", "stop", "sell", "stop", "100",
     "49.60", "filled", "100", "50.95", None, None),
    ("o-msft-entry", "orbiex-20261006-MSFT-entry", DEFECT_DAY, "MSFT", "entry", "sell", "limit", "50",
     None, "filled", "50", "200.00", None, None),
    # The protection failed here: the stop was replaced, then rejected, and the engine's
    # own flatten closed the position instead.
    ("o-msft-stop-1", "orbiex-20261006-MSFT-stop", DEFECT_DAY, "MSFT", "stop", "buy", "stop", "50",
     "200.60", "replaced", "0", None, None, "o-msft-stop-2"),
    ("o-msft-stop-2", "orbiex-20261006-MSFT-stop-r2", DEFECT_DAY, "MSFT", "stop", "buy", "stop", "50",
     "200.95", "rejected", "0", None, "o-msft-stop-1", None),
    ("o-msft-flatten", "orbiex-20261006-MSFT-flatten", DEFECT_DAY, "MSFT", "flatten", "buy", "market",
     "50", None, "filled", "50", "201.00", None, None),
    ("o-amd-entry", "orbiex-20261009-AMD-entry", OPEN_DAY, "AMD", "entry", "buy", "limit", "25",
     None, "filled", "25", "100.00", None, None),
]

FILLS = [
    # order_id, cum_qty, day, symbol, role, side, qty, price, ts
    ("o-iwm-entry", "10", CARRY_DAY, "IWM", "entry", "sell", "10", "30.00", "2026-10-01T19:55:00Z"),
    ("o-iwm-exit", "10", NO_PLAN_DAY, "IWM", "eod", "buy", "10", "29.00", "2026-10-02T13:31:00Z"),
    ("o-pltr-entry", "40", NO_PLAN_DAY, "PLTR", "entry", "buy", "40", "25.00", "2026-10-02T13:36:00Z"),
    ("o-pltr-exit", "40", NO_PLAN_DAY, "PLTR", "eod", "sell", "40", "24.90", "2026-10-02T19:55:00Z"),
    ("o-aapl-entry", "60", CLEAN_DAY, "AAPL", "entry", "buy", "60", "50.05", "2026-10-05T13:35:04Z"),
    ("o-aapl-entry", "100", CLEAN_DAY, "AAPL", "entry", "buy", "40", "50.15", "2026-10-05T13:35:09Z"),
    ("o-aapl-stop", "100", CLEAN_DAY, "AAPL", "stop", "sell", "100", "50.95", "2026-10-05T14:10:00Z"),
    ("o-msft-entry", "50", DEFECT_DAY, "MSFT", "entry", "sell", "50", "200.00", "2026-10-06T13:35:05Z"),
    ("o-msft-flatten", "25", DEFECT_DAY, "MSFT", "flatten", "buy", "25", "200.50", "2026-10-06T13:44:00Z"),
    ("o-msft-flatten", "50", DEFECT_DAY, "MSFT", "flatten", "buy", "25", "201.50", "2026-10-06T13:46:00Z"),
    ("o-amd-entry", "25", OPEN_DAY, "AMD", "entry", "buy", "25", "100.00", "2026-10-09T13:36:00Z"),
    # A fill row with no price: it carries no accounting value and must be reported as
    # dropped, never counted as a zero-priced fill.
    ("o-amd-entry", "0", OPEN_DAY, "AMD", "entry", "buy", "0", "", "2026-10-09T13:36:01Z"),
]

BREAKERS = [
    ("unprotected", "AMD", "entry filled with no protective stop", "2026-10-09T13:36:05Z"),
    ("broker_error", "", "repeated stop-order rejections", "2026-10-06T13:45:00Z"),
]

RISK = [("AMD", "2026-10-09T13:36:05Z", None, "entry_unprotected")]

INTENTS = [
    # intent_id, ts, symbol, trade_id, role, client_order_id, order_id, mutation,
    # desired_qty, desired_price, state, response, attempt
    ("i-0001", "2026-10-09T13:36:06Z", "AMD", "2026-10-09:AMD", "stop",
     "orbiex-20261009-AMD-stop", None, "place", "25", "99.5", "SENT", None, 1),
]

# ---------------------------------------------------------------------------
# append-only trading journal (runtime/journal/trade_journal.sqlite)
# ---------------------------------------------------------------------------

RUNS = [
    ("run-1", "2026-10-01T13:29:00Z", "America/New_York", "paper", "abc1234", "0.1.0", "config-7",
     "{}", "100000", "100000", "{}", "4", 20, "20000"),
    ("run-2", "2026-10-05T13:29:00Z", "America/New_York", "paper", "abc1234", "0.1.0", "config-7",
     "{}", "100042", "100042", "{}", "4", 20, "20000"),
]

TLOG_CANDIDATES = [
    # day, symbol, run_id, rank, direction, relvol, atr, adv, rules, action, skip_reason
    (CLEAN_DAY, "AAPL", "run-2", 1, 1, "2.1", "1.00", 20_000_000, rules(), "trade", None),
    (CLEAN_DAY, "TSLA", "run-2", 21, 1, "1.4", "8.00", 100_000_000, rules("in_top20"), "skip", "rank>20"),
    (DEFECT_DAY, "MSFT", "run-2", 1, -1, "1.9", "1.20", 25_000_000, rules(), "trade", None),
    (DEFECT_DAY, "NVDA", "run-2", None, 1, "1.6", "12.00", 900_000, rules("adv_ge_1m", ranked=False),
     "skip", "adv<1M"),
    (DEFECT_DAY, "ORCL", "run-2", 24, 1, "1.3", "2.40", 30_000_000, rules("in_top20"), "skip", "rank>20"),
]

TLOG_PLANS = [
    # day, symbol, run_id, direction, entry_trigger, planned_stop, planned_qty, one_r_per_share
    (CARRY_DAY, "IWM", "run-1", -1, "29.90", "30.40", 10, "0.50"),
    (CLEAN_DAY, "AAPL", "run-2", 1, "50.10", "49.60", 100, "0.50"),
    (DEFECT_DAY, "MSFT", "run-2", -1, "199.95", "200.95", 50, "1.00"),
]

TLOG_EVENTS = [
    # run_id, day, symbol, kind, role, detail
    ("run-2", DEFECT_DAY, "MSFT", "stop_failed", "stop",
     {"broker": "stop price too close to the market"}),
    ("run-2", CLEAN_DAY, "AAPL", "sizing_difference", "entry", {"unrounded": "100.0"}),
]


def _connect(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(schema)
    return con


def _insert(con: sqlite3.Connection, table: str, columns: tuple[str, ...], rows) -> None:
    placeholders = ",".join("?" * len(columns))
    con.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})", rows)


def write_state_journal(path: Path) -> None:
    con = _connect(path, journal_mod.SCHEMA)
    _insert(con, "days", ("day", "scanned", "scan_status", "session_closing", "eod_status", "summary"),
            [(d, scanned, status, closing, eod, json.dumps(summary))
             for d, scanned, status, closing, eod, summary in DAYS])
    _insert(con, "candidates", ("day", "symbol", "rank", "status", "reason", "details"),
            [(d, sym, rank, status, reason, json.dumps(details))
             for d, sym, rank, status, reason, details in CANDIDATES])
    _insert(con, "diagnostics", ("day", "kind", "body"),
            [(d, kind, json.dumps(body)) for d, kind, body in DIAGNOSTICS])
    _insert(con, "trades", ("day", "symbol", "direction", "planned_qty", "entry_price", "stop_price",
                            "atr", "entry_client_id", "entry_order_id", "stop_order_id",
                            "protected_qty", "closed", "flatten_attempts", "state"), TRADES)
    _insert(con, "orders", ("order_id", "client_order_id", "day", "symbol", "role", "side", "type",
                            "qty", "stop_price", "status", "filled_qty", "filled_avg_price",
                            "replaces", "replaced_by", "created_at", "updated_at"),
            [(*o, f"{o[2]}T13:35:03Z", f"{o[2]}T19:55:00Z") for o in ORDERS])
    _insert(con, "fills", ("order_id", "cum_qty", "day", "symbol", "role", "side", "qty", "price", "ts"),
            FILLS)
    _insert(con, "breakers", ("kind", "symbol", "detail", "ts"), BREAKERS)
    _insert(con, "risk", ("symbol", "unprotected_since", "exit_role", "reason"), RISK)
    _insert(con, "intents", ("intent_id", "ts", "symbol", "trade_id", "role", "client_order_id",
                             "order_id", "mutation", "desired_qty", "desired_price", "state",
                             "response", "attempt"), INTENTS)
    _insert(con, "meta", ("key", "value"), [("schema_version", "p0a")])
    _insert(con, "universe", ("month", "symbol", "rank", "price", "dollar_volume"),
            [(MONTH, "AAPL", 1, "1.00", "20000000"), (MONTH, "MSFT", 2, "1.20", "25000000")])
    _insert(con, "universe_meta", ("month", "built_at", "source_session", "first_session", "count"),
            [(MONTH, "2026-10-01T12:00:00Z", CARRY_DAY, CARRY_DAY, 2)])
    con.commit()
    con.close()


def write_trade_journal(path: Path) -> None:
    con = _connect(path, tradelog_mod.SCHEMA)
    _insert(con, "runs", ("run_id", "started_at", "timezone", "mode", "git_commit", "bot_version",
                          "config_version", "config", "equity", "cash", "bot_positions",
                          "max_gross_exposure", "max_positions", "per_position_cap", "recorded_at"),
            [(*r, r[1]) for r in RUNS])
    _insert(con, "candidates", ("day", "symbol", "run_id", "rank", "direction", "relvol", "atr", "adv",
                                "rules", "action", "skip_reason", "recorded_at"),
            [(d, sym, run, rank, direction, relvol, atr, adv, json.dumps(rules_),
              action, skip, f"{d}T13:35:00Z")
             for d, sym, run, rank, direction, relvol, atr, adv, rules_, action, skip in TLOG_CANDIDATES])
    _insert(con, "plans", ("day", "symbol", "run_id", "direction", "entry_trigger", "planned_stop",
                           "planned_qty", "one_r_per_share", "sizing", "recorded_at"),
            [(d, sym, run, direction, entry, stop, qty, one_r, json.dumps({"binding": "risk"}),
              f"{d}T13:35:00Z") for d, sym, run, direction, entry, stop, qty, one_r in TLOG_PLANS])
    _insert(con, "exec_events", ("dedupe_key", "run_id", "day", "symbol", "kind", "role", "detail",
                                 "event_time", "recorded_at"),
            [(f"{d}-{sym}-{kind}", run, d, sym, kind, role, json.dumps(detail), f"{d}T13:40:00Z",
              f"{d}T13:40:00Z") for run, d, sym, kind, role, detail in TLOG_EVENTS])
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# health, logs, research
# ---------------------------------------------------------------------------

HEARTBEAT = {"mode": "paper", "phase": "open", "session": OPEN_DAY, "health": "ok",
             "last_data_update": f"{OPEN_DAY}T13:36:00Z", "updated_at": f"{OPEN_DAY}T13:36:02Z",
             "pid": 4242, "variant": "ORB-IEX"}

PAPER_AUTO = {"updated_at": f"{OPEN_DAY}T20:05:00Z", "mode": "paper", "final_positions": {"AMD": 25},
              "final_orders": [], "flat_gate": False, "reason": "position still open"}

BROKER_SNAPSHOT = {
    "observed_at": f"{OPEN_DAY}T20:04:00Z",
    "source": "read-only broker acceptance check",
    "positions": {"AMD": 25},
    "orders": [{"id": "o-amd-entry", "symbol": "AMD", "side": "buy", "qty": "25", "status": "filled"}],
    "open_order_count": 0,
    "open_bot_orders": 0,
    "flat_gate": False,
    "paper_verified": True,
    "ownership_valid": True,
    "kill_switch_active": False,
    "journal_owned_qty": {"AMD": "25"},
    "breakers": [{"kind": "unprotected", "symbol": "AMD"}],
    "unresolved_intents": [{"intent_id": "i-0001"}],
    "blocked_reasons": ["position AMD still open with no protective stop"],
}

ORB_RESULTS = {
    "config_version": "config-7",
    "baseline": {
        "calibrated/paper": {
            "ALL": {"trades": 4210, "sessions": 905, "years": 5, "total_return": 1.82, "cagr": 0.23,
                    "sharpe": 1.14, "sortino": 1.62, "max_drawdown": -0.19, "win_rate": 0.47,
                    "expectancy_r": 0.11, "profit_factor": 1.32, "payoff": 1.49,
                    "avg_gross_leverage": 1.4, "turnover_per_year": 210.0, "end_equity": 282000.0},
            "2020-2021": {"trades": 1600, "sharpe": 1.31, "expectancy_r": 0.14, "profit_factor": 1.41},
        },
        "pessimistic/paper": {
            "ALL": {"trades": 4210, "sessions": 905, "sharpe": 0.71, "expectancy_r": 0.04,
                    "profit_factor": 1.11, "max_drawdown": -0.29, "win_rate": 0.44},
        },
    },
    "matrix": {"E1_or1": {"sharpe": 1.02, "expectancy_r": 0.08}, "E4_atrpct": {"sharpe": 1.19,
                                                                            "expectancy_r": 0.12}},
    "segments": {"long": {"expectancy_r": 0.13}, "short": {"expectancy_r": 0.07}},
}

LOG_DAYS = {
    CLEAN_DAY: [
        {"event": "scan", "level": "INFO", "msg": "CANDIDATES 2026-10-05 (2 ready)"},
        {"event": "signal", "level": "INFO", "msg": "SIGNAL AAPL LONG qty=100", "symbol": "AAPL",
         "direction": 1, "qty": 100},
        {"event": "entry_quote", "level": "INFO", "msg": "quote usable", "symbol": "AAPL",
         "verdict": "ok", "bid": "50.08", "ask": "50.10", "spread_pct": "0.04", "age_ms": 120},
        {"event": "fill", "level": "INFO", "msg": "AAPL entry filled 60", "symbol": "AAPL", "qty": 60},
        {"event": "trade_closed", "level": "INFO", "msg": "AAPL closed by stop", "symbol": "AAPL"},
    ],
    DEFECT_DAY: [
        {"event": "order_rejected", "level": "WARNING", "msg": "stop rejected", "symbol": "MSFT",
         "role": "stop", "error": "stop price too close to the market"},
        {"event": "entry_quote", "level": "WARNING", "msg": "quote stale", "symbol": "MSFT",
         "verdict": "stale_quote", "bid": "199.90", "ask": "200.10", "spread_pct": "0.10", "age_ms": 4200},
        {"event": "stream", "level": "WARNING", "msg": "trade stream reconnect after disconnect"},
        {"event": "flatten_not_working", "level": "ERROR", "msg": "flatten still working", "symbol": "MSFT"},
    ],
    OPEN_DAY: [
        {"event": "signal", "level": "INFO", "msg": "SIGNAL AMD LONG qty=25", "symbol": "AMD"},
        {"event": "stop_failed", "level": "ERROR", "msg": "could not place the protective stop",
         "symbol": "AMD", "role": "stop"},
    ],
}


def write_logs(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    for day, events in LOG_DAYS.items():
        records = []
        for i, event in enumerate(events):
            body = dict(event)
            body.update({"ts": f"{day}T13:36:{i:02d}Z", "level": body.pop("level", "INFO"),
                         "variant": "ORB-IEX"})
            records.append(json.dumps(body))
        (log_dir / f"orb_iex-{day.replace('-', '')}.jsonl").write_text("\n".join(records) + "\n")


def write_health_and_research(data_dir: Path) -> None:
    (data_dir / "health").mkdir(parents=True, exist_ok=True)
    (data_dir / "health" / "heartbeat.json").write_text(json.dumps(HEARTBEAT, indent=2))
    (data_dir / "health" / "paper-auto.json").write_text(json.dumps(PAPER_AUTO, indent=2))
    research = data_dir / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "broker_snapshot_20261009.json").write_text(json.dumps(BROKER_SNAPSHOT, indent=2))
    (research / "orb_results.json").write_text(json.dumps(ORB_RESULTS, indent=2))


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def write(tmp_path: Path) -> Path:
    """Write the whole fixture under ``tmp_path/runtime`` and return ``tmp_path``."""
    root = Path(tmp_path)
    data_dir = root / "runtime"
    write_state_journal(data_dir / "journal.sqlite")
    write_trade_journal(data_dir / "journal" / "trade_journal.sqlite")
    write_logs(data_dir / "logs")
    write_health_and_research(data_dir)
    (data_dir / "reports").mkdir(parents=True, exist_ok=True)
    return root


def settings_for(root: Path):
    """The bot's own Settings object, pointing at the fixture. No live paths involved."""
    return parse_settings({"runtime": {"data_dir": str(Path(root) / "runtime"),
                                       "mode": "paper"},
                           "journal": {"dir": str(Path(root) / "runtime" / "journal")}},
                          source="dashboard-fixture")


def sources_for(root: Path) -> Sources:
    return Sources.from_settings(settings_for(root), root=Path(root))
