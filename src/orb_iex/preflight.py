"""Startup safety checks for signal / shadow / paper. Fail closed.

Any doubt about the account or environment raises SafetyError and the bot
does not start. For paper mode the broker is armed only at the very end.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import constants as C
from .logging_setup import LOGGER_NAME, log_event
from .safety import (INSTANCE_ID_VAR, BrokerUnavailableError, ClockSkewError, SafetyError, account_fingerprint, assert_paper_trading_url,
                     check_account_allowlist, check_instance_owner, check_paper_account)
from .secrets import check_base_url_env

log = logging.getLogger(LOGGER_NAME)
MAX_SKEW_SECONDS = 2.0


def verify_environment(client, settings, mode: str, now=lambda: datetime.now(timezone.utc),
                       environ: dict | None = None) -> dict:
    import os
    environ = os.environ if environ is None else environ
    check_base_url_env()
    assert_paper_trading_url(C.PAPER_TRADING_BASE_URL + "/v2/account")
    resp = client.trading_get("/v2/account", purpose="preflight:account")
    if resp.status in (401, 403):
        raise SafetyError(f"paper host rejected the keys (HTTP {resp.status}); use PAPER keys")
    if not resp.ok or not isinstance(resp.data, dict):
        exc = BrokerUnavailableError if (resp.status >= 500 or resp.status == 429 or resp.ok) else SafetyError
        raise exc(f"cannot verify the account (HTTP {resp.status}); refusing to start")
    summary = check_paper_account(resp.data, settings.expected_account_prefix)
    summary["fingerprint"] = account_fingerprint(resp.data)
    if mode == "paper":
        # Before any order activity: the right account, and this machine is its declared owner.
        check_account_allowlist(resp.data, settings.allowed_account_fingerprints)
        summary["instance_id"] = check_instance_owner(settings.account_owner_instance_id, environ.get(INSTANCE_ID_VAR))
    if mode == "paper" and (summary.get("trading_blocked") or summary.get("account_blocked")
                            or summary.get("trade_suspended_by_user")):
        raise SafetyError(f"paper account cannot trade: {summary}")
    before = now()
    clk = client.trading_get("/v2/clock", purpose="preflight:clock")
    after = now()
    if not clk.ok or not isinstance(clk.data, dict) or not clk.data.get("timestamp"):
        raise BrokerUnavailableError("cannot read /v2/clock; refusing to start")
    server = datetime.fromisoformat(clk.data["timestamp"])
    skew = ((before + (after - before) / 2) - server).total_seconds()
    if abs(skew) > MAX_SKEW_SECONDS:
        msg = f"clock skew {skew:+.2f}s exceeds {MAX_SKEW_SECONDS}s; sync the system clock (NTP)"
        if mode == "paper":
            raise ClockSkewError(msg)
        log_event(log, logging.WARNING, "preflight", msg)
    log_event(log, logging.INFO, "preflight",
              f"preflight OK mode={mode} account=...{summary['account_number_suffix']} fingerprint={summary['fingerprint']} "
              f"instance={summary.get('instance_id', '-')} status={summary['status']} "
              f"equity={summary['equity']} pdt={summary.get('pattern_day_trader')} "
              f"daytrades={summary.get('daytrade_count')} skew={skew:+.3f}s host={C.PAPER_TRADING_HOST}",
              **summary, skew_seconds=round(skew, 3), mode=mode)
    return summary
