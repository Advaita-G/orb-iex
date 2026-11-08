"""Paper-only guards around order writes, key handling and the CLI."""

import json
import re
from decimal import Decimal as D
from pathlib import Path

import pytest

from fakes import FakeResponse, FakeTradingTransport, basic_plan_routes
from orb_iex import cli, constants as C, http
from orb_iex.broker import AlpacaPaperBroker, OrderRejected, OrderUncertain, positions_from_alpaca
from orb_iex.config import parse_settings
from orb_iex.http import ReadOnlyClient
from orb_iex.logging_setup import register_secrets, setup_logging
from orb_iex.orders import OrderRequest
from orb_iex.safety import SafetyError
from orb_iex.secrets import Credentials
from orb_iex.stream import TradeUpdateStream, check_stream_url

SRC = Path(__file__).resolve().parents[2] / "src" / "orb_iex"
CREDS = Credentials("PKFAKEKEYID9876", "fake-secret-value-abc")
ORDER = {"id": "o-1", "client_order_id": "orbiex-20260924-AAA-entry", "symbol": "AAA", "side": "buy",
         "type": "stop", "qty": "10", "status": "new", "filled_qty": "0", "stop_price": "50"}


def broker(writes=None, routes=None, tmp_path=None):
    t = FakeTradingTransport(routes or basic_plan_routes(), writes or {})
    c = ReadOnlyClient(CREDS, 150, 150, transport=t, clock=lambda: 0.0, sleep=lambda s: None)
    kill = str((tmp_path or Path("/nonexistent")) / "KILL")
    return AlpacaPaperBroker(c, kill, sleep=lambda s: None), t


def req(role="entry"):
    return OrderRequest("AAA", "buy", D(10), "stop", f"orbiex-20260924-AAA-{role}", D(50))


def test_unarmed_broker_refuses_writes_before_network():
    b, t = broker({("POST", "/v2/orders"): FakeResponse(200, ORDER)})
    with pytest.raises(SafetyError):
        b.submit_order(req())
    assert [c for c in t.calls if c["method"] != "GET"] == []


@pytest.mark.parametrize("mode, flag, verified", [("shadow", True, True), ("paper", False, True),
                                                   ("paper", True, False), ("live", True, True)])
def test_arming_needs_paper_mode_flag_and_verified_account(mode, flag, verified):
    b, _ = broker()
    with pytest.raises(SafetyError):
        b.arm(mode=mode, enable_paper_orders=flag, account_verified=verified)
    assert not b.armed


def test_armed_broker_writes_only_to_paper_host():
    b, t = broker({("POST", "/v2/orders"): FakeResponse(200, ORDER),
                   ("DELETE", "*"): FakeResponse(204, None),
                   ("PATCH", "*"): FakeResponse(200, {**ORDER, "id": "o-2", "replaces": "o-1"})})
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    assert b.submit_order(req()).order_id == "o-1"
    b.cancel_order("o-1")
    assert b.replace_order("o-1", D(5), "orbiex-20260924-AAA-stop2").replaces == "o-1"
    writes = [c for c in t.calls if c["method"] != "GET"]
    assert {c["method"] for c in writes} == {"POST", "DELETE", "PATCH"}
    assert all(c["url"].startswith("https://paper-api.alpaca.markets/v2/orders") for c in writes)
    assert writes[0]["json"]["time_in_force"] == "day"


