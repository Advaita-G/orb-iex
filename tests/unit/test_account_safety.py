"""Account fingerprint allowlist and the declared owner machine (P0-A item 3). Verified in preflight, before the
broker is armed, so before any order activity. This is NOT distributed locking (see docs/SAFETY.md)."""

from pathlib import Path

import pytest

from fakes import FakeTradingTransport, FakeTransport, basic_plan_routes
from orb_iex import cli, http
from orb_iex.config import ConfigError, load_settings, parse_settings
from orb_iex.http import ReadOnlyClient
from orb_iex.preflight import verify_environment
from orb_iex.safety import (INSTANCE_ID_VAR, SafetyError, account_fingerprint, check_account_allowlist,
                            check_instance_owner)
from orb_iex.secrets import Credentials

ROOT = Path(__file__).resolve().parents[2]
CREDS = Credentials("PKFAKEKEYID9876", "fake-secret-value-abc")
ACCOUNT = {"id": "9f1c-uuid-1", "account_number": "PA1234567890", "status": "ACTIVE"}
FP = account_fingerprint(ACCOUNT)


def client(account_number="PA1234567890", account_id="9f1c-uuid-1"):
    routes = basic_plan_routes(account_number=account_number)
    base = routes["/v2/account"]

    def account(p):
        r = base(p)
        r._data = {**r._data, "id": account_id}
        return r
    routes["/v2/account"] = account
    t = FakeTransport(routes)
    return ReadOnlyClient(CREDS, 150, 150, transport=t, clock=lambda: 0.0, sleep=lambda s: None), t


def paper_settings(**safety):
    return parse_settings({"safety": safety})


GOOD = {"allowed_account_fingerprints": [FP], "account_owner_instance_id": "oracle-vm"}


# --- the fingerprint -----------------------------------------------------------------------------------------------

def test_fingerprint_is_stable_non_secret_and_account_specific():
    assert FP == account_fingerprint(dict(ACCOUNT)) and len(FP) == 16 and int(FP, 16) >= 0
    assert "PA1234567890" not in FP and "9f1c" not in FP
    assert account_fingerprint({**ACCOUNT, "id": "other"}) != FP
    assert account_fingerprint({**ACCOUNT, "account_number": "PA0000000001"}) != FP


def test_allowlist_check():
    assert check_account_allowlist(ACCOUNT, (FP,)) == FP
    with pytest.raises(SafetyError, match="empty"):
        check_account_allowlist(ACCOUNT, ())
    with pytest.raises(SafetyError, match="not on the allowlist"):
        check_account_allowlist(ACCOUNT, ("0123456789abcdef",))


def test_owner_check():
    assert check_instance_owner("oracle-vm", "oracle-vm") == "oracle-vm"
    with pytest.raises(SafetyError, match="not set"):
        check_instance_owner("", "oracle-vm")                      # nobody is declared the owner
    with pytest.raises(SafetyError, match=INSTANCE_ID_VAR):
        check_instance_owner("oracle-vm", None)                    # this machine cannot say who it is
    with pytest.raises(SafetyError, match="never trade one account|belong to"):
        check_instance_owner("oracle-vm", "mac")                   # the Mac must not trade the VM's account


# --- config ----------------------------------------------------------------------------------------------------------

def test_settings_default_to_fail_closed_and_validate_fingerprints():
    s = parse_settings({})
    assert s.allowed_account_fingerprints == () and s.account_owner_instance_id == ""
    assert parse_settings({"safety": GOOD}).allowed_account_fingerprints == (FP,)
    for bad in (["short"], "0123456789abcdef", [123], ["ZZZZZZZZZZZZZZZZ"]):
        with pytest.raises(ConfigError):
            parse_settings({"safety": {"allowed_account_fingerprints": bad}})


def test_shipped_files_cannot_start_paper_without_a_person_filling_in_the_account():
    mac = load_settings(ROOT / "config" / "settings.toml")
    assert mac.allowed_account_fingerprints == ("99b682d697a7b5b8",)
    assert mac.account_owner_instance_id == "orb-mac-supervised"
    vm = load_settings(ROOT / "config" / "settings.oracle.toml")
    unit = (ROOT / "deploy" / "oracle" / "orb-iex-paper.service").read_text()
    assert f"Environment={INSTANCE_ID_VAR}={vm.account_owner_instance_id}" in unit             # the VM names itself
    assert vm.account_owner_instance_id and vm.account_owner_instance_id != "mac"


# --- preflight: before any order activity -------------------------------------------------------------------------

def test_paper_preflight_passes_only_with_allowlisted_account_and_matching_owner():
    c, t = client()
    s = verify_environment(c, paper_settings(**GOOD), "paper", environ={INSTANCE_ID_VAR: "oracle-vm"})
    assert s["fingerprint"] == FP and s["instance_id"] == "oracle-vm"
    assert all(call["method"] == "GET" for call in t.calls)


