import json

from fakes import FakeTransport, basic_plan_routes
from orb_iex import cli, http


def _write_settings(tmp_path):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path / "rt"}"\n'
                   f'[safety]\nkill_switch_file = "{tmp_path / "KILL"}"\n')
    return cfg


def test_check_alpaca_end_to_end_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "PKFAKE12345")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fake-secret-abcdef")
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)
    transport = FakeTransport(basic_plan_routes())
    monkeypatch.setattr(http.requests, "Session", lambda: transport)
    code = cli.main(["--config", str(_write_settings(tmp_path)),
                     "--env-file", str(tmp_path / "none.env"), "check-alpaca"])
    assert code == cli.EXIT_OK
    reports = list((tmp_path / "rt" / "reports").glob("entitlements-*.json"))
    assert len(reports) == 1 and json.loads(reports[0].read_text())["passed"]
    assert {c["method"] for c in transport.calls} == {"GET"}


def test_live_base_url_is_a_safety_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("APCA_API_KEY_ID", "PKFAKE12345")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fake-secret-abcdef")
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    code = cli.main(["--config", str(_write_settings(tmp_path)),
                     "--env-file", str(tmp_path / "none.env"), "check-alpaca"])
    assert code == cli.EXIT_SAFETY
