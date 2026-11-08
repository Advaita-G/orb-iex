"""Strict settings loader.

Rules:
- Every section and key must be known. A typo fails at startup.
- Every value must have the right type. `true` is not an integer.
- Every value must be in range.
- Blank or missing key -> documented default (mirrors Main.cs defaults).
- Secrets are never read from this file (see secrets.py).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from . import constants as C


class ConfigError(ValueError):
    """Settings are invalid. The bot must not start."""


# ---------------------------------------------------------------------------
# Typed readers
# ---------------------------------------------------------------------------

def _int(name: str, raw: Any) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{name}={raw!r} must be an integer")
    return raw


def _dec(name: str, raw: Any) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ConfigError(f"{name}={raw!r} must be a number")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc
    if not value.is_finite():
        raise ConfigError(f"{name}={raw!r} must be finite")
    return value


def _str(name: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{name}={raw!r} must be a non-empty string")
    return raw.strip()


def _fingerprints(name: str, raw: Any) -> tuple[str, ...]:
    """A list of account fingerprints (lowercase hex, as printed by `python -m orb_iex account-fingerprint`)."""
    import re
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ConfigError(f'{name}={raw!r} must be a list of strings, for example ["0123456789abcdef"]')
    out = tuple(x.strip().lower() for x in raw)
    bad = [x for x in out if not re.fullmatch(r"[0-9a-f]{16}", x)]
    if bad:
        raise ConfigError(f"{name} has invalid fingerprint(s) {bad}: each must be 16 hex characters")
    return out


def _at_most_one(name: str, v) -> None:
    if len(v) > 1:
        raise ConfigError(f"{name} names {len(v)} accounts: each config allowlists only its own account "
                          "(one paper account per machine; see docs/SAFETY.md)")


def _bool(name: str, raw: Any) -> bool:
    if not isinstance(raw, bool):
        raise ConfigError(f"{name}={raw!r} must be true or false")
    return raw


def _hhmm(name: str, raw: Any) -> str:
    """'HH:MM' on a 24-hour clock (America/New_York), returned in canonical form."""
    import re
    if not isinstance(raw, str) or not re.fullmatch(r"\d{2}:\d{2}", raw.strip()):
        raise ConfigError(f'{name}={raw!r} must look like "15:30" (HH:MM, 24-hour, New York time)')
    hh, mm = int(raw.strip()[:2]), int(raw.strip()[3:])
    if hh > 23 or mm > 59:
        raise ConfigError(f"{name}={raw!r} is not a valid time")
    return f"{hh:02d}:{mm:02d}"


def _time_between(lo: str, hi: str):
    def check(name, v):
        if not lo <= v <= hi:
            raise ConfigError(f"{name}={v!r} must be between {lo} and {hi}")
    return check


def _range(lo=None, hi=None, lo_open=False):
    def check(name, v):
        if lo is not None and (v <= lo if lo_open else v < lo):
            raise ConfigError(f"{name}={v} must be {'>' if lo_open else '>='} {lo}")
        if hi is not None and v > hi:
            raise ConfigError(f"{name}={v} must be <= {hi}")
    return check


def _one_of(*allowed):
    def check(name, v):
        if v not in allowed:
            raise ConfigError(f"{name}={v!r} must be one of {list(allowed)}")
    return check


def _equals(expected, why):
    def check(name, v):
        if v != expected:
            raise ConfigError(f"{name}={v!r} is not allowed: {why}")
    return check


# section -> key -> (reader, default, validator or None)
Spec = dict[str, dict[str, tuple[Callable, Any, Callable | None]]]

SCHEMA: Spec = {
    "strategy": {
        # Fixed name so logs and reports never pretend this is the QC/SIP run.
        "variant": (_str, C.VARIANT, _equals(C.VARIANT, "this code implements ORB-IEX only")),
        "research_profile": (_str, "baseline_a", _one_of("baseline_a", "candidate_b")),
        # Not used any more: the paper universe is every eligible stock, built daily.
        # Kept so existing settings files stay valid.
        "universe_size": (_int, 200, _range(0, None, lo_open=True)),
        # Paper: maximum leverage 4x. Each position is capped at
        # max_gross_exposure * equity / 20, so 20 full positions stay <= 4x.
        "max_gross_exposure": (_dec, Decimal("4"), _range(Decimal("0"), Decimal("4"), lo_open=True)),
    },
    "costs": {
        # Modeled only (Alpaca paper charges no commission). Used to make paper
        # and shadow P&L comparable to the QC cost model.
        "commission_per_share": (_dec, Decimal("0.0035"), _range(Decimal("0"), Decimal("1"))),
        "slippage_per_share": (_dec, Decimal("0.01"), _range(Decimal("0"), Decimal("1"))),
    },
    "data": {
        # Hybrid free-data design. These three are locked on purpose.
        "opening_feed": (_str, "iex", _equals("iex", "ORB-IEX reads the 09:30-09:35 bar from the free IEX feed only")),
        "history_feed": (_str, "sip", _equals("sip", "daily stats use free SIP history older than 15 minutes")),
        "adjustment": (_str, "raw", _equals("raw", "Main.cs uses DataNormalizationMode.Raw")),
        # SIP requests must end at least this long ago (Basic plan blocks the last 15 min).
        "history_min_age_minutes": (_int, 16, _range(C.SIP_FREE_MIN_AGE_MINUTES + 1, 24 * 60)),
        # Chunk size for multi-symbol requests. Set from the Stage 1 probe result.
        "max_symbols_per_request": (_int, 200, _range(1, 10000)),
        # Basic plan limit is 200/min; stay below it.
        "market_data_calls_per_minute": (_int, 150, _range(1, 190)),
    },
    "trading": {
        # Trading API limit is 200/min per account; stay below it.
        "trading_calls_per_minute": (_int, 150, _range(1, 190)),
        # Explicit switch for `python -m orb_iex paper`. False = the paper
        # command refuses to start. There is no live switch of any kind.
        "enable_paper_orders": (_bool, False, None),
        # REST order reconciliation interval (backs up the trade-update stream).
        "order_poll_seconds": (_int, 10, _range(2, 120)),
        # An entry decision may only use an IEX quote at most this old (decision time minus the quote's own
        # exchange timestamp). A quote that is older, undated or from the future is NOT used: the entry is
        # skipped and the reason ("stale_quote") is journaled. A missing or unusable quote is NOT replaced by
        # Alpaca's own order validation either: the entry is skipped ("skip_quote_unavailable").
        # Default 2 s (was 15 s in the first P0-A commit). On 2026-09-30 at 09:35:03, 11 of the 20 candidates
        # had an IEX quote that fresh; thin names skip. Keep it configurable and read the journaled ages.
        "max_quote_age_seconds": (_int, 2, _range(1, 300)),
        # Spread gate, FAIL CLOSED: if the latest usable IEX quote has (ask - bid) / mid wider than this percent,
        # the entry is NOT submitted, the signal is skipped for the day and "skip_spread_too_wide" is journaled
        # (with bid, ask, spread, quote age and feed). Default 1 (percent), configurable.
        "max_quote_spread_pct": (_dec, Decimal("1"), _range(Decimal("0"), Decimal("100"), lo_open=True)),
    },
    "schedule": {
        # Wait after 09:35:00 before the single IEX opening-bar request.
        "scan_delay_seconds": (_int, 3, _range(0, 30)),
        # Diagnostic-only re-request of the opening bars (never used to trade).
        "diagnostic_rescan_delay_seconds": (_int, 120, _range(30, 600)),
        # End of day: wait this long for cancels to finish before closing positions.
        # EOD starts at close-60 s; keep this below 40 s so the exit is sent
        # before the close-20 s sweep.
        "eod_cancel_timeout_seconds": (_int, 20, _range(5, 35)),
        "protection_timeout_seconds": (_int, 20, _range(1, 120)),
        "exit_timeout_seconds": (_int, 30, _range(1, 120)),
        # A scan that could not run at 09:35:03 (restart) may still run this
        # many seconds later. After that the day is marked missed, never re-run.
        "scan_grace_seconds": (_int, 60, _range(0, 300)),
        # Unfilled ORB entry orders are cancelled at this New York time and never re-entered that day
        # (a 09:35 stop entry must not stay live until the close). Early-close days cap it at close-30 min.
        # The end-of-day window (close-60 s) always cancels any entry that is still working.
        # The default (10:30) is an OPERATIONAL default, not a researched strategy parameter: it only guarantees
        # that no entry order stays live all day. The best cutoff is to be backtested separately.
        "entry_cutoff_time": (_hhmm, C.ENTRY_CUTOFF_DEFAULT, _time_between("09:40", "15:30")),
    },
    "runtime": {
        # What `python -m orb_iex run` does. readonly = check-alpaca.
        # paper additionally needs trading.enable_paper_orders = true.
        "mode": (_str, "readonly", _one_of("readonly", "signal", "shadow", "paper")),
        "data_dir": (_str, "runtime", None),
        "log_level": (_str, "INFO", _one_of("DEBUG", "INFO", "WARNING", "ERROR")),
        "request_timeout_seconds": (_int, 15, _range(1, 120)),
        # Refuse to start (and block new entries while running) when the disk
        # holding data_dir has less free space than this.
        "min_free_disk_mb": (_int, 512, _range(50, 10_000_000)),
        # Daily JSON-lines logs: gzip after one day, delete after this many days.
        "log_retention_days": (_int, 30, _range(1, 3650)),
        # Ctrl+C / SIGTERM: how long the bot may spend cancelling its unfilled entries and confirming them (and
        # flattening a fill that won the cancel race) before it exits. Keep it below systemd TimeoutStopSec (60).
        "shutdown_timeout_seconds": (_int, 20, _range(5, 50)),
    },
    "journal": {
        # Trading journal (append-only SQLite + CSV/Markdown exports). Use an
        # absolute path on a persistent disk for a server, e.g. on the Oracle VM
        # "/home/opc/orb_iex_data/journal".
        "dir": (_str, "runtime/journal", None),
        # Write daily/weekly/monthly CSV + Markdown exports after each close.
        "export": (_bool, True, None),
    },
    "safety": {
        # Paper account numbers are expected to start with this. Fail closed.
        "expected_account_prefix": (_str, "PA", None),
        # Allowlist of Alpaca paper account fingerprints (see safety.account_fingerprint). Paper mode refuses to
        # start unless the account behind the keys is on this list. Empty = paper mode refuses to start.
        # Print the value for your keys with:  python -m orb_iex account-fingerprint
        "allowed_account_fingerprints": (_fingerprints, (), _at_most_one),
        # Which machine may trade this account. Paper mode refuses to start unless the environment variable
        # ORB_IEX_INSTANCE_ID (set per machine in its own .env / service unit, never in a shared file) equals this.
        # This is an explicit declaration, NOT distributed locking: see docs/SAFETY.md "Cross-machine".
        "account_owner_instance_id": (_str, "", None),
        # If this file exists, no new entries are placed.
        "kill_switch_file": (_str, "runtime/KILL", None),
    },
}


@dataclass(frozen=True)
class Settings:
    variant: str
    research_profile: str
    universe_size: int
    max_gross_exposure: Decimal
    commission_per_share: Decimal
    slippage_per_share: Decimal
    opening_feed: str
    history_feed: str
    adjustment: str
    history_min_age_minutes: int
    max_symbols_per_request: int
    market_data_calls_per_minute: int
    trading_calls_per_minute: int
    enable_paper_orders: bool
    order_poll_seconds: int
    max_quote_age_seconds: int
    max_quote_spread_pct: Decimal
    scan_delay_seconds: int
    diagnostic_rescan_delay_seconds: int
    eod_cancel_timeout_seconds: int
    protection_timeout_seconds: int
    exit_timeout_seconds: int
    scan_grace_seconds: int
    entry_cutoff_time: str
    mode: str
    data_dir: str
    log_level: str
    request_timeout_seconds: int
    min_free_disk_mb: int
    log_retention_days: int
    shutdown_timeout_seconds: int
    journal_dir: str
    journal_export: bool
    expected_account_prefix: str
    allowed_account_fingerprints: tuple[str, ...]
    account_owner_instance_id: str
    kill_switch_file: str
    source: str = "defaults"

    def config_line(self) -> str:
        """One-line summary, like the Main.cs CONFIG debug line."""
        return (
            f"CONFIG variant={self.variant} profile={self.research_profile} mode={self.mode} source={self.source} "
            f"universe=all-eligible-daily maxPositions={C.MAX_POSITIONS} "
            f"maxGrossExposure={self.max_gross_exposure} perPositionCap={self.max_gross_exposure / C.MAX_POSITIONS} "
            f"lookbackDays={C.LOOKBACK_DAYS} openingRangeMinutes={C.OPENING_RANGE_MINUTES} "
            f"minAtr={C.MIN_ATR} minAvgVolume={C.MIN_AVERAGE_VOLUME} minRelVol={C.MIN_RELATIVE_VOLUME} "
            f"stopAtrFraction={C.STOP_ATR_FRACTION} riskFraction={C.RISK_FRACTION_OF_ALLOCATION} "
            f"entryCutoff={self.entry_cutoff_time}ET maxQuoteAge={self.max_quote_age_seconds}s maxQuoteSpread={self.max_quote_spread_pct}% "
            f"commissionPerShare={self.commission_per_share} slippagePerShare={self.slippage_per_share} "
            f"openingFeed={self.opening_feed} historyFeed={self.history_feed} adjustment={self.adjustment} "
            f"tradingHost={C.PAPER_TRADING_HOST}"
        )


def _field(section: str, key: str) -> str:
    return f"journal_{key}" if section == "journal" else key


_FIELD_NAMES = {f.name for f in fields(Settings)} - {"source"}
assert _FIELD_NAMES == {_field(s, k) for s, sec in SCHEMA.items() for k in sec}, "schema/dataclass mismatch"


def parse_settings(raw: dict[str, Any], source: str = "inline") -> Settings:
    errors: list[str] = []
    unknown_sections = set(raw) - set(SCHEMA)
    if unknown_sections:
        errors.append(f"unknown section(s): {sorted(unknown_sections)}")

    values: dict[str, Any] = {}
    for section, spec in SCHEMA.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            errors.append(f"[{section}] must be a table")
            continue
        unknown_keys = set(given) - set(spec)
        if unknown_keys:
            errors.append(f"[{section}] unknown key(s): {sorted(unknown_keys)}")
        for key, (reader, default, validator) in spec.items():
            name = f"{section}.{key}"
            try:
                value = reader(name, given[key]) if key in given else default
                if validator:
                    validator(name, value)
                values[_field(section, key)] = value
            except ConfigError as exc:
                errors.append(str(exc))

    if values.get("research_profile") == "candidate_b" and values.get("enable_paper_orders"):
        errors.append("candidate_b is unqualified; paper orders must remain disabled")
    if errors:
        raise ConfigError("invalid settings:\n  - " + "\n  - ".join(errors))
    return Settings(**values, source=source)


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"settings file not found: {path}")
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return parse_settings(raw, source=str(path))
