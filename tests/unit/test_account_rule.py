"""Operating rule (decided 2026-10-01): ONE Alpaca paper account PER MACHINE. The Mac and the VM each have their own
account, keys and fingerprint; each settings file allowlists only its own account; a config/account mismatch fails
startup before the broker is armed. No Alpaca-side lease is implemented."""

from pathlib import Path

import pytest

from fakes import FakeTradingTransport, basic_plan_routes
from orb_iex import cli, http
from orb_iex.config import ConfigError, load_settings, parse_settings
from orb_iex.safety import INSTANCE_ID_VAR, account_fingerprint

ROOT = Path(__file__).resolve().parents[2]
MAC = {"id": "mac-uuid", "account_number": "PA1111111111", "status": "ACTIVE"}
VM = {"id": "vm-uuid", "account_number": "PA2222222222", "status": "ACTIVE"}


def test_a_config_may_allowlist_only_one_account():
    one = account_fingerprint(MAC)
    assert parse_settings({"safety": {"allowed_account_fingerprints": [one]}}).allowed_account_fingerprints == (one,)
    assert parse_settings({"safety": {"allowed_account_fingerprints": []}}).allowed_account_fingerprints == ()
    with pytest.raises(ConfigError, match="only its own account"):
        parse_settings({"safety": {"allowed_account_fingerprints": [one, account_fingerprint(VM)]}})


def test_the_two_accounts_have_different_fingerprints():
    assert account_fingerprint(MAC) != account_fingerprint(VM)


def test_shipped_configs_name_at_most_one_account_each_and_never_the_same_one():
    mac, vm = (load_settings(ROOT / "config" / n) for n in ("settings.toml", "settings.oracle.toml"))
    assert len(mac.allowed_account_fingerprints) <= 1 and len(vm.allowed_account_fingerprints) <= 1
    assert not set(mac.allowed_account_fingerprints) & set(vm.allowed_account_fingerprints)
    assert mac.account_owner_instance_id != vm.account_owner_instance_id          # distinct machine identities
    assert vm.account_owner_instance_id == "oracle-vm"


def _cfg(tmp_path, fingerprint, owner):
    cfg = tmp_path / "settings.toml"
    cfg.write_text(f'[runtime]\ndata_dir = "{tmp_path / "rt"}"\n[trading]\nenable_paper_orders = true\n'
                   f'[safety]\nkill_switch_file = "{tmp_path / "KILL"}"\nallowed_account_fingerprints = ["{fingerprint}"]\n'
                   f'account_owner_instance_id = "{owner}"\n')
    return cfg


def _run_paper(tmp_path, monkeypatch, account, fingerprint, owner, instance):
    routes = basic_plan_routes(account_number=account["account_number"])
    base = routes["/v2/account"]

    def acct(p):
        r = base(p)
        r._data = {**r._data, "id": account["id"]}
        return r
    routes["/v2/account"] = acct
    t = FakeTradingTransport(routes)
    monkeypatch.setenv("APCA_API_KEY_ID", "PKFAKE12345")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "fake-secret-abcdef")
    monkeypatch.delenv("APCA_API_BASE_URL", raising=False)
    monkeypatch.setenv(INSTANCE_ID_VAR, instance)
    monkeypatch.setattr(http.requests, "Session", lambda: t)
    code = cli.main(["--config", str(_cfg(tmp_path, fingerprint, owner)), "--env-file", str(tmp_path / "x"), "paper"])
    return code, t


def test_vm_keys_on_the_mac_config_fail_startup_before_anything_is_armed(tmp_path, monkeypatch):
    code, t = _run_paper(tmp_path, monkeypatch, VM, account_fingerprint(MAC), "mac", "mac")   # VM account, Mac allowlist
    assert code == cli.EXIT_SAFETY
    assert [c for c in t.calls if c["method"] != "GET"] == [] and not any(c["url"].endswith("/v2/orders") for c in t.calls)


def test_mac_keys_on_the_vm_config_fail_startup_before_anything_is_armed(tmp_path, monkeypatch):
    code, t = _run_paper(tmp_path, monkeypatch, MAC, account_fingerprint(VM), "oracle-vm", "oracle-vm")
    assert code == cli.EXIT_SAFETY and [c for c in t.calls if c["method"] != "GET"] == []


def test_the_right_account_on_the_wrong_machine_fails_startup(tmp_path, monkeypatch):
    code, t = _run_paper(tmp_path, monkeypatch, VM, account_fingerprint(VM), "oracle-vm", "mac")
    assert code == cli.EXIT_SAFETY and [c for c in t.calls if c["method"] != "GET"] == []


def test_the_operating_rule_is_documented_and_no_lease_exists():
    safety = (ROOT / "docs" / "SAFETY.md").read_text()
    for needle in ("one paper account per machine", "The Mac bot has its OWN Alpaca paper account",
                   "The VM bot has a DIFFERENT paper account", "allowlists only its own account",
                   "Not implemented, on purpose: a lease"):
        assert needle in safety, needle
    assert "orbiex-lease" not in safety
    assert "one paper account per machine" in (ROOT / "docs" / "RUNBOOK.md").read_text().lower()
    src = "".join(p.read_text() for p in (ROOT / "src" / "orb_iex").glob("*.py"))
    import re
    assert not re.search(r"watchlist|\blease\b", src.lower())            # (the word "release" is unrelated)
