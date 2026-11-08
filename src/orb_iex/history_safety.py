"""Causal feature eligibility. Quarantine action windows; never guess adjustments."""
from datetime import date, datetime

from . import constants as C
from .calendar import NY


def session_history_reason(daily, openings, sessions, today_bar=None, today_session=None):
    required = sessions[-(C.LOOKBACK_DAYS + 1):]
    if len(required) != C.LOOKBACK_DAYS + 1:
        return 'history:calendar_too_short'
    for s in required:
        b = daily.get(s.day)
        if b is None:
            return f'history:missing_daily_session:{s.day}'
        if b.t.astimezone(NY).date() != s.day:
            return f'history:daily_timestamp_mismatch:{s.day}'
    if openings is not None:
        for s in required[-C.LOOKBACK_DAYS:]:
            b = openings.get(s.day)
            if b is None:
                return f'history:missing_opening_session:{s.day}'
            if b.t != s.open:
                return f'history:opening_window_mismatch:{s.day}'
    if today_bar is not None and today_session is not None and today_bar.t != today_session.open:
        return 'history:today_opening_window_mismatch'
    return None


def action_exclusions(snapshot, symbols, start, today, decision_at):
    """Only observations available by this decision may authorize raw features.

    All event kinds quarantine the symbol. This deliberately also excludes an
    already-adjusted split window: production uses raw bars, not mixed bases.
    """
    syms = set(symbols)
    def all_bad(reason): return {s: reason for s in syms}
    if not isinstance(snapshot, dict) or snapshot.get('error'):
        return all_bad('history:action_metadata_unavailable')
    try:
        observed = datetime.fromisoformat(snapshot['observed_at'])
        if (observed.tzinfo is None or observed > decision_at
                or observed.astimezone(NY).date() != today
                or not syms <= set(snapshot['symbols'])
                or snapshot.get('complete') is not True):
            return all_bad('history:action_metadata_not_point_in_time')
        actions = snapshot['actions']
        if not isinstance(actions, list): raise ValueError('actions')
    except (KeyError, TypeError, ValueError):
        return all_bad('history:action_metadata_ambiguous')
    excluded = {}
    for action in actions:
        if not isinstance(action, dict):
            return all_bad('history:action_metadata_ambiguous')
        affected = {action[k] for k in ('symbol', 'source_symbol', 'new_symbol', 'old_symbol', 'initiating_symbol', 'target_symbol',
                     'acquirer_symbol', 'acquiree_symbol')
                    if isinstance(action.get(k), str) and action[k]}
        if not affected:
            return all_bad('history:action_symbol_ambiguous')
        targets = affected & syms
        if not targets:
            continue
        try:
            effective = date.fromisoformat(action.get('ex_date') or action['effective_date'])
        except (KeyError, TypeError, ValueError):
            excluded.update({s: 'history:action_effective_date_ambiguous' for s in targets})
            continue
        if start <= effective <= today:
            kind = action.get('kind', 'unknown')
            excluded.update({s: f'history:corporate_action:{kind}:{effective}' for s in targets})
    return excluded


def unexplained_discontinuity(daily, today_bar=None):
    """A factor-of-two jump is an ambiguous data basis, NOT an inferred action.

    Ordinary gaps retain their full true range. Extreme economic gaps can also
    be excluded by this conservative ceiling; only metadata labels an action.
    """
    bars = [daily[d] for d in sorted(daily)]
    if today_bar is not None: bars.append(today_bar)
    for previous, current in zip(bars, bars[1:]):
        if previous.c <= 0 or current.o <= 0:
            return 'history:invalid_price_basis'
        ratio = current.o / previous.c
        if ratio <= .5 or ratio >= 2:
            return 'history:unexplained_discontinuity'
    return None
