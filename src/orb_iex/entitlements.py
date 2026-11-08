"""Stage 1 read-only entitlement checks against Alpaca (paper + free data).

Every call here is a GET. Nothing is submitted, replaced or canceled.

Required checks (Stage 1 passes only if all pass):
  account          paper account, ACTIVE, keys accepted by the paper host
  clock            /v2/clock works; local clock skew is measured
  calendar         /v2/calendar returns sessions (open/close incl. early closes)
  assets           asset list with shortable / easy_to_borrow flags
  iex_latest       free real-time IEX bars are available
  iex_opening_bar  a past 09:30-09:35 IEX 5-minute bar can be fetched
  sip_history      free SIP daily bars older than the minimum age are available

Informational checks (recorded, do not fail Stage 1):
  sip_recent_blocked       recent SIP is refused (expected on the Basic plan)
  max_symbols_per_request  largest multi-symbol request that succeeded
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import constants as C
from .config import Settings
from .http import ApiResponse, ReadOnlyClient
from .logging_setup import LOGGER_NAME, log_event
from .safety import SafetyError, check_paper_account

log = logging.getLogger(LOGGER_NAME)
NY = ZoneInfo(C.MARKET_TZ)

PROBE_SYMBOLS = ["SPY", "AAPL", "MSFT", "NVDA", "TSLA"]
SYMBOL_COUNT_STEPS = (200, 1000, 2000, 4000)
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")
MAX_CLOCK_SKEW_SECONDS = 2.0


@dataclass
class Probe:
    name: str
    required: bool
    ok: bool | None = None  # True pass, False fail, None informational/unknown
    expected: str = ""
    observed: dict[str, Any] = field(default_factory=dict)
    note: str = ""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_times(day: dict) -> tuple[datetime, datetime]:
    d = date.fromisoformat(day["date"])
    o_h, o_m = map(int, day["open"].split(":"))
    c_h, c_m = map(int, day["close"].split(":"))
    return (datetime(d.year, d.month, d.day, o_h, o_m, tzinfo=NY),
            datetime(d.year, d.month, d.day, c_h, c_m, tzinfo=NY))


def _bars_by_symbol(resp: ApiResponse) -> dict[str, Any]:
    if not resp.ok or not isinstance(resp.data, dict):
        return {}
    bars = resp.data.get("bars") or {}
    return bars if isinstance(bars, dict) else {}


def _record(probe: Probe, ok: bool | None, **observed) -> Probe:
    probe.ok = ok
    probe.observed.update(observed)
    level = logging.INFO if ok is not False else logging.ERROR
    state = {True: "PASS", False: "FAIL", None: "INFO"}[ok]
    log_event(log, level, "entitlement_probe", f"{state} {probe.name}: {probe.note or probe.expected}",
              probe=probe.name, ok=ok, **observed)
    return probe


def run_entitlement_checks(client: ReadOnlyClient, settings: Settings,
                           now_utc: datetime | None = None) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    probes: list[Probe] = []

    # --- account: paper guard. Any problem here stops everything. -----------
    p = Probe("account", True, expected="paper account, status ACTIVE")
    resp = client.trading_get("/v2/account", purpose="probe:account")
    if resp.status in (401, 403):
        raise SafetyError(
            f"paper host rejected the keys (HTTP {resp.status}). Use PAPER API keys; "
            "live keys do not authenticate on the paper host."
        )
    if not resp.ok or not isinstance(resp.data, dict):
        raise SafetyError(f"/v2/account failed (HTTP {resp.status}): {resp.text[:200]}")
    summary = check_paper_account(resp.data, settings.expected_account_prefix)
    blocked = bool(summary.get("trading_blocked") or summary.get("account_blocked"))
    p.note = "paper account is blocked for trading" if blocked else "paper account confirmed"
    probes.append(_record(p, not blocked, **summary))

    # --- clock ---------------------------------------------------------------
    p = Probe("clock", True, expected="/v2/clock responds; skew <= 2s")
    local_before = datetime.now(timezone.utc)
    resp = client.trading_get("/v2/clock", purpose="probe:clock")
    local_after = datetime.now(timezone.utc)
    if resp.ok and isinstance(resp.data, dict) and resp.data.get("timestamp"):
        server = datetime.fromisoformat(resp.data["timestamp"])
        local_mid = local_before + (local_after - local_before) / 2
        skew = round((local_mid - server).total_seconds(), 3)
        p.note = f"clock skew {skew:+.3f}s"
        ok = abs(skew) <= MAX_CLOCK_SKEW_SECONDS
        if not ok:
            p.note += " (too large: the 09:35 scan timing depends on an accurate clock; sync with NTP)"
        probes.append(_record(p, ok, is_open=resp.data.get("is_open"),
                              next_open=resp.data.get("next_open"),
                              next_close=resp.data.get("next_close"), skew_seconds=skew))
    else:
        p.note = f"HTTP {resp.status}"
        probes.append(_record(p, False, status=resp.status))

    # --- calendar --------------------------------------------------------------
    p = Probe("calendar", True, expected="sessions for the last 10 and next 14 days")
    today_ny = now_utc.astimezone(NY).date()
    resp = client.trading_get("/v2/calendar", params={
        "start": (today_ny - timedelta(days=10)).isoformat(),
        "end": (today_ny + timedelta(days=14)).isoformat(),
    }, purpose="probe:calendar")
    sessions = resp.data if resp.ok and isinstance(resp.data, list) else []
    history_cutoff = now_utc - timedelta(minutes=settings.history_min_age_minutes)
    past = [d for d in sessions if _session_times(d)[1] <= history_cutoff]
    last_session = past[-1] if past else None
    early = [d["date"] for d in sessions if _session_times(d)[1].hour < 16]
    p.note = f"{len(sessions)} sessions; last usable history session {last_session and last_session['date']}"
    probes.append(_record(p, bool(sessions) and last_session is not None,
                          sessions=len(sessions), early_closes=early,
                          last_history_session=last_session and last_session["date"]))

    # --- assets ------------------------------------------------------------
    p = Probe("assets", True, expected="active US equities with shortable flags")
    resp = client.trading_get("/v2/assets", params={"status": "active", "asset_class": "us_equity"},
                              purpose="probe:assets")
    assets = resp.data if resp.ok and isinstance(resp.data, list) else []
    tradable = [a for a in assets if a.get("tradable")]
    by_exchange: dict[str, int] = {}
    for a in tradable:
        by_exchange[a.get("exchange", "?")] = by_exchange.get(a.get("exchange", "?"), 0) + 1
    symbols = sorted(a["symbol"] for a in tradable
                     if SYMBOL_RE.match(a.get("symbol", "")) and a.get("exchange") != "OTC")
    p.note = f"{len(tradable)} tradable, {sum(1 for a in tradable if a.get('easy_to_borrow'))} easy to borrow"
    probes.append(_record(p, len(tradable) > 0 and "SPY" in {a.get("symbol") for a in assets},
                          active=len(assets), tradable=len(tradable),
                          shortable=sum(1 for a in tradable if a.get("shortable")),
                          easy_to_borrow=sum(1 for a in tradable if a.get("easy_to_borrow")),
                          by_exchange=by_exchange, probe_symbol_pool=len(symbols)))

    # --- free real-time IEX ----------------------------------------------------
    p = Probe("iex_latest", True, expected="HTTP 200 with latest IEX bars")
    resp = client.data_get("/v2/stocks/bars/latest",
                           params={"symbols": ",".join(PROBE_SYMBOLS), "feed": "iex"},
                           purpose="probe:iex_latest")
    got = _bars_by_symbol(resp)
    p.note = f"HTTP {resp.status}, {len(got)} symbols"
    probes.append(_record(p, resp.ok and len(got) > 0, status=resp.status,
                          symbols=sorted(got), latest={s: b.get("t") for s, b in got.items()}))

    # --- past IEX opening bar (the exact request shape used at 09:35) --------
    p = Probe("iex_opening_bar", True, expected="09:30-09:35 IEX 5Min bars for a past session")
    if last_session:
        open_dt, _ = _session_times(last_session)
        resp = client.data_get("/v2/stocks/bars", params={
            "symbols": ",".join(PROBE_SYMBOLS), "timeframe": "5Min",
            "start": _iso(open_dt), "end": _iso(open_dt + timedelta(minutes=C.OPENING_RANGE_MINUTES)),
            "feed": settings.opening_feed, "adjustment": settings.adjustment, "limit": 10000,
        }, purpose="probe:iex_opening_bar")
        got = _bars_by_symbol(resp)
        sample = {s: {k: b[0].get(k) for k in ("t", "o", "h", "l", "c", "v", "n")}
                  for s, b in got.items() if b}
        p.note = f"session {last_session['date']}: HTTP {resp.status}, {len(sample)} symbols with a bar"
        probes.append(_record(p, resp.ok and len(sample) > 0, status=resp.status,
                              session=last_session["date"], bars=sample))
    else:
        p.note = "no past session in calendar"
        probes.append(_record(p, False))

    # --- free SIP history (older than the minimum age) ------------------------
    p = Probe("sip_history", True, expected="HTTP 200 SIP daily bars ending >= min age ago")
    resp = client.data_get("/v2/stocks/bars", params={
        "symbols": ",".join(PROBE_SYMBOLS), "timeframe": "1Day",
        "start": _iso(now_utc - timedelta(days=30)), "end": _iso(history_cutoff),
        "feed": settings.history_feed, "adjustment": settings.adjustment, "limit": 10000,
    }, purpose="probe:sip_history")
    got = _bars_by_symbol(resp)
    counts = {s: len(b) for s, b in got.items()}
    p.note = f"HTTP {resp.status}, bars per symbol {counts}"
    probes.append(_record(p, resp.ok and bool(counts) and min(counts.values()) >= 10,
                          status=resp.status, bars_per_symbol=counts, error=None if resp.ok else resp.text[:200]))

    # --- recent SIP should be refused on Basic (informational) ---------------
    p = Probe("sip_recent_blocked", False, expected="HTTP 403 on the Basic plan")
    resp = client.data_get("/v2/stocks/bars", params={
        "symbols": "SPY", "timeframe": "1Min",
        "start": _iso(now_utc - timedelta(minutes=10)), "end": _iso(now_utc),
        "feed": "sip", "limit": 10,
    }, purpose="probe:sip_recent")
    if resp.status == 403:
        p.note = "recent SIP refused, as expected on Basic"
    elif resp.ok:
        p.note = "recent SIP allowed (paid plan?). ORB-IEX still never uses it."
    else:
        p.note = f"unexpected HTTP {resp.status}"
    probes.append(_record(p, None, status=resp.status, body=resp.text[:200]))

    # --- largest multi-symbol request (informational) -------------------------
    p = Probe("max_symbols_per_request", False, expected="largest symbols= list that returns HTTP 200")
    results = []
    best = 0
    if last_session and symbols:
        # Daily bars are stamped at 00:00 ET, so the window starts at midnight.
        day_start = datetime.combine(date.fromisoformat(last_session["date"]), datetime.min.time(), tzinfo=NY)
        for n in SYMBOL_COUNT_STEPS:
            if len(symbols) < n:
                break
            batch = symbols[:n]
            try:
                resp = client.data_get("/v2/stocks/bars", params={
                    "symbols": ",".join(batch), "timeframe": "1Day",
                    "start": _iso(day_start), "end": _iso(min(day_start + timedelta(days=1), history_cutoff)),
                    "feed": settings.history_feed, "adjustment": settings.adjustment, "limit": 10000,
                }, purpose=f"probe:symbols_{n}")
            except ConnectionError as exc:
                # informational probe: a slow or timed-out large request is a result, not a failure
                results.append({"requested": n, "status": None, "error": str(exc)[:120]})
                break
            returned = len(_bars_by_symbol(resp))
            paged = bool(resp.ok and isinstance(resp.data, dict) and resp.data.get("next_page_token"))
            results.append({"requested": n, "status": resp.status, "returned": returned, "paged": paged})
            # A 200 with no bars proves nothing; count only requests that returned data.
            if not resp.ok or returned < max(1, n // 2):
                break
            best = n
    p.note = f"largest successful request: {best} symbols"
    # Keep the shipped 200 unless a probe proved a larger value returns data.
    recommended = min(best, 1000) if best >= settings.max_symbols_per_request else settings.max_symbols_per_request
    probes.append(_record(p, None, steps=results, largest_ok=best,
                          recommended_setting=recommended))

    required_failed = [pr.name for pr in probes if pr.required and pr.ok is not True]
    report = {
        "variant": C.VARIANT,
        "generated_at": _iso(now_utc),
        "stage": 1,
        "passed": not required_failed,
        "required_failed": required_failed,
        "call_counts": dict(client.call_counts),
        "data_rate_limit_header": client.last_rate_limit_header.get(C.DATA_HOST),
        "trading_rate_limit_header": client.last_rate_limit_header.get(C.PAPER_TRADING_HOST),
        "probes": [asdict(pr) for pr in probes],
    }
    return report
