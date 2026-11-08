"""Synthetic acceptance and fault checks; none of these are performance evidence."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from helpers import Harness, bar, et
from orb_iex.candidate_b import adapt_plan, liquidity_check
from orb_iex.config import ConfigError, load_settings, parse_settings
from orb_iex.marketdata import Quote, parse_quote
from orb_iex.safety import SafetyError
from orb_iex.strategy import Features
from test_runner import DAY, make


def test_config_cli_works_without_credentials(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from orb_iex import cli
    path = tmp_path / "candidate.toml"
    path.write_text('[strategy]\nresearch_profile = "candidate_b"\n')
    monkeypatch.setattr(cli, "load_credentials", lambda: pytest.fail("offline command read credentials"))
    assert cli.cmd_check_config(Namespace(config=path)) == 0
    assert "profile=candidate_b" in capsys.readouterr().out


def test_profile_configuration_cannot_enable_orders():
    assert parse_settings({}).research_profile == "baseline_a"
    with pytest.raises(ConfigError, match="unqualified"):
        parse_settings({"strategy": {"research_profile": "candidate_b"},
                        "trading": {"enable_paper_orders": True}})
    with pytest.raises(ConfigError):
        parse_settings({"strategy": {"research_profile": "typo"}})


def test_candidate_rejects_paper_runner_before_broker_calls(tmp_path):
    with pytest.raises(SafetyError, match="unqualified"):
        make(tmp_path, mode="paper", strategy__research_profile="candidate_b")


@pytest.mark.parametrize("direction", [1, -1])
def test_participation_only_reduces_size_and_preserves_levels(tmp_path, direction):
    h = Harness(tmp_path)
    p = h.plan(direction=direction, qty=1000)
    f = Features("AAA", bar(et(DAY, 9, 30), 50, 51, 49, 50.5, 12345), D(2), D(2), D(2_000_000))
    b = adapt_plan(p, f)
    assert b.qty == 123 and p.qty == 1000
    assert b.entry_price == p.entry_price and b.stop_price == p.stop_price and b.atr == p.atr
    assert b.sizing["baseline_qty"] == 1000
    assert adapt_plan(p, replace(f, opening=None)) == "candidate_missing_opening_volume"
    assert adapt_plan(p, replace(f, opening=replace(f.opening, v=99))) == "candidate_zero_liquidity_quantity"


@pytest.mark.parametrize("direction", [1, -1])
def test_cost_boundary_and_correct_book_side(direction):
    q = Quote("AAA", D("49.977"), D("50.000"), bid_size=100, ask_size=100)
    args = dict(direction=direction, qty=100, risk=D("0.20"), commission=D("0.0035"), slippage=D("0.01"))
    assert liquidity_check(q, **args)["reason"] is None  # 0.023 + 0.027 = 0.25 * 0.20
    assert liquidity_check(replace(q, bid=D("49.976")), **args)["reason"] == "candidate_cost_risk"
    side = "ask_size" if direction > 0 else "bid_size"
    assert liquidity_check(replace(q, **{side: 99}), **args)["reason"] == "candidate_displayed_size"
    assert liquidity_check(replace(q, **{side: None}), **args)["reason"] == "candidate_displayed_size"


def candidate_harness(tmp_path, *, profile="candidate_b", mode="shadow", size=1000, spread="0.01"):
    h = Harness(tmp_path, strategy__research_profile=profile)
    h.engine.mode = mode
    h.open_session()
    h.j.save_diagnostic(h.day, "ab_plans", {"AAA": {"baseline_qty": 100, "candidate_qty": 100}})
    h.engine.quote_source = lambda symbols, now: {
        "AAA": Quote("AAA", D("49.90") - D(spread), D("49.90"), now,
                     bid_size=size, ask_size=size)}
    return h


def test_candidate_engine_uses_quote_gate_and_does_not_retry(tmp_path):
    h = candidate_harness(tmp_path, size=99)
    h.enter()
    assert h.trade().note == "candidate_displayed_size" and not h.sim.submitted
    assert h.j.diagnostic(h.day, "ab_quote:AAA")["ab"]["baseline_reason"] is None
    h.engine.quote_source = h.default_quotes
    h.engine.submit_entry(h.trade())
    h.engine.recover(h.clock.now())
    assert not h.sim.submitted
    assert h.engine.stats["candidate_liquidity_skipped"] == 1


def test_baseline_records_rejected_candidate_without_filtering_its_order(tmp_path):
    h = candidate_harness(tmp_path, profile="baseline_a", size=None)
    h.enter()
    assert h.trade().state == "entry_open"
    assert h.j.diagnostic(h.day, "ab_quote:AAA")["ab"]["candidate_reason"] == "candidate_displayed_size"


def test_candidate_paper_guard_survives_configuration_bypass(tmp_path):
    h = candidate_harness(tmp_path, mode="paper")
    h.enter()
    assert not h.sim.submitted
    assert h.trade().state == "entry_rejected"


def test_candidate_paper_block_does_not_suppress_protection_of_existing_fills(tmp_path):
    h = candidate_harness(tmp_path)
    h.enter()
    entry = h.trade().entry_order_id
    h.engine.mode = "paper"  # simulate recovery of an existing fill under the block
    h.sim.fill(entry, D(100), D("50"))
    h.pump()
    assert h.trade().protected_qty == D(100)
    assert len(h.orders_by_role("stop")) == 1


def test_missing_paired_plan_fails_closed(tmp_path):
    h = candidate_harness(tmp_path)
    h.j.save_diagnostic(h.day, "ab_plans", {})
    h.enter()
    assert not h.sim.submitted and h.trade().note == "candidate_missing_paired_plan"


def test_candidate_shadow_full_day_preserves_top20_and_finishes_flat(tmp_path):
    r, clock, data, j, sim = make(tmp_path, strategy__research_profile="candidate_b")
    original = data.entry_quotes
    r.engine.quote_source = lambda syms, now: {
        s: replace(q, bid_size=10000, ask_size=10000, ask=q.bid + D("0.01"))
        for s, q in original(syms, now).items()}
    r.run(until=et(DAY, 16, 5))
    plans = j.diagnostic(DAY, "ab_plans")
    assert len(plans) == 20 and all(p["candidate_qty"] <= p["baseline_qty"] for p in plans.values())
    assert {c["rank"] for c in j.candidates(DAY) if c.get("rank") and c["rank"] <= 20} == set(range(1, 21))
    assert sim.submitted and not sim.list_positions()
    assert not any(o.status not in ("filled", "canceled", "rejected", "expired") for o in j.orders())


@pytest.mark.parametrize("size, expected", [(2, 2), (0, 0), (None, None), (True, None),
                                              (-1, None), ("bad", None), ("Infinity", None), (1.5, None)])
def test_quote_sizes_do_not_invent_shares_or_break_valid_prices(size, expected):
    q = parse_quote("AAA", {"bp": "49", "ap": "50", "bs": size, "as": size})
    assert q.bid == D(49) and q.ask == D(50)
    assert q.bid_size == expected and q.ask_size == expected
