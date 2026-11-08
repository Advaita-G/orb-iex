"""Arithmetic checks for the frozen offline audit, without private evidence."""
import runpy
from pathlib import Path

AUDIT = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'tools/audit_rc_20261028.py'))


def row(pnl, exit_time, day='2026-10-01'):
    return dict(gross_pnl=str(pnl), modeled_cost='1', initial_risk='10',
                exit_time=exit_time, day=day, symbol='X')


def test_drawdown_includes_initial_loss_and_orders_by_exit():
    rows = [row(4, '03'), row(-5, '01'), row(10, '02')]
    result = AUDIT['metrics'](rows, 1)
    assert result['net_pnl'] == '6'
    assert result['closed_cash_drawdown'] == '6'
    assert result['profit_factor'] == '2'


def test_bootstrap_samples_whole_days_and_is_repeatable():
    rows = [row(-3, '01'), row(5, '02')]
    assert AUDIT['bootstrap'](rows, 1) == [0.0, 0.0]
    rows.append(row(10, '03', '2026-10-02'))
    assert AUDIT['bootstrap'](rows, 1) == AUDIT['bootstrap'](rows, 1)


def test_changed_snapshot_fails_before_any_reconstruction(tmp_path, monkeypatch):
    import json
    import pytest

    (tmp_path / 'manifest.json').write_text(json.dumps({'broker_orders_sha256': '0' * 64}))
    (tmp_path / 'broker-orders.json').write_text('[]')
    monkeypatch.setitem(AUDIT['main'].__globals__, 'EVIDENCE', tmp_path)
    with pytest.raises(ValueError, match='Frozen evidence changed: broker-orders.json'):
        AUDIT['main']()
    assert not (tmp_path / 'reaudit.json').exists()
