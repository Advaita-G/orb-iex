"""Read-only data adapters for the journal dashboard.

Every source the bot already produces is reachable from here, in its own terms:

* ``StateJournal``    ``<data_dir>/journal.sqlite`` — the engine's durable state: days,
                      universe, candidates, trades, orders, fills, intents, breakers, risk.
* ``TradeJournal``    ``<journal_dir>/trade_journal[_<mode>].sqlite`` — the append-only
                      record: runs, candidates (+ rule checklist), plans, exec_events,
                      outcomes, day_summaries, and the strategy change log.
* ``HealthFiles``     ``<data_dir>/health/*.json`` — heartbeat, paper-auto runtime report,
                      shutdown report, startup failure.
* ``BrokerSnapshots`` ``research/*broker*.json`` — the last read-only broker acceptance
                      snapshots (positions + raw orders). Historical, timestamped.
* ``Research``        ``<data_dir>/research/*.json`` — backtest model output.
* ``LogEvents``       ``<data_dir>/logs/orb_iex-*.jsonl[.gz]`` — quote age, spread,
                      rejections, reconnects, flattens, sizing differences.
* ``LossJournal``     ``docs/loss-journal.jsonl`` — the daily loss review: each loss judged
                      a normal stop-out or a bot mistake, with its cause and fix status.

All connections use the SQLite ``mode=ro`` URI. No adapter here ever writes.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from functools import cached_property
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .. import tradelog


class SourceUnavailable(RuntimeError):
    """A source the dashboard needs is missing or unreadable. Never silently zeroed."""


def open_ro(path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    """Open a SQLite file strictly read-only. Raises SourceUnavailable if that is impossible."""
    p = Path(path)
    if not p.is_file():
        raise SourceUnavailable(f"no database at {p}")
    uri = p.resolve().as_uri() + f"?mode=ro&timeout={timeout}"
    try:
        db = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        raise SourceUnavailable(f"cannot open {p} read-only: {exc}") from exc
    db.row_factory = sqlite3.Row
    return db


def table_names(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def query(db: sqlite3.Connection, sql: str, args: Iterable = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in db.execute(sql, tuple(args))]


def have(db: sqlite3.Connection, *tables: str) -> bool:
    return table_names(db) >= set(tables)


class _ReadOnly:
    """Lazy, thread-safe, read-only handle to one SQLite file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._error: str | None = None

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def db(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is None:
                self._db = open_ro(self.path)
            return self._db

    def rows(self, sql: str, args: Iterable = ()) -> list[dict[str, Any]]:
        with self._lock:
            return query(self.db(), sql, args)

    def one(self, sql: str, args: Iterable = ()) -> dict[str, Any] | None:
        r = self.rows(sql, args)
        return r[0] if r else None

    def scalar(self, sql: str, args: Iterable = ()):
        row = self.one(sql, args)
        return None if row is None else next(iter(row.values()))

    def tables(self) -> set[str]:
        with self._lock:
            return table_names(self.db())

    def integrity(self) -> dict[str, Any]:
        """SQLite's own read-only opinion of the file. 'missing' is not 'ok'."""
        if not self.exists:
            return {"state": "missing", "detail": f"no file at {self.path}", "tables": []}
        try:
            with self._lock:
                result = self.db().execute("PRAGMA quick_check").fetchone()
                ok = bool(result) and str(result[0]).lower() == "ok"
                return {"state": "ok" if ok else "corrupt", "detail": None if ok else str(result[0]),
                        "tables": sorted(self.tables()), "size_bytes": self.path.stat().st_size}
        except (sqlite3.Error, OSError) as exc:
            return {"state": "unreadable", "detail": f"{type(exc).__name__}: {exc}", "tables": []}


# ---------------------------------------------------------------------------
# Engine state journal
# ---------------------------------------------------------------------------

class StateJournal(_ReadOnly):
    """``journal.sqlite``: what the engine itself persisted. Source of raw fills/orders."""

    def integrity(self) -> dict[str, Any]:
        report = super().integrity()
        if report["state"] != "ok":
            return report
        required = {"days", "universe", "candidates", "trades", "orders", "fills"}
        missing = sorted(required - set(report["tables"]))
        report["required_tables_missing"] = missing
        # 'breakers'/'intents'/'risk' arrive with the P0-A schema; their absence is a
        # documented legacy condition, not corruption.
        report["safety_tables"] = sorted({"breakers", "intents", "risk"} & set(report["tables"]))
        report["safety_tables_missing"] = sorted({"breakers", "intents", "risk"} - set(report["tables"]))
        return report

    def days(self) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        out = self.rows("SELECT day, scanned, scan_status, session_closing, eod_status, eod_started_at, done "
                        "FROM days ORDER BY day")
        for d in out:
            d["scanned"] = bool(d["scanned"])
            d["session_closing"] = bool(d["session_closing"])
            try:
                d["done"] = json.loads(d["done"] or "{}")
            except ValueError:
                d["done"] = {}
        return out

    def day(self, day: str) -> dict[str, Any] | None:
        return next((d for d in self.days() if d["day"] == day), None)

    def summary(self, day: str) -> dict[str, Any] | None:
        row = self.one("SELECT summary FROM days WHERE day=?", (day,))
        if not row or not row["summary"]:
            return None
        try:
            return json.loads(row["summary"])
        except ValueError:
            return None

    def universe_months(self) -> list[dict[str, Any]]:
        if not self.exists or "universe_meta" not in self.tables():
            return []
        return self.rows("SELECT month, built_at, source_session, first_session, count "
                         "FROM universe_meta ORDER BY month")

    def universe(self, month: str | None = None) -> list[dict[str, Any]]:
        """Eligible listing list; legacy columns hold day=month, ATR=price, ADV=dollar_volume."""
        if not self.exists:
            return []
        if month:
            return self.rows("SELECT month, symbol, rank, price AS atr, dollar_volume AS adv "
                             "FROM universe WHERE month=? ORDER BY rank", (month,))
        return self.rows("SELECT month, symbol, rank, price AS atr, dollar_volume AS adv "
                         "FROM universe ORDER BY month, rank")

    def candidates(self, day: str | None = None) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        sql = ("SELECT day, symbol, rank, status, reason, details FROM candidates"
               + (" WHERE day=?" if day else "") + " ORDER BY day, rank")
        out = self.rows(sql, (day,) if day else ())
        for c in out:
            try:
                c["details"] = json.loads(c["details"]) if c["details"] else {}
            except ValueError:
                c["details"] = {}
        return out

    def trades(self, day: str | None = None) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        sql = "SELECT * FROM trades" + (" WHERE day=?" if day else "") + " ORDER BY day, symbol"
        return self.rows(sql, (day,) if day else ())

    def orders(self, day: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        where, args = [], []
        if day:
            where.append("day=?")
            args.append(day)
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        sql = "SELECT * FROM orders" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created_at"
        return self.rows(sql, args)

    def fills(self, day: str | None = None) -> list[dict[str, Any]]:
        if not self.exists:
            return []
        sql = "SELECT * FROM fills" + (" WHERE day=?" if day else "") + " ORDER BY ts, order_id, cum_qty"
        return self.rows(sql, (day,) if day else ())

    def intents(self, unresolved_only: bool = False) -> list[dict[str, Any]]:
        if not self.exists or "intents" not in self.tables():
            return []
        sql = "SELECT * FROM intents"
        if unresolved_only:
            sql += " WHERE state != 'TERMINAL'"
        return self.rows(sql + " ORDER BY ts, intent_id")

    def breakers(self) -> list[dict[str, Any]]:
        if not self.exists or "breakers" not in self.tables():
            return []
        return self.rows("SELECT kind, symbol, detail, ts FROM breakers ORDER BY ts")

    def risk(self) -> list[dict[str, Any]]:
        if not self.exists or "risk" not in self.tables():
            return []
        return self.rows("SELECT symbol, unprotected_since, exit_role, reason FROM risk ORDER BY symbol")

    def diagnostics(self, day: str | None = None, *, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.exists or "diagnostics" not in self.tables():
            return []
        filters = [(column, value) for column, value in (("day", day), ("kind", kind)) if value is not None]
        sql = "SELECT day, kind, body FROM diagnostics"
        if filters:
            sql += " WHERE " + " AND ".join(column + "=?" for column, _ in filters)
        out = self.rows(sql + " ORDER BY day, kind", tuple(value for _, value in filters))
        for d in out:
            try:
                d["body"] = json.loads(d["body"]) if d["body"] else {}
            except ValueError:
                d["body"] = {}
        return out

    def meta(self) -> dict[str, Any]:
        if not self.exists:
            return {}
        return {r["key"]: r["value"] for r in self.rows("SELECT key, value FROM meta")}

    def owned_qty(self) -> dict[str, str]:
        """Signed bot-owned shares: net of every recorded fill, plus corrections."""
        if not self.exists:
            return {}
        out: dict[str, float] = {}
        for r in self.rows("SELECT symbol, side, qty FROM fills"):
            q = float(r["qty"])
            out[r["symbol"]] = out.get(r["symbol"], 0.0) + (q if r["side"] == "buy" else -q)
        if "owned_adjust" in self.tables():
            for r in self.rows("SELECT symbol, qty FROM owned_adjust"):
                out[r["symbol"]] = out.get(r["symbol"], 0.0) + float(r["qty"])
        return {s: str(round(q, 6)) for s, q in sorted(out.items()) if abs(q) > 1e-9}

    def emergency_fallback(self) -> dict[str, Any]:
        """The write-ahead file the MutationGate uses when SQLite is unavailable."""
        p = Path(str(self.path) + ".emergency.jsonl")
        if not p.is_file():
            return {"present": False, "records": 0, "corrupt_lines": 0, "path": str(p)}
        bad = total = 0
        for line in p.read_text(errors="replace").splitlines():
            if not line.strip():
                continue
            total += 1
            try:
                json.loads(line)
            except ValueError:
                bad += 1
        return {"present": True, "records": total, "corrupt_lines": bad, "path": str(p)}


# ---------------------------------------------------------------------------
# Append-only trading journal
# ---------------------------------------------------------------------------

TRADELOG_TABLES = ("runs", "candidates", "plans", "exec_events", "outcomes", "day_summaries")


class TradeJournal(_ReadOnly):
    """``trade_journal.sqlite``: the append-only record (record only, never a decision input)."""

    def integrity(self) -> dict[str, Any]:
        report = super().integrity()
        if report["state"] != "ok":
            return report
        missing = sorted(set(TRADELOG_TABLES) - set(report["tables"]))
        report["required_tables_missing"] = missing
        return report

    def runs(self) -> list[dict[str, Any]]:
        if not self.exists or "runs" not in self.tables():
            return []
        out = self.rows("SELECT run_id, started_at, timezone, mode, git_commit, bot_version, config_version, "
                        "config, equity, cash, bot_positions, max_gross_exposure, max_positions, "
                        "per_position_cap, feeds, recorded_at FROM runs ORDER BY started_at")
        for r in out:
            for k in ("config", "bot_positions", "feeds"):
                if isinstance(r.get(k), str):
                    try:
                        r[k] = json.loads(r[k])
                    except ValueError:
                        pass
        return out

    def latest_run(self, day: str | None = None) -> dict[str, Any] | None:
        runs = self.runs()
        if day:
            runs = [r for r in runs if (r.get("started_at") or "")[:10] <= day] or runs
        return runs[-1] if runs else None

    def candidates(self, day: str | None = None) -> list[dict[str, Any]]:
        if not self.exists or "candidates" not in self.tables():
            return []
        sql = "SELECT * FROM candidates" + (" WHERE day=?" if day else "") + " ORDER BY day, rank, symbol"
        out = self.rows(sql, (day,) if day else ())
        for c in out:
            for k in ("rules",):
                if isinstance(c.get(k), str):
                    try:
                        c[k] = json.loads(c[k])
                    except ValueError:
                        c[k] = {}
            for k in ("open", "high", "low", "close", "relvol", "atr", "adv"):
                c[k] = _num(c.get(k))
        return out

    def candidate_days(self) -> list[str]:
        if not self.exists or "candidates" not in self.tables():
            return []
        return [r["day"] for r in self.rows("SELECT DISTINCT day FROM candidates ORDER BY day")]

    def plans(self, day: str | None = None) -> list[dict[str, Any]]:
        if not self.exists or "plans" not in self.tables():
            return []
        sql = "SELECT * FROM plans" + (" WHERE day=?" if day else "") + " ORDER BY day, symbol"
        out = self.rows(sql, (day,) if day else ())
        for p in out:
            if isinstance(p.get("sizing"), str):
                try:
                    p["sizing"] = json.loads(p["sizing"])
                except ValueError:
                    pass
        return out

    def events(self, day: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        if not self.exists or "exec_events" not in self.tables():
            return []
        where, args = [], []
        if day:
            where.append("day=?")
            args.append(day)
        if symbol:
            where.append("symbol=?")
            args.append(symbol)
        sql = ("SELECT seq, dedupe_key, run_id, day, symbol, kind, role, order_type, side, client_order_id, "
               "order_id, qty, price, cum_qty, status, detail, event_time, recorded_at FROM exec_events"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY event_time, seq")
        return self.rows(sql, args)

    def outcomes(self, day: str | None = None) -> list[dict[str, Any]]:
        if not self.exists or "outcomes" not in self.tables():
            return []
        sql = "SELECT * FROM outcomes" + (" WHERE day=?" if day else "") + " ORDER BY day, entry_time"
        return self.rows(sql, (day,) if day else ())

    def day_summaries(self) -> list[dict[str, Any]]:
        if not self.exists or "day_summaries" not in self.tables():
            return []
        out = self.rows("SELECT day, run_id, body, recorded_at FROM day_summaries ORDER BY day")
        for r in out:
            try:
                r["body"] = json.loads(r["body"]) if r["body"] else {}
            except ValueError:
                r["body"] = {}
        return out

    def strategy_changes(self, path: Path | None = None) -> list[dict[str, Any]]:
        p = Path(path) if path else self.path.parent / "strategy_changes.sqlite"
        if not p.is_file():
            return []
        other = _ReadOnly(p)
        if "strategy_changes" not in set(other.integrity().get("tables", [])):
            return []
        return other.rows("SELECT * FROM strategy_changes ORDER BY seq")

    def trigger_names(self) -> list[str]:
        if not self.exists:
            return []
        return sorted(r["name"] for r in self.rows(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE '%_no_%'"))


# ---------------------------------------------------------------------------
# Health, broker snapshots, research, logs
# ---------------------------------------------------------------------------

HEALTH_FILES = {
    "heartbeat": "health/heartbeat.json",
    "paper_auto": "health/paper-auto.json",
    "shutdown": "health/shutdown.json",
    "startup_failure": "health/paper-auto-startup-failure.json",
}


class HealthFiles:
    """JSON health/runtime reports written by the supervisor. Read-only, never rewritten."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    def _read(self, rel: str) -> dict[str, Any] | None:
        p = self.data_dir / rel
        if not p.is_file():
            return None
        try:
            body = json.loads(p.read_text())
        except (OSError, ValueError):
            return {"_unreadable": True, "_path": str(p)}
        body["_path"] = str(p)
        body["_mtime"] = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
        return body

    def heartbeat(self) -> dict[str, Any] | None:
        return self._read(HEALTH_FILES["heartbeat"])

    def paper_auto(self) -> dict[str, Any] | None:
        return self._read(HEALTH_FILES["paper_auto"])

    def shutdown(self) -> dict[str, Any] | None:
        return self._read(HEALTH_FILES["shutdown"])

    def startup_failure(self) -> dict[str, Any] | None:
        return self._read(HEALTH_FILES["startup_failure"])

    def all(self) -> dict[str, Any]:
        return {k: self._read(v) for k, v in HEALTH_FILES.items()}


class BrokerSnapshots:
    """Historical read-only broker acceptance snapshots saved under ``research/``.

    These are the strongest accounting evidence the repository holds for the days
    they cover. They are timestamped, so freshness is always shown, and older
    days are explicitly out of scope rather than assumed flat.
    """

    NAMES = ("broker_snapshot_*.json", "merge_gate_broker_*.json", "final_broker_status_*.json")

    def __init__(self, research_dir: str | Path):
        self.research_dir = Path(research_dir)

    def list(self) -> list[dict[str, Any]]:
        out = []
        for pattern in self.NAMES:
            for p in sorted(self.research_dir.glob(pattern)):
                try:
                    body = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                out.append({"name": p.name, "path": str(p), "observed_at": body.get("observed_at"),
                            "source": body.get("source"), "positions": body.get("positions"),
                            "open_orders": body.get("open_orders") or body.get("orders") or [],
                            "open_order_count": body.get("open_order_count"),
                            "open_bot_orders": body.get("open_bot_orders"),
                            "flat_gate": body.get("flat_gate"), "paper_verified": body.get("paper_verified"),
                            "ownership_valid": body.get("ownership_valid"),
                            "kill_switch_active": body.get("kill_switch_active"),
                            "journal_owned_qty": body.get("journal_owned_qty"),
                            "breaker_count": len(body.get("breakers") or []),
                            "unresolved_intent_count": len(body.get("unresolved_intents") or []),
                            "blocked_reasons": body.get("blocked_reasons")})
        return sorted(out, key=lambda r: r["observed_at"] or "")

    def latest(self) -> dict[str, Any] | None:
        rows = self.list()
        return rows[-1] if rows else None

    def raw_orders(self, name: str) -> list[dict[str, Any]]:
        p = self.research_dir / name
        if not p.is_file():
            return []
        try:
            return json.loads(p.read_text()).get("orders") or []
        except (OSError, ValueError):
            return []


class Research:
    """Backtest / research model output. Aggregate only, as produced."""

    def __init__(self, research_dir: str | Path):
        self.research_dir = Path(research_dir)

    def _read(self, name: str) -> dict[str, Any] | None:
        p = self.research_dir / name
        if not p.is_file():
            return None
        try:
            body = json.loads(p.read_text())
            body["_path"] = str(p)
            body["_mtime"] = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()
            return body
        except (OSError, ValueError):
            return None

    def orb(self) -> dict[str, Any] | None:
        return self._read("orb_results.json")

    def alternative(self) -> dict[str, Any] | None:
        return self._read("alt_results.json")

    def audit(self) -> dict[str, Any] | None:
        return self._read("audit.json")


class LogEvents:
    """Filtered reader over the daily JSON-lines logs, gz-aware.

    Only the requested kinds are retained, so a 2 MB session log costs almost nothing.
    """

    def __init__(self, log_dir: str | Path, data_dir: str | Path | None = None):
        self.log_dir = Path(log_dir)
        self.data_dir = Path(data_dir) if data_dir else self.log_dir.parent
        self._cache: dict[tuple, list[dict[str, Any]]] = {}
        self._lock = threading.RLock()

    def available_days(self) -> list[str]:
        days = set()
        for p in self.log_dir.glob("orb_iex-*.jsonl*"):
            stem = p.name.split(".")[0]
            if len(stem) == len("orb_iex-YYYYMMDD"):
                days.add(stem[len("orb_iex-"):])
        return [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in sorted(days)]

    def _paths(self, day: str) -> list[Path]:
        stamp = day.replace("-", "")
        return [self.log_dir / f"orb_iex-{stamp}.jsonl", self.log_dir / f"orb_iex-{stamp}.jsonl.gz"]

    def stream(self, day: str) -> Iterator[dict[str, Any]]:
        for p in self._paths(day):
            if not p.is_file():
                continue
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "rt", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
            return

    def events(self, day: str, kinds: Iterable[str] | None = None,
               levels: Iterable[str] | None = None) -> list[dict[str, Any]]:
        kinds = frozenset(kinds) if kinds else None
        levels = frozenset(levels) if levels else None
        key = (day, kinds, levels)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            out = []
            for record in self.stream(day):
                if kinds is not None and record.get("event") not in kinds:
                    continue
                if levels is not None and record.get("level") not in levels:
                    continue
                out.append(record)
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[key] = out
            return out

    def count_by_kind(self, days: Iterable[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for day in days:
            for record in self.stream(day):
                e = record.get("event") or "?"
                counts[e] = counts.get(e, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------

def _num(v):
    """Decimal-ish column -> float, or None. An empty string is missing, not zero."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


CONFIG_KEYS = ("variant", "mode", "data_dir", "journal_dir", "kill_switch_file", "commission_per_share",
               "slippage_per_share", "max_gross_exposure", "max_quote_age_seconds", "max_quote_spread_pct",
               "scan_delay_seconds", "entry_cutoff_time", "opening_feed", "history_feed", "adjustment",
               "enable_paper_orders", "expected_account_prefix", "min_free_disk_mb", "log_retention_days",
               "history_min_age_minutes", "order_poll_seconds")


def config_summary(settings) -> dict[str, Any]:
    """The settings the dashboard is allowed to display. No secret ever reaches here.

    Only the keys above are read, so inventing a new setting never leaks it into a page.
    """
    out: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        if hasattr(settings, key):
            out[key] = str(getattr(settings, key))
    out["source"] = getattr(settings, "source", None)
    return out


LOSS_VERDICTS = ("mistake", "unlucky", "unknown")   # unknown: the record cannot support either verdict
LOSS_ERAS = ("pre-hardening", "current")                # before or after the 2026-10-02 execution hardening
FIX_STATUSES = ("none", "proposed", "approved", "shipped", "not_needed")


class LossJournal:
    """``docs/loss-journal.jsonl``: one reviewed loss (or bot incident) per line.

    Written by the daily after-close review, read here only. A malformed line is reported
    in ``errors`` with its line number, never dropped silently.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"state": "missing", "path": str(self.path), "entries": [], "errors": []}
        entries, errors = [], []
        with open(self.path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    errors.append({"line": n, "error": f"not JSON: {exc}"})
                    continue
                problem = _loss_entry_problem(row)
                if problem:
                    errors.append({"line": n, "error": problem})
                    continue
                entries.append(row)
        return {"state": "ok", "path": str(self.path), "entries": entries, "errors": errors}


def _loss_entry_problem(row: Any) -> str | None:
    if not isinstance(row, dict):
        return "entry is not an object"
    for key in ("day", "verdict", "title", "cause"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            return f"missing {key}"
    if row["verdict"] not in LOSS_VERDICTS:
        return f"verdict must be one of {LOSS_VERDICTS}"
    if row["verdict"] == "mistake" and not (isinstance(row.get("category"), str) and row["category"].strip()):
        return "a mistake needs a category (short kebab-case kind, reused for repeats)"
    if row.get("era") is not None and row["era"] not in LOSS_ERAS:
        return f"era must be one of {LOSS_ERAS}"
    fix = row.get("fix") or {"status": "none"}
    if not isinstance(fix, dict) or fix.get("status") not in FIX_STATUSES:
        return f"fix.status must be one of {FIX_STATUSES}"
    if fix["status"] in ("proposed", "approved", "shipped") and not fix.get("test"):
        return "a fix must name its regression test (fix.test)"
    return None


@dataclass
class Sources:
    """Everything the dashboard reads, resolved once. No writes, no network."""

    data_dir: Path
    journal_dir: Path
    research_dir: Path
    log_dir: Path
    mode: str = "paper"
    settings_source: str | None = None
    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings, *, root: str | Path = ".", mode: str | None = None) -> "Sources":
        """Build from the bot's own Settings object: same paths, same mode, no writes.

        ``mode`` overrides ``settings.runtime.mode``, which is what `run` does (by default
        "readonly") and therefore is usually not the mode whose journal exists. The caller
        that knows which journal it wants passes it in explicitly.
        """
        root = Path(root)
        data_dir = Path(settings.data_dir)
        journal_dir = Path(settings.journal_dir)
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        if not journal_dir.is_absolute():
            journal_dir = root / journal_dir
        return cls(data_dir=data_dir, journal_dir=journal_dir, research_dir=data_dir / "research",
                   log_dir=data_dir / "logs", mode=mode or getattr(settings, "mode", "paper"),
                   settings_source=getattr(settings, "source", None), config=config_summary(settings))

    @classmethod
    def from_root(cls, root: str | Path, *, data_dir: str | Path = "runtime",
                  journal_dir: str | Path | None = None, mode: str = "paper",
                  settings_source: str | None = None, config: dict[str, Any] | None = None) -> "Sources":
        root = Path(root)
        data = root / data_dir if not Path(data_dir).is_absolute() else Path(data_dir)
        journal = Path(journal_dir) if journal_dir else data / "journal"
        return cls(data_dir=data, journal_dir=journal, research_dir=data / "research",
                   log_dir=data / "logs", mode=mode, settings_source=settings_source,
                   config=dict(config or {}))

    # -- lazy handles ---------------------------------------------------------
    @cached_property
    def state_journal(self) -> StateJournal:
        return StateJournal(self.data_dir / "journal.sqlite")

    @cached_property
    def paper_journal(self) -> StateJournal:
        """The pre-recovery copy kept beside the live journal, when it exists."""
        return StateJournal(self.data_dir / "journal-pre-recovery.sqlite")

    @cached_property
    def trade_journal(self) -> TradeJournal:
        """The same file the bot writes for this mode (one definition, in the writer's module)."""
        path, _ = tradelog.journal_paths(self.journal_dir, self.mode)
        return TradeJournal(path)

    @property
    def health(self) -> HealthFiles:
        return HealthFiles(self.data_dir)

    @property
    def broker_snapshots(self) -> BrokerSnapshots:
        return BrokerSnapshots(self.research_dir)

    @property
    def research(self) -> Research:
        return Research(self.research_dir)

    @property
    def logs(self) -> LogEvents:
        return LogEvents(self.log_dir, self.data_dir)

    @property
    def loss_journal(self) -> LossJournal:
        """Kept in the repo (``<project>/docs``) so every review and fix is versioned."""
        return LossJournal(self.data_dir.parent / "docs" / "loss-journal.jsonl")

    @property
    def research_ledger(self):
        """``docs/experiment-ledger.jsonl``: hypotheses, experiments and decisions, versioned in the repo."""
        from ..research_ledger import ResearchLedger
        return ResearchLedger(self.data_dir.parent / "docs" / "experiment-ledger.jsonl")

    def available(self) -> dict[str, Any]:
        sj, tj = self.state_journal, self.trade_journal
        return {
            "state_journal": sj.integrity(),
            "state_journal_pre_recovery": self.paper_journal.integrity(),
            "trade_journal": tj.integrity(),
            "strategy_change_log": (self.journal_dir / "strategy_changes.sqlite").is_file(),
            "health": sorted(k for k, v in self.health.all().items() if v is not None),
            "broker_snapshots": len(self.broker_snapshots.list()),
            "research": {"orb_results": self.research.orb() is not None,
                         "alt_results": self.research.alternative() is not None},
            "log_days": self.logs.available_days(),
            "log_today": self.log_dir.joinpath(
                "orb_iex-{}.jsonl".format(date.today().strftime("%Y%m%d"))).is_file(),
            "cache_journal": (self.data_dir / "cache.sqlite").is_file(),
            "kill_switch_file": self.config.get("kill_switch_file"),
        }
