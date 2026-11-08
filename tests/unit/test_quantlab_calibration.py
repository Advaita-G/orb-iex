import json

import pandas as pd
import pytest

from quantlab import calibrate as cal, report


def test_calibration_never_fetches_validation_or_final_test_ticks(tmp_path, monkeypatch):
    dates = pd.to_datetime(['2018-01-02', '2019-01-02', '2020-01-02', '2024-01-02'])
    amb = pd.DataFrame(dict(date=dates, symbol='X', direction=1, level=50., stop_dist=.2,
                            bar_o=50., bar_h=51., bar_l=49., bar_c=50.5, entry_minute=575))
    seen = []
    monkeypatch.setattr(cal.ad, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(cal.ad, 'make_client', lambda *a: None)
    def ticks(client, symbol, day, minute):
        seen.append(day)
        return cal.np.array([50., 49.])
    monkeypatch.setattr(cal, 'fetch_ticks', ticks)
    result = cal.calibrate(amb, n=4)
    assert seen == list(dates[:2])
    assert result['max_sample_date'] == '2019-01-02'
    assert result['cv_method'] == 'expanding_year'


@pytest.mark.parametrize('metadata', [{}, {'training_end': '2023-12-31', 'max_sample_date': '2024-01-02'},
                                      {'training_end': '2019-12-31', 'max_sample_date': '2020-01-02'}])
def test_report_rejects_unproven_or_leaking_calibration(tmp_path, monkeypatch, metadata):
    monkeypatch.setattr(report, 'DATA', tmp_path)
    (tmp_path / 'calibration_model.json').write_text(json.dumps({'weights': [0.] * 5, **metadata}))
    with pytest.raises(ValueError, match='calibration'):
        report._model()



def test_cv_constant_uses_only_past_years(tmp_path, monkeypatch):
    dates = pd.to_datetime(['2016-01-04', '2017-01-03', '2018-01-02', '2019-01-02'])
    amb = pd.DataFrame(dict(date=dates, symbol='X', direction=1, level=50., stop_dist=.2,
                            bar_o=50., bar_h=51., bar_l=49., bar_c=50.5, entry_minute=575))
    monkeypatch.setattr(cal.ad, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(cal.ad, 'make_client', lambda *a: None)
    monkeypatch.setattr(cal, 'fetch_ticks', lambda client, symbol, day, minute:
                        cal.np.array([50., 49. if day.year % 2 == 0 else 51.]))
    result = cal.calibrate(amb, n=4)
    expected = -cal.np.log([1e-6, .5, 1 / 3]).mean()
    assert result['cv_n'] == 3
    assert result['logloss_const'] == pytest.approx(expected)
    monkeypatch.setattr(report, 'DATA', tmp_path)
    assert len(report._model()) == 5