def test_live_host_refused_even_when_armed(monkeypatch):
    b, t = broker({("POST", "/v2/orders"): FakeResponse(200, ORDER)})
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    monkeypatch.setattr(C, "PAPER_TRADING_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(SafetyError):
        b.submit_order(req())
    assert [c for c in t.calls if c["method"] != "GET"] == []


def test_kill_switch_refuses_entry_but_not_exit(tmp_path):
    b, t = broker({("POST", "/v2/orders"): lambda body: FakeResponse(200, {**ORDER,
                   "client_order_id": body["client_order_id"]})}, tmp_path=tmp_path)
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    (tmp_path / "KILL").touch()
    with pytest.raises(SafetyError):
        b.submit_order(req("entry"))
    exit_req = OrderRequest("AAA", "sell", D(10), "market", "orbiex-20260924-AAA-eod1")
    assert b.submit_order(exit_req).client_order_id == "orbiex-20260924-AAA-eod1"


def test_foreign_client_order_id_refused():
    b, _ = broker()
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    with pytest.raises(SafetyError):
        b.submit_order(OrderRequest("AAA", "buy", D(1), "market", "manual-123"))


def test_duplicate_client_order_id_is_adopted_not_resubmitted():
    routes = basic_plan_routes()
    routes["/v2/orders:by_client_order_id"] = FakeResponse(200, ORDER)
    b, t = broker({("POST", "/v2/orders"): FakeResponse(422, {"message": "client_order_id must be unique"})},
                  routes=routes)
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    assert b.submit_order(req()).order_id == "o-1"
    assert sum(c["method"] == "POST" for c in t.calls) == 1


def test_rejected_order_raises():
    b, _ = broker({("POST", "/v2/orders"): FakeResponse(403, {"message": "insufficient buying power"})})
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    with pytest.raises(OrderRejected) as exc:
        b.submit_order(req())
    assert "buying power" in str(exc.value)


@pytest.mark.parametrize('status', [500, 502, 503, 504])
def test_server_error_keeps_submit_ambiguous_and_retains_exact_body(status):
    body = {'code': 50410000, 'message': 'upstream timeout', 'detail': 'accepted status unknown'}
    b, t = broker({('POST','/v2/orders'): FakeResponse(status, body)})
    b.arm(mode='paper', enable_paper_orders=True, account_verified=True)
    with pytest.raises(OrderUncertain) as exc:
        b.submit_order(req())
    assert exc.value.body == body and exc.value.status == status
    assert sum(c['method'] == 'POST' for c in t.calls) == 1


def test_failed_order_lookup_is_not_misreported_as_absent():
    routes = basic_plan_routes()
    routes['/v2/orders:by_client_order_id'] = FakeResponse(403, {'message':'forbidden'})
    b, _ = broker(routes=routes)
    with pytest.raises(ConnectionError): b.get_order_by_client_id(req().client_order_id)


def test_redirect_on_write_is_a_safety_stop():
    b, _ = broker({("POST", "/v2/orders"): FakeResponse(307, None,
                   headers={"Location": "https://api.alpaca.markets/v2/orders"})})
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    with pytest.raises(SafetyError):
        b.submit_order(req())


def test_short_positions_are_negative():
    assert positions_from_alpaca([{"symbol": "A", "qty": "5", "side": "long"},
                                  {"symbol": "B", "qty": "-7", "side": "short"},
                                  {"symbol": "C", "qty": "0", "side": "long"}]) == {"A": D(5), "B": D(-7)}


def test_stream_url_must_be_paper():
    check_stream_url(C.PAPER_STREAM_URL)
    for bad in ("wss://api.alpaca.markets/stream", "ws://paper-api.alpaca.markets/stream",
                "wss://stream.data.alpaca.markets/v2/iex"):
        with pytest.raises(SafetyError):
            check_stream_url(bad)
    with pytest.raises(SafetyError):
        TradeUpdateStream(CREDS, None, url="wss://api.alpaca.markets/stream")


# --- source-level guarantees ---------------------------------------------------

def test_only_broker_module_sends_order_writes():
    offenders = []
    for f in SRC.glob("*.py"):
        text = f.read_text()
        if re.search(r'["\'](POST|PATCH|DELETE)["\']', text) and f.name != "broker.py":
            offenders.append(f.name)
    assert offenders == []


def test_live_host_appears_only_as_the_refusal_constant():
    for f in SRC.glob("*.py"):
        for line in f.read_text().splitlines():
            if "api.alpaca.markets" in line and "paper-api" not in line and "data.alpaca" not in line:
                assert f.name == "constants.py" and line.startswith("LIVE_TRADING_HOST"), (f.name, line)


def test_no_setting_can_change_the_trading_host_or_enable_live():
    for key in ("base_url", "live", "trading_host", "enable_live_orders"):
        for section in ("trading", "runtime", "safety"):
            with pytest.raises(Exception):
                parse_settings({section: {key: True}})
    with pytest.raises(Exception):
        parse_settings({"runtime": {"mode": "live"}})
    assert parse_settings({}).enable_paper_orders is False


def test_api_keys_never_logged_during_orders(tmp_path):
    setup_logging(tmp_path, "DEBUG")
    register_secrets(*CREDS.secrets())
    b, _ = broker({("POST", "/v2/orders"): FakeResponse(200, ORDER)})
    b.arm(mode="paper", enable_paper_orders=True, account_verified=True)
    b.submit_order(req())
    import logging
    logging.getLogger("orb_iex").info("oops %s %s", CREDS.key_id, CREDS.secret_key)
    text = "".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    assert CREDS.secret_key not in text and CREDS.key_id not in text


# --- CLI ------------------------------------------------------------------------

def _cfg(tmp_path, extra=""):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path / "rt"}"\n[safety]\nkill_switch_file = "{tmp_path / "KILL"}"\n'
                   + extra)
    return cfg


def _env(monkeypatch, transport):
    monkeypatch.setenv("APCA_API_KEY_ID", "PKFAKE12345")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fake-secret-abcdef")
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)
    monkeypatch.setattr(http.requests, "Session", lambda: transport)


def test_paper_command_refuses_without_config_flag(tmp_path, monkeypatch):
    t = FakeTradingTransport(basic_plan_routes())
    _env(monkeypatch, t)
    code = cli.main(["--config", str(_cfg(tmp_path)), "--env-file", str(tmp_path / "x"), "paper"])
    assert code == cli.EXIT_SAFETY
    assert [c for c in t.calls if c["method"] != "GET"] == []


def test_paper_command_refuses_live_style_account(tmp_path, monkeypatch):
    t = FakeTradingTransport(basic_plan_routes(account_number="123456789"))
    _env(monkeypatch, t)
    code = cli.main(["--config", str(_cfg(tmp_path, "[trading]\nenable_paper_orders = true\n")),
                     "--env-file", str(tmp_path / "x"), "paper"])
    assert code == cli.EXIT_SAFETY


def test_paper_command_refuses_live_base_url(tmp_path, monkeypatch):
    t = FakeTradingTransport(basic_plan_routes())
    _env(monkeypatch, t)
    monkeypatch.setenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
    code = cli.main(["--config", str(_cfg(tmp_path, "[trading]\nenable_paper_orders = true\n")),
                     "--env-file", str(tmp_path / "x"), "paper"])
    assert code == cli.EXIT_SAFETY
    assert t.calls == []