@pytest.mark.parametrize("safety, env, fragment", [
    ({"account_owner_instance_id": "oracle-vm"}, {INSTANCE_ID_VAR: "oracle-vm"}, "empty"),              # no allowlist
    ({**GOOD, "allowed_account_fingerprints": ["0123456789abcdef"]}, {INSTANCE_ID_VAR: "oracle-vm"},
     "not on the allowlist"),                                                                             # wrong account
    ({"allowed_account_fingerprints": [FP]}, {INSTANCE_ID_VAR: "oracle-vm"}, "not set"),                # no owner named
    (GOOD, {}, INSTANCE_ID_VAR),                                                                          # machine anonymous
    (GOOD, {INSTANCE_ID_VAR: "mac"}, "belong to"),                                                       # the Mac
])
def test_paper_preflight_refuses(safety, env, fragment):
    c, t = client()
    with pytest.raises(SafetyError, match=fragment):
        verify_environment(c, paper_settings(**safety), "paper", environ=env)
    assert all(call["method"] == "GET" for call in t.calls)


def test_a_different_account_with_the_right_keys_is_refused():
    c, _ = client(account_number="PA9999999999", account_id="other-uuid")
    with pytest.raises(SafetyError, match="not on the allowlist"):
        verify_environment(c, paper_settings(**GOOD), "paper", environ={INSTANCE_ID_VAR: "oracle-vm"})


def test_signal_and_shadow_need_no_allowlist():
    for mode in ("signal", "shadow"):
        c, _ = client()
        assert verify_environment(c, paper_settings(), mode, environ={})["fingerprint"] == FP


# --- CLI: refused before the broker is armed, so before any write ------------------------------------------------------

def _cfg(tmp_path, safety_lines=""):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path / "rt"}"\n[trading]\nenable_paper_orders = true\n'
                   f'[safety]\nkill_switch_file = "{tmp_path / "KILL"}"\n{safety_lines}')
    return cfg


def _env(monkeypatch, transport, instance=None):
    monkeypatch.setenv("APCA_API_KEY_ID", "PKFAKE12345")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fake-secret-abcdef")
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)
    monkeypatch.delenv(INSTANCE_ID_VAR, raising=False)
    if instance:
        monkeypatch.setenv(INSTANCE_ID_VAR, instance)
    monkeypatch.setattr(http.requests, "Session", lambda: transport)


@pytest.mark.parametrize("safety_lines, instance", [
    ("", "oracle-vm"),                                                               # empty allowlist
    (f'allowed_account_fingerprints = ["0123456789abcdef"]\naccount_owner_instance_id = "oracle-vm"\n', "oracle-vm"),
    (f'allowed_account_fingerprints = ["{FP}"]\naccount_owner_instance_id = "oracle-vm"\n', None),
    (f'allowed_account_fingerprints = ["{FP}"]\naccount_owner_instance_id = "oracle-vm"\n', "mac"),
])
def test_paper_command_refuses_before_any_order_activity(tmp_path, monkeypatch, safety_lines, instance):
    routes = basic_plan_routes()
    base = routes["/v2/account"]

    def account(p):
        r = base(p)
        r._data = {**r._data, "id": "9f1c-uuid-1"}
        return r
    routes["/v2/account"] = account
    t = FakeTradingTransport(routes)
    _env(monkeypatch, t, instance)
    code = cli.main(["--config", str(_cfg(tmp_path, safety_lines)), "--env-file", str(tmp_path / "x"), "paper"])
    assert code == cli.EXIT_SAFETY                                   # exit 2: systemd does NOT restart-loop it
    assert [c for c in t.calls if c["method"] != "GET"] == []
    assert not any(c["url"].endswith("/v2/orders") for c in t.calls)    # not even an order read
    assert "RestartPreventExitStatus=2 3" in (ROOT / "deploy" / "oracle" / "orb-iex-paper.service").read_text()


def test_account_fingerprint_command_prints_the_value(tmp_path, monkeypatch, capsys):
    routes = basic_plan_routes()
    base = routes["/v2/account"]

    def account(p):
        r = base(p)
        r._data = {**r._data, "id": "9f1c-uuid-1"}
        return r
    routes["/v2/account"] = account
    _env(monkeypatch, FakeTransport(routes))
    assert cli.main(["--config", str(_cfg(tmp_path)), "--env-file", str(tmp_path / "x"), "account-fingerprint"]) == 0
    out = capsys.readouterr().out
    assert FP in out and "NOT on the allowlist" in out and "allowed_account_fingerprints" in out
