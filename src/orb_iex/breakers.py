"""Breaker lifecycle policy. A breaker stays latched while its cause may be present.

Only breakers whose cause is objectively re-checkable by the bot itself may resolve
automatically, and only on fresh evidence (never because time passed). Everything else
needs a human. Exposure breakers (positions, orders, intents, journal) are never listed
here: they clear through the engine's own reconciliation, or by an operator.

  TRANSIENT  infrastructure condition; resolves after `checks` consecutive fresh healthy
             broker checks spanning at least `span_s` seconds, plus a valid broker snapshot,
             zero unresolved intents, a healthy journal and no non-transient breaker.
  SESSION    a failed per-session input (scan data); resolves once a later session starts
             and the account is flat with no live orders/intents. The new scan re-tests it.
  MANUAL     anything not listed (identity, foreign inventory, KILL, overnight/unprotected
             position, sign flip, duplicate order, journal failure, ...).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

TRANSIENT_CHECKS = 3
TRANSIENT_SPAN_S = 10


@dataclass(frozen=True)
class Policy:
    kind: str  # 'transient' | 'session'
    checks: int = TRANSIENT_CHECKS
    span_s: int = TRANSIENT_SPAN_S


POLICIES = {
    'auto_clock_skew': Policy('transient'),
    # Derived latch: recovery still requires fresh broker evidence and no unresolved safety state.
    'auto_safety_failure': Policy('transient'),
    'auto_control_failure': Policy('transient'),
    'auto_broker_failure': Policy('transient'),
    'auto_calendar_failure': Policy('transient'),
    'auto_market_closed': Policy('transient'),
    'auto_data_failure': Policy('session'),
}


def policy(kind: str) -> Policy | None:
    return POLICIES.get(kind)


def is_manual(kind: str) -> bool:
    return kind not in POLICIES


class Evidence:
    """Consecutive healthy samples. Any unhealthy sample resets it."""

    def __init__(self, checks: int = TRANSIENT_CHECKS, span_s: int = TRANSIENT_SPAN_S, min_gap_s: float = 4.0):
        self.checks, self.span, self.gap = checks, timedelta(seconds=span_s), timedelta(seconds=min_gap_s)
        self.samples: list[datetime] = []

    def observe(self, now: datetime, healthy: bool) -> None:
        if not healthy:
            self.samples.clear()
        elif not self.samples or now - self.samples[-1] >= self.gap:
            self.samples.append(now)

    def reset(self) -> None:
        self.samples.clear()

    def satisfied(self, policy_: Policy) -> bool:
        return (len(self.samples) >= policy_.checks
                and self.samples[-1] - self.samples[0] >= timedelta(seconds=policy_.span_s))

    def summary(self) -> dict:
        return {'healthy_checks': len(self.samples),
                'span_s': round((self.samples[-1] - self.samples[0]).total_seconds(), 1) if self.samples else 0}
