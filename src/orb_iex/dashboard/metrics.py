"""Deterministic metrics. Every number here is a pure function of the journal.

Principles that the rest of the product inherits:

* Sample size is always reported next to the statistic. A metric computed from
  four trades is labelled as such, never presented as an edge.
* A ratio that is not statistically meaningful is ``None`` with a reason, never
  ``0`` and never a number with false precision. Sharpe and Sortino need at least
  ``MIN_DAYS_FOR_RATIO`` distinct trading days and a non-degenerate dispersion.
* Missing inputs are missing. ``_ratio`` refuses to annualise from no data.
* Accounting views stay in separate columns: realized (broker fills), modeled
  cost, modeled net, and backtest. They are never summed into one number.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from statistics import median as _median
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

from .. import constants as C
from .trades import D, ZERO, Trade, q

NY = ZoneInfo(C.MARKET_TZ)

# Validity thresholds. Explicit, so the UI can explain exactly why a ratio is absent.
MIN_DAYS_FOR_RATIO = 20          # distinct trading days before a Sharpe/Sortino is shown
MIN_TRADES_FOR_STATS = 5         # trades before expectancy/profit factor are shown
TRADING_DAYS_PER_YEAR = 252

# Reporting buckets.
ATR_PCT_BUCKETS = ((D("0"), D("0.5")), (D("0.5"), D("1")), (D("1"), D("2")), (D("2"), D("5")),
                   (D("5"), None))
DELAY_BUCKETS = ((0, 5), (5, 30), (30, 120), (120, None))
SLIPPAGE_R_BUCKETS = ((None, D("0")), (D("0"), D("0.05")), (D("0.05"), D("0.1")),
                      (D("0.1"), D("0.25")), (D("0.25"), None))
SPREAD_BUCKETS = ((D("0"), D("0.1")), (D("0.1"), D("0.25")), (D("0.25"), D("0.5")),
                  (D("0.5"), D("1")), (D("1"), None))
HOUR_BUCKETS = (("09:35-10:00", 9 * 60 + 35, 10 * 60), ("10:00-11:00", 10 * 60, 11 * 60),
                ("11:00-12:00", 11 * 60, 12 * 60), ("12:00-13:00", 12 * 60, 13 * 60),
                ("13:00-14:00", 13 * 60, 14 * 60), ("14:00-15:00", 14 * 60, 15 * 60),
                ("15:00-16:00", 15 * 60, 16 * 60))
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return D(str(v))
    except (InvalidOperation, ValueError):
        return None


def mean(xs: Sequence[Decimal]) -> Decimal | None:
    return sum(xs, ZERO) / len(xs) if xs else None


def med(xs: Sequence[Decimal]) -> Decimal | None:
    return D(_median(xs)) if xs else None


def stdev(xs: Sequence[Decimal]) -> Decimal | None:
    if len(xs) < 2:
        return None
    m = sum(xs, ZERO) / len(xs)
    var = sum(((x - m) ** 2 for x in xs), ZERO) / (len(xs) - 1)
    return var.sqrt()


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile. Small samples report exactly what they saw."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def dist(values: Sequence[float]) -> dict[str, Any]:
    """Distribution summary. n is always reported; an empty input is not a zero."""
    if not values:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None}
    return {"n": len(values), "min": round(min(values), 4),
            "p50": round(percentile(values, 50) or 0, 4),
            "p95": round(percentile(values, 95) or 0, 4), "max": round(max(values), 4)}


def cvar(xs: Sequence[Decimal]) -> Decimal | None:
    """Fifth-percentile tail mean, when there are enough observations to mean anything."""
    if len(xs) < MIN_TRADES_FOR_STATS:
        return None
    ordered = sorted(xs)
    cut = max(1, len(ordered) // 20)
    return sum(ordered[:cut], ZERO) / cut


def max_drawdown(pnls: Iterable[Decimal]) -> Decimal:
    peak = cum = worst = ZERO
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def bucket(value: Decimal | None, buckets, *, suffix: str = "") -> str | None:
    """Bucket label for a value, or None when it falls in no bucket (missing data).

    An open-ended top bucket is always labelled ``lo+``, so "10+" can never be
    rendered as a bare "10" that reads like an exact value.
    """
    if value is None:
        return None
    for lo, hi in buckets:
        if (lo is None or value >= lo) and (hi is None or value < hi):
            if suffix:
                return f"{lo}-{hi}{suffix}" if hi is not None else f"{lo}+{suffix}"
            if hi is None:
                return f"{lo}+"                   # open-ended top bucket, e.g. relvol "10+"
            return str(lo) if lo == hi else f"{lo}-{hi}"
    return None


def relvol_bucket(value: Decimal | None) -> str | None:
    return bucket(value, ((D("1"), D("2")), (D("2"), D("3")), (D("3"), D("5")), (D("5"), D("10")),
                          (D("10"), None)))


def atr_pct_bucket(value: Decimal | None) -> str | None:
    return bucket(value, ATR_PCT_BUCKETS, suffix="%")


def delay_bucket(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    for lo, hi in DELAY_BUCKETS:
        if seconds >= lo and (hi is None or seconds < hi):
            return f"{lo}-{hi}s" if hi is not None else f"{lo}s+"
    return None


def slippage_r_bucket(value: Decimal | None) -> str | None:
    if value is None:
        return None
    for lo, hi in SLIPPAGE_R_BUCKETS:
        if (lo is None or value >= lo) and (hi is None or value < hi):
            lo_s = "-inf" if lo is None else str(lo)
            return f"{lo_s}..{hi}R" if hi is not None else f"{lo_s}R+"
    return None


def spread_bucket(value: Decimal | None) -> str | None:
    return bucket(value, SPREAD_BUCKETS, suffix="%")


def weekday_bucket(day: str | None) -> str | None:
    if not day:
        return None
    try:
        return WEEKDAYS[date.fromisoformat(day).weekday()]
    except ValueError:
        return None


def hour_bucket(entry_time: str | None) -> str | None:
    if not entry_time:
        return None
    try:
        local = datetime.fromisoformat(entry_time).astimezone(NY)
    except ValueError:
        return None
    minutes = local.hour * 60 + local.minute
    for name, lo, hi in HOUR_BUCKETS:
        if lo <= minutes < hi:
            return name
    return None


def scheduled_scan(day: str, scan_delay_seconds: int) -> datetime | None:
    """09:35:03 ET by default: the open + the 5-minute range + the configured delay."""
    try:
        return datetime.combine(date.fromisoformat(day), time(9, 30), tzinfo=NY) + timedelta(
            minutes=C.OPENING_RANGE_MINUTES, seconds=scan_delay_seconds)
    except ValueError:
        return None


def entry_delay_seconds(day: str, entry_time: str | None, scan_delay_seconds: int = 3) -> float | None:
    target = scheduled_scan(day, scan_delay_seconds)
    if target is None or not entry_time:
        return None
    try:
        return (datetime.fromisoformat(entry_time) - target).total_seconds()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# performance block
# ---------------------------------------------------------------------------

def perf(trades: Sequence[Trade], *, label: str | None = None,
         incidents: int | None = None) -> dict[str, Any]:
    """One performance row. Counts and sample sizes always present, ratios gated."""
    realized = [t.realized for t in trades if t.realized is not None]
    nets = [t.net for t in trades if t.net is not None]
    rs = [t.r for t in trades if t.r is not None]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    flat = [x for x in nets if x == 0]
    modeled = [t.raw["modeled_cost"] for t in trades]
    defect_count = incidents if incidents is not None else sum(1 for t in trades if t.execution_defects)
    days = sorted({t.day for t in trades if t.day})
    enough = len(trades) >= MIN_TRADES_FOR_STATS
    gross_wins = sum(wins, ZERO)
    gross_losses = -sum(losses, ZERO)
    return {
        "label": label,
        "trades": len(trades),
        "trading_days": len(days),
        "days": days,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(flat),
        "win_rate": q(D(len(wins)) / len(nets), "0.0001") if nets else None,
        "realized_pnl": q(sum(realized, ZERO)),
        "recorded_fees": None,
        "recorded_fees_note": "Alpaca paper charges no commission; only modeled costs exist",
        "modeled_cost": q(sum(modeled, ZERO)),
        "modeled_net_pnl": q(sum(nets, ZERO)),
        "cost_per_trade": q(mean(modeled)) if modeled else None,
        "cost_per_trade_note": None if modeled else "no trades to average over",
        "expectancy_per_trade": q(mean(nets)) if (nets and enough) else None,
        "expectancy_r": q(mean(rs), "0.01") if (rs and enough) else None,
        "mean_r": q(mean(rs), "0.01") if rs else None,
        "median_r": q(med(rs), "0.01") if rs else None,
        "r_trades": len(rs),
        "r_unavailable": len(trades) - len(rs),
        "profit_factor": q(gross_wins / gross_losses, "0.01") if gross_losses else None,
        "profit_factor_note": None if gross_losses else "no losing trades: profit factor is undefined, not infinite",
        "average_win": q(mean(wins)) if wins else None,
        "average_loss": q(mean(losses)) if losses else None,
        "payoff_ratio": q((gross_wins / len(wins)) / (gross_losses / len(losses)), "0.01")
                         if wins and losses else None,
        "worst_trade": q(min(nets)) if nets else None,
        "best_trade": q(max(nets)) if nets else None,
        "cvar_5pct": q(cvar(nets)) if nets else None,
        "execution_incidents": defect_count,
        "execution_incident_rate": q(D(defect_count) / len(trades), "0.0001") if trades else None,
        "normal_strategy_trades": sum(1 for t in trades if t.loss_class == "normal_strategy"),
        "sample_note": None if enough else f"fewer than {MIN_TRADES_FOR_STATS} trades in this bucket",
        "valid": enough,
    }


# ---------------------------------------------------------------------------
# ratios
# ---------------------------------------------------------------------------

def daily_pnl(trades: Sequence[Trade], key: str = "net") -> list[tuple[str, Decimal]]:
    """Sum one P&L view per trading day, in day order. ``key`` is 'net' or 'realized'."""
    totals: dict[str, Decimal] = {}
    for t in trades:
        value = t.net if key == "net" else t.realized
        if value is None:
            continue
        totals[t.day] = totals.get(t.day, ZERO) + value
    return sorted(totals.items())


def ratios(trades: Sequence[Trade], base_equity: Decimal | None, *, key: str = "net") -> dict[str, Any]:
    """Annualised Sharpe and Sortino on the daily P&L series, with an explicit gate.

    Not shown unless the sample can support it: at least ``MIN_DAYS_FOR_RATIO``
    distinct trading days, a known base equity and non-degenerate dispersion.
    """
    series = daily_pnl(trades, key)
    days = len(series)
    out: dict[str, Any] = {"days": days, "key": key, "valid": False, "sharpe": None, "sortino": None,
                           "annualised_vol": None, "reason": None}
    if days < MIN_DAYS_FOR_RATIO:
        out["reason"] = (f"{days} trading day(s) with P&L; at least {MIN_DAYS_FOR_RATIO} are required "
                         "before an annualised risk ratio means anything")
        return out
    if not base_equity or base_equity <= 0:
        out["reason"] = "no recorded base equity, so daily returns cannot be formed"
        return out
    returns = [value / base_equity for _, value in series]
    sd = stdev(returns)
    m = mean(returns)
    if sd is None or sd == 0:
        out["reason"] = "zero dispersion in the daily returns"
        return out
    out["sharpe"] = str(((m / sd) * D(TRADING_DAYS_PER_YEAR).sqrt()).quantize(D("0.01")))
    out["annualised_vol"] = str((sd * D(TRADING_DAYS_PER_YEAR).sqrt() * 100).quantize(D("0.01")))
    downside = [x for x in returns if x < 0]
    if len(downside) < 2:
        out["reason"] = "fewer than two losing days, so downside deviation is not estimable"
        return out
    dd = stdev(downside) or (sum((x * x for x in downside), ZERO) / len(downside)).sqrt()
    if dd == 0:
        out["reason"] = "zero downside deviation"
        return out
    out["sortino"] = str(((m / dd) * D(TRADING_DAYS_PER_YEAR).sqrt()).quantize(D("0.01")))
    out["valid"] = True
    return out


def equity_curve(trades: Sequence[Trade], base_equity: Decimal | None,
                 recorded_points: Sequence[dict] | None = None) -> dict[str, Any]:
    """Cumulative realized and modeled-net P&L, in day order, plus drawdown.

    The account equity series the broker never gave us is NOT invented: the curve is
    cumulative P&L, and the bot's own recorded account equity at run start is returned
    separately so the two can be compared.
    """
    by_day: dict[str, dict[str, Decimal]] = {}
    for t in sorted(trades, key=lambda t: (t.day, t.raw["entry_time"] or "")):
        slot = by_day.setdefault(t.day, {"realized": ZERO, "net": ZERO})
        if t.realized is not None:
            slot["realized"] += t.realized
        if t.net is not None:
            slot["net"] += t.net
    points, cum_realized, cum_net = [], ZERO, ZERO
    for day in sorted(by_day):
        cum_realized += by_day[day]["realized"]
        cum_net += by_day[day]["net"]
        points.append({"day": day, "daily_realized": str(by_day[day]["realized"].quantize(D("0.01"))),
                       "daily_net": str(by_day[day]["net"].quantize(D("0.01"))),
                       "cum_realized": str(cum_realized.quantize(D("0.01"))),
                       "cum_net": str(cum_net.quantize(D("0.01")))})
    recorded = [{"t": p.get("t"), "equity": None if p.get("equity") is None else str(p["equity"]),
                 "git_commit": p.get("git_commit"), "config_version": p.get("config_version")}
                for p in (recorded_points or [])]
    return {"points": points,
            "recorded_equity_points": recorded,
            "recorded_equity_note": (None if recorded else
                                     "the account equity at run start is not recorded; only P&L is shown"),
            "cum_realized": str(cum_realized.quantize(D("0.01"))),
            "cum_net": str(cum_net.quantize(D("0.01"))),
            "max_drawdown": str(max_drawdown([
                t.net for t in sorted(trades, key=lambda t: (
                    t.raw.get("exit_time") or t.raw.get("entry_time") or t.day, t.symbol))
                if t.net is not None]).quantize(D("0.01"))),
            "max_drawdown_note": "peak-to-trough of closed-trade modeled-net P&L, ordered by exit; excludes unrealized excursions",
            "base_equity": None if base_equity is None else str(base_equity),
            "note": ("cumulative realized P&L from the broker's own fill prices; the account equity "
                     "series itself is not recorded per day")}


def stress(trades: Sequence[Trade]) -> dict[str, Any]:
    """What modeled net P&L becomes if the cost model is wrong by 2x or 3x."""
    realized = sum((t.realized for t in trades if t.realized is not None), ZERO)
    modeled = sum((t.raw["modeled_cost"] for t in trades), ZERO)
    out = {"realized_pnl": q(realized), "modeled_cost_1x": q(modeled)}
    for n in (2, 3):
        out[f"modeled_net_{n}x"] = q(realized - modeled * n)
    return out


# ---------------------------------------------------------------------------
# breakdowns
# ---------------------------------------------------------------------------

def _by(trades: Sequence[Trade], key) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        name = key(t)
        if name is None:
            continue
        groups.setdefault(str(name), []).append(t)
    return {name: perf(rows, label=name) for name, rows in sorted(groups.items())}


def spread_map(spread_series: dict[str, dict[str, Decimal | None]]) -> dict[str, Decimal | None]:
    """Flatten a per-trade spread lookup into the bucket key used by the breakdowns."""
    return spread_series


def breakdowns(trades: Sequence[Trade], *, scan_delay_seconds: int = 3,
               spreads: dict[str, Decimal | None] | None = None,
               delays: dict[str, float | None] | None = None) -> dict[str, Any]:
    """Every requested breakdown, each with its own sample size and validity flag."""
    spreads = spreads or {}
    delays = delays or {}
    atr_pct = {}
    for t in trades:
        if t.atr is not None and t.opening and dec(t.opening.get("open")):
            price = dec(t.opening.get("open"))
            atr_pct[t.trade_id] = (t.atr / price * 100) if price else None
        else:
            atr_pct[t.trade_id] = None
    slippage_r = {t.trade_id: (t.entry_slippage / t.one_r_per_share)
                  if (t.entry_slippage is not None and t.one_r_per_share) else None
                  for t in trades}
    return {
        "by_symbol": _by(trades, lambda t: t.symbol),
        "by_side": _by(trades, lambda t: t.side),
        "by_strategy_version": _by(trades, lambda t: t.strategy_version or "unversioned"),
        "by_time_of_day": _by(trades, lambda t: hour_bucket(t.raw["entry_time"])),
        "by_weekday": _by(trades, lambda t: weekday_bucket(t.day)),
        "by_relvol": _by(trades, lambda t: relvol_bucket(t.relvol)),
        "by_atr_pct": _by(trades, lambda t: atr_pct_bucket(atr_pct.get(t.trade_id))),
        "by_entry_delay": _by(trades, lambda t: delay_bucket(delays.get(t.trade_id))),
        "by_slippage": _by(trades, lambda t: slippage_r_bucket(slippage_r.get(t.trade_id))),
        "by_spread": _by(trades, lambda t: spread_bucket(spreads.get(t.trade_id))),
        "by_exit_reason": _by(trades, lambda t: t.exit_reason),
        "by_loss_class": _by(trades, lambda t: t.loss_class),
        "by_regime": {"_unavailable": {
            "reason": "no market/volatility regime is journaled per session",
            "data_needed": ["daily SPY close + 200-day average (or a regime flag) recorded per session",
                            "realized-volatility bucket per session"],
        }},
    }


def incidents_by_day(trades: Sequence[Trade]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in trades:
        slot = out.setdefault(t.day, {"trades": 0, "defects": 0, "broker": 0, "data": 0, "mixed": 0})
        slot["trades"] += 1
        slot["defects"] += 1 if t.execution_defects else 0
        slot["broker"] += 1 if t.broker_flags else 0
        slot["data"] += 1 if t.data_flags else 0
        slot["mixed"] += 1 if t.loss_class == "mixed" else 0
    return out
