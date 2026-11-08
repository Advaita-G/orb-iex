import pytest

from orb_iex.safety import (SafetyError, assert_allowed_url, assert_paper_trading_url,
                            check_paper_account, kill_switch_active)
from orb_iex.secrets import (Credentials, CredentialsError, check_base_url_env,
                             load_credentials, load_env_file)


@pytest.mark.parametrize("url", [
    "https://paper-api.alpaca.markets/v2/account",
    "https://data.alpaca.markets/v2/stocks/bars",
])
def test_allowed_urls(url):
    assert_allowed_url(url)


@pytest.mark.parametrize("url", [
    "https://api.alpaca.markets/v2/orders",                       # live trading host
    "http://paper-api.alpaca.markets/v2/account",                 # not https
    "https://paper-api.alpaca.markets.evil.com/v2/account",       # look-alike
    "https://paper-api.alpaca.markets@api.alpaca.markets/v2/x",   # userinfo trick
    "https://paper-api.alpaca.markets:8443/v2/account",           # odd port
    "https://broker-api.alpaca.markets/v1/accounts",
])
def test_refused_urls(url):
    with pytest.raises(SafetyError):
        assert_allowed_url(url)


def test_trading_calls_must_use_paper_host():
    with pytest.raises(SafetyError):
        assert_paper_trading_url("https://data.alpaca.markets/v2/account")


def test_paper_account_accepted():
    summary = check_paper_account({"account_number": "PA3XYZ1234", "status": "ACTIVE"}, "PA")
    assert summary["account_number_suffix"] == "1234"


@pytest.mark.parametrize("acct", [
    {"account_number": "123456789", "status": "ACTIVE"},   # live-style number
    {"account_number": "PA123", "status": "ACCOUNT_CLOSED"},
    {},
])
def test_non_paper_or_inactive_account_refused(acct):
    with pytest.raises(SafetyError):
        check_paper_account(acct, "PA")


def test_live_base_url_env_refused():
    with pytest.raises(SafetyError):
        check_base_url_env({"APCA_API_BASE_URL": "https://api.alpaca.markets"})
    check_base_url_env({"APCA_API_BASE_URL": "https://paper-api.alpaca.markets"})
    check_base_url_env({})


def test_missing_keys_refused():
    with pytest.raises(CredentialsError):
        load_credentials({"APCA_API_KEY_ID": "abc"})


def test_credentials_repr_is_masked():
    c = Credentials("PKTESTKEY1234", "supersecretvalue")
    assert "supersecretvalue" not in repr(c) and "PKTESTKEY1234" not in repr(c)


def test_env_file_does_not_override_environment(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nAPCA_API_KEY_ID="fromfile"\nexport APCA_API_SECRET_KEY=s3cret\n')
    env_file.chmod(0o600)
    environ = {"APCA_API_KEY_ID": "fromenv"}
    warnings = load_env_file(env_file, environ)
    assert environ == {"APCA_API_KEY_ID": "fromenv", "APCA_API_SECRET_KEY": "s3cret"}
    assert warnings == []


def test_env_file_permission_warning(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("APCA_API_KEY_ID=x\n")
    env_file.chmod(0o644)
    assert load_env_file(env_file, {})


def test_kill_switch(tmp_path):
    flag = tmp_path / "KILL"
    assert not kill_switch_active(flag)
    flag.touch()
    assert kill_switch_active(flag)
