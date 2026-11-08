"""Paper-only safety guard and kill switch.

Layers (all fail closed):
1. The trading base URL is a constant for the paper host. Not configurable.
2. APCA_API_BASE_URL, if set, must be the paper host (secrets.py).
3. Every HTTP request re-checks its host against an allowlist (http.py).
   The live trading host is not on the list.
4. At startup, /v2/account must look like a paper account: account number
   prefix and ACTIVE status. Live keys also fail to authenticate on the
   paper host, so a live key cannot pass step 4.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import constants as C


class SafetyError(RuntimeError):
    """A paper-only rule was broken. Stop immediately."""


class ClockSkewError(SafetyError):
    """Local clock disagrees with the broker clock. Transient: NTP can repair it."""


class BrokerUnavailableError(SafetyError):
    """Broker answered 429/5xx or an unreadable body. Transient: says nothing about identity or exposure."""


def assert_allowed_url(url: str) -> str:
    """Return the host if the URL is an allowed HTTPS Alpaca paper/data URL."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https":
        raise SafetyError(f"refusing non-HTTPS URL: {url}")
    if parts.username or parts.password:
        raise SafetyError(f"refusing URL with embedded credentials: {url}")
    if parts.port not in (None, 443):
        raise SafetyError(f"refusing unexpected port in URL: {url}")
    if host == C.LIVE_TRADING_HOST:
        raise SafetyError(f"refusing LIVE trading host {host}. ORB-IEX is paper only.")
    if host not in C.ALLOWED_HOSTS:
        raise SafetyError(f"refusing host {host!r}; allowed: {sorted(C.ALLOWED_HOSTS)}")
    return host


def assert_paper_trading_url(url: str) -> None:
    host = assert_allowed_url(url)
    if host != C.PAPER_TRADING_HOST:
        raise SafetyError(f"trading call must go to {C.PAPER_TRADING_HOST}, got {host}")


def check_paper_account(account: dict[str, Any], expected_prefix: str) -> dict[str, Any]:
    """Validate /v2/account. Returns a safe summary (no balances beyond equity)."""
    number = str(account.get("account_number", ""))
    status = str(account.get("status", ""))
    if not number.startswith(expected_prefix):
        raise SafetyError(
            f"account number does not start with {expected_prefix!r} "
            f"(got prefix {number[:2]!r}). Refusing: this may not be a paper account."
        )
    if status != "ACTIVE":
        raise SafetyError(f"account status is {status!r}, expected 'ACTIVE'")
    return {
        "account_number_suffix": number[-4:],
        "status": status,
        "currency": account.get("currency"),
        "equity": account.get("equity"),
        "multiplier": account.get("multiplier"),
        "shorting_enabled": account.get("shorting_enabled"),
        "trading_blocked": account.get("trading_blocked"),
        "account_blocked": account.get("account_blocked"),
        "trade_suspended_by_user": account.get("trade_suspended_by_user"),
        "pattern_day_trader": account.get("pattern_day_trader"),
        "daytrade_count": account.get("daytrade_count"),
        "daytrading_buying_power": account.get("daytrading_buying_power"),
    }


INSTANCE_ID_VAR = "ORB_IEX_INSTANCE_ID"


def account_fingerprint(account: dict[str, Any]) -> str:
    """Stable, non-secret identity of an Alpaca account: 16 hex chars of sha256(account id | account number).
    Two different accounts never share it; the keys are not part of it (keys can be rotated)."""
    ident = f"{account.get('id', '')}|{account.get('account_number', '')}"
    return hashlib.sha256(ident.encode()).hexdigest()[:16]


def check_account_allowlist(account: dict[str, Any], allowed: tuple[str, ...]) -> str:
    """Fail closed unless this account's fingerprint is on the configured allowlist. Returns the fingerprint."""
    fp = account_fingerprint(account)
    if not allowed:
        raise SafetyError(
            "safety.allowed_account_fingerprints is empty: paper mode will not trade an account nobody named. "
            f"This account's fingerprint is {fp}. If it is the right account, add it to the settings file.")
    if fp not in allowed:
        raise SafetyError(
            f"account fingerprint {fp} (...{str(account.get('account_number', ''))[-4:]}) is not on the "
            f"allowlist {list(allowed)}. Wrong keys or wrong account; refusing to trade.")
    return fp


def check_instance_owner(declared_owner: str, instance_id: str | None) -> str:
    """The settings name the machine that may trade the account; this machine proves who it is through
    ORB_IEX_INSTANCE_ID. Mismatch or either side missing = refuse. This is a declaration that a person made
    and can audit; it is NOT a lock and cannot see another machine that declares the same id."""
    declared, mine = (declared_owner or "").strip(), (instance_id or "").strip()
    if not declared:
        raise SafetyError("safety.account_owner_instance_id is not set: no machine is declared as the owner of "
                          "this account; refusing to trade")
    if not mine:
        raise SafetyError(f"{INSTANCE_ID_VAR} is not set on this machine; it cannot show it is the declared owner "
                          f"({declared!r}) of the account; refusing to trade")
    if mine != declared:
        raise SafetyError(f"this machine is {mine!r} but the account is declared to belong to {declared!r}; "
                          "refusing to trade (two machines must never trade one account)")
    return mine


def kill_switch_active(path: str | Path) -> bool:
    """True if the kill-switch file exists. It blocks new entries (later stages)."""
    return Path(path).exists()
