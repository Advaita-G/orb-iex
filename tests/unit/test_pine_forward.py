"""Forward configurations may change dates and chart guards, never frozen rules."""
import hashlib
from pathlib import Path

import pytest

PINE = Path(__file__).resolve().parents[2] / 'research/pine'
HASHES = {
    'baseline_a': 'ee9707e02dda2c23939b8c96bba9b63da8d3f1e35639cf7a19b6e4eb7e1f9ace',
    'candidate_b': 'f0d50daadddcac9ca553ce90e705c66c1b46039b9eca42e87ab38d0ea7baf9b2',
}


@pytest.mark.parametrize('name', HASHES)
def test_forward_preserves_frozen_rules_and_confirmed_observations(name):
    frozen = (PINE / f'{name}.pine').read_bytes()
    assert hashlib.sha256(frozen).hexdigest() == HASHES[name]
    historical = frozen.decode()
    forward = (PINE / f'{name}_forward_v1.pine').read_text()
    assert 'input.bool(false, "Use forward end date")' in forward
    assert 'timestamp("29 Oct 2026 00:00 +0000")' in forward
    assert 'if useEndDate and endDate <= startDate' in forward
    assert 'not chart.is_standard' in forward
    assert 'syminfo.timezone != "America/New_York"' in forward
    # Remove only the versioned date configuration and explicit chart validation.
    start = forward.index('int startDate =')
    end = forward.index('if not timeframe.isminutes')
    old_start = historical.index('int startDate =')
    old_end = historical.index('if not timeframe.isminutes')
    normalized = forward[:start] + historical[old_start:old_end] + forward[end:]
    normalized = normalized.replace(f'ORB-IEX {name} forward v1', f'ORB-IEX {name} chart projection')
    normalized = normalized.replace(
        f'// Forward v1: date/session configuration only; frozen {name}.pine is unchanged.',
        '// Single-chart RESEARCH projection of frozen bc9ecde. No universe rank or IEX quote replay.')
    normalized = normalized.replace('(not useEndDate or time < endDate)', 'time < endDate')
    assert normalized == historical
    assert 'barstate.isconfirmed and session.ismarket' in forward
    assert '[ta.sma(ta.tr(true), 14)[1], ta.sma(volume, 14)[1]]' in forward
    assert 'bool quoteEvidenceAvailable = not candidateB' in forward
