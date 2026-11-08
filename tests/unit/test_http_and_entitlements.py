import logging

import pytest

from fakes import FakeResponse, FakeTransport, basic_plan_routes
from orb_iex import constants as C
from orb_iex.config import parse_settings
from orb_iex.entitlements import run_entitlement_checks
from orb_iex.http import RateLimiter, ReadOnlyClient
from orb_iex.logging_setup import LOGGER_NAME, register_secrets, setup_logging
from orb_iex.safety import SafetyError
from orb_iex.secrets import Credentials

CREDS = Credentials("PKFAKEKEYID1234", "fake-secret-value-xyz")


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make_client(routes, per_minute=150):
    clock = FakeClock()
    transport = FakeTransport(routes)
    client = ReadOnlyClient(CREDS, per_minute, per_minute, transport=transport,
                            clock=clock.now, sleep=clock.sleep)
    return client, transport, clock


# --- read-only client -------------------------------------------------------

def test_client_has_no_write_methods():
    for name in ("post", "put", "patch", "delete", "submit_order", "cancel_order",
                 "replace_order", "close_position", "close_all_positions"):
        assert not hasattr(ReadOnlyClient, name)


def test_only_get_is_sent():
    client, transport, _ = make_client(basic_plan_routes())
    client.trading_get("/v2/account")
    client.data_get("/v2/stocks/bars/latest", {"symbols": "SPY", "feed": "iex"})
    assert {c["method"] for c in transport.calls} == {"GET"}
    assert all(c["url"].startswith(("https://paper-api.alpaca.markets/",
                                    "https://data.alpaca.markets/")) for c in transport.calls)


def test_host_guard_runs_before_network(monkeypatch):
    client, transport, _ = make_client({})
    monkeypatch.setattr(C, "PAPER_TRADING_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(SafetyError):
        client.trading_get("/v2/account")
    assert transport.calls == []


def test_retry_on_429_then_success():
    responses = iter([FakeResponse(429, {}, headers={"Retry-After": "2"}),
                      FakeResponse(200, {"ok": True})])
    client, transport, clock = make_client({"/v2/clock": lambda p: next(responses)})
    resp = client.trading_get("/v2/clock")
    assert resp.ok and len(transport.calls) == 2 and clock.slept == [2.0]


def test_403_is_returned_not_retried():
    client, transport, _ = make_client({"/v2/x": FakeResponse(403, {"message": "no"})})
    assert client.data_get("/v2/x").status == 403
    assert len(transport.calls) == 1


def test_redirect_is_refused():
    client, _, _ = make_client({"/v2/account": FakeResponse(302, None,
                                headers={"Location": "https://api.alpaca.markets/v2/account"})})
    with pytest.raises(SafetyError):
        client.trading_get("/v2/account")


def test_rate_limiter_waits_after_limit():
    clock = FakeClock()
    rl = RateLimiter(3, clock.now, clock.sleep)
    for _ in range(3):
        assert rl.acquire() == 0
    assert rl.acquire() > 59


def test_secrets_never_reach_logs(tmp_path, caplog):
    logger = setup_logging(tmp_path, "DEBUG")
    register_secrets(*CREDS.secrets())
    client, _, _ = make_client(basic_plan_routes())
    client.trading_get("/v2/account", purpose="test")
    logger.info("leak attempt %s", CREDS.secret_key)
    text = "".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    assert CREDS.secret_key not in text and CREDS.key_id not in text
    assert "***" in text


# --- entitlement checks -------------------------------------------------------

def test_basic_plan_passes_stage1(tmp_path):
    setup_logging(tmp_path, "INFO")
    client, transport, _ = make_client(basic_plan_routes())
    report = run_entitlement_checks(client, parse_settings({}))
    assert report["passed"], report["required_failed"]
    probes = {p["name"]: p for p in report["probes"]}
    assert probes["sip_recent_blocked"]["observed"]["status"] == 403
    assert probes["max_symbols_per_request"]["observed"]["largest_ok"] == 2000
    assert {c["method"] for c in transport.calls} == {"GET"}
    # budget: a Stage 1 run is a handful of calls, far below free limits
    assert sum(report["call_counts"].values()) <= 15
    # the opening-bar probe uses the exact 09:30-09:35 IEX request shape
    opening = [c for c in transport.calls if c["params"].get("timeframe") == "5Min"][0]
    assert opening["params"]["feed"] == "iex" and opening["params"]["adjustment"] == "raw"
    assert opening["params"]["start"].endswith(("13:30:00Z", "14:30:00Z"))


def test_live_style_account_stops_before_any_other_call(tmp_path):
    setup_logging(tmp_path, "INFO")
    client, transport, _ = make_client(basic_plan_routes(account_number="987654321"))
    with pytest.raises(SafetyError):
        run_entitlement_checks(client, parse_settings({}))
    assert [c["url"].rsplit("/", 1)[-1] for c in transport.calls] == ["account"]


def test_rejected_keys_are_a_safety_stop(tmp_path):
    setup_logging(tmp_path, "INFO")
    routes = basic_plan_routes()
    routes["/v2/account"] = FakeResponse(401, {"message": "unauthorized"})
    client, transport, _ = make_client(routes)
    with pytest.raises(SafetyError):
        run_entitlement_checks(client, parse_settings({}))
    assert len(transport.calls) == 1


def test_sip_history_failure_fails_stage1(tmp_path):
    setup_logging(tmp_path, "INFO")
    routes = basic_plan_routes()
    original = routes["/v2/stocks/bars"]

    def bars(params):
        if params.get("feed") == "sip" and params.get("timeframe") == "1Day" and "SPY" in params["symbols"].split(",")[:1]:
            return FakeResponse(403, {"message": "subscription does not permit"})
        return original(params)

    routes["/v2/stocks/bars"] = bars
    client, _, _ = make_client(routes)
    report = run_entitlement_checks(client, parse_settings({}))
    assert not report["passed"] and "sip_history" in report["required_failed"]


def test_backoff_is_jittered_capped_and_retry_after_is_capped():
    responses = iter([FakeResponse(503, {}), FakeResponse(503, {}), FakeResponse(429, {}, headers={"Retry-After": "600"}),
                      FakeResponse(200, {"ok": True})])
    clock = FakeClock()
    client = ReadOnlyClient(CREDS, 150, 150, transport=FakeTransport({"/v2/clock": lambda p: next(responses)}),
                            clock=clock.now, sleep=clock.sleep, jitter=lambda: 0.0, max_attempts=4)
    assert client.trading_get("/v2/clock").ok
    assert clock.slept == [0.75, 1.5, 30.0]           # 75% floor of 1 s and 2 s; hostile Retry-After capped at 30 s
    assert all(0.75 <= client._backoff(d) / d <= 1.0 for d in (1.0, 8.0))
    assert client._backoff(1000.0) <= 30.0
