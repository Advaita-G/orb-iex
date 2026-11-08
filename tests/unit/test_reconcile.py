import json
from decimal import Decimal as D
from pathlib import Path

from orb_iex.reconcile import (compare, compare_trips, fills_from_journal, reconstruct, totals)

ROOT = Path(__file__).resolve().parents[2]


def o(i, sym, side, qty, px, ts, role="entry", day="20260928"):
    return {"id": i, "client_order_id": f"orbiex-{day}-{sym}-{role}", "symbol": sym, "side": side,
            "filled_qty": str(qty), "filled_avg_price": None if px is None else str(px), "filled_at": ts}


def test_vwap_partials_gross_actual_and_modeled_costs_are_separate():
    orders = [o("a", "X", "sell", 10, 100, "2026-09-28T13:35:01Z"), o("b", "X", "sell", 30, 101, "2026-09-28T13:35:02Z"),
              o("c", "X", "buy", 40, 102, "2026-09-28T13:40:00Z", "stop")]
    (t,), opened = reconstruct(orders, risk_per_share=None)
    assert not opened and t["direction"] == -1 and t["qty"] == 40
    assert t["entry_vwap"] == D("100.75") and t["exit_vwap"] == D(102)
    assert t["gross_pnl"] == t["actual_realized_pnl"] == D(-50)
    assert t["modeled_commission"] == D("0.0035") * 80 and t["modeled_slippage"] == D("0.01") * 80
    assert t["net_modeled_pnl"] == t["gross_pnl"] - t["modeled_cost"]


def test_r_uses_planned_risk_not_the_mutated_stop():
    orders = [o("a", "X", "buy", 10, 50, "2026-09-28T13:35:01Z"), o("b", "X", "sell", 10, 49, "2026-09-28T13:36:00Z", "stop")]
    (t,), _ = reconstruct(orders, risk_per_share={("2026-09-28", "X"): D("0.5")})
    assert t["r"] == D(-2)


def test_carry_over_is_one_trade_across_days_and_open_positions_are_reported():
    orders = [o("a", "C", "sell", 5, 20, "2026-09-30T19:59:00Z"), o("b", "C", "buy", 5, 21, "2026-10-01T13:30:00Z", "eod"),
              o("c", "D", "buy", 7, 10, "2026-09-30T14:00:00Z")]
    (t,), opened = reconstruct(orders)
    assert t["carry"] and t["gross_pnl"] == D(-5) and t["day"] == "2026-09-30"
    assert [(x["symbol"], x["open_qty"]) for x in opened] == [("D", D(7))]


def test_sign_flip_is_split_and_flagged_not_hidden():
    orders = [o("a", "S", "buy", 10, 10, "2026-09-28T14:00:00Z"), o("b", "S", "sell", 15, 11, "2026-09-28T14:05:00Z", "stop")]
    (t,), opened = reconstruct(orders)
    assert t["sign_flip"] and t["gross_pnl"] == D(10) and opened[0]["direction"] == -1 and opened[0]["open_qty"] == 5


def test_duplicate_exit_after_flat_opens_a_visible_position_instead_of_vanishing():
    orders = [o("a", "S", "buy", 10, 10, "2026-09-28T14:00:00Z"), o("b", "S", "sell", 10, 11, "2026-09-28T14:05:00Z", "stop"),
              o("c", "S", "sell", 10, 11, "2026-09-28T14:05:01Z", "eod")]
    closed, opened = reconstruct(orders)
    assert len(closed) == 1 and opened[0]["open_qty"] == 10


def test_partial_fill_of_a_cancelled_order_counts_and_unfilled_orders_do_not():
    orders = [o("a", "S", "buy", 3, 10, "2026-09-28T14:00:00Z"), o("z", "S", "buy", 0, None, None),
              o("b", "S", "sell", 3, 12, "2026-09-28T14:05:00Z", "stop")]
    (t,), _ = reconstruct(orders)
    assert t["qty"] == 3 and t["gross_pnl"] == D(6)


def test_foreign_orders_are_ignored():
    f = o("a", "S", "buy", 3, 10, "2026-09-28T14:00:00Z")
    f["client_order_id"] = "manual-1"
    assert reconstruct([f]) == ([], [])


def test_historical_broker_snapshot_totals():
    snap = json.loads((ROOT / "research/broker_snapshot_20261003.json").read_text())
    closed, opened = reconstruct(snap["orders"])
    assert len(closed) == 27 and {x["symbol"] for x in opened} == {"CTVA", "SPHD"}
    assert totals(closed)["gross_pnl"].quantize(D("0.01")) == D("-1730.49")
    assert totals(closed)["net_modeled_pnl"].quantize(D("0.01")) == D("-2025.87")


def test_historical_partial_fills_equal_broker_fills():
    snap = json.loads((ROOT / "research/broker_snapshot_20261003.json").read_text())
    closed, opened = reconstruct([o for o in snap['orders'] if o['symbol'] == 'NKE'])
    rows = json.loads((ROOT / 'tests/fixtures/nke_20261002_fills.json').read_text())
    jc, jo = reconstruct(fills=fills_from_journal(rows))
    assert len(closed) == len(jc) == 1 and not opened and not jo
    assert compare_trips(closed, jc) == []
    assert totals(jc)['gross_pnl'].quantize(D('0.01')) == D('-50.17')


def test_compare_never_silently_omits_a_trade_or_a_value():
    closed, _ = reconstruct([o("a", "X", "buy", 1, 10, "2026-09-28T14:00:00Z"), o("b", "X", "sell", 1, 11, "2026-09-28T14:01:00Z", "stop"),
                             o("c", "Y", "buy", 1, 10, "2026-09-28T14:00:00Z"), o("d", "Y", "sell", 1, 9, "2026-09-28T14:01:00Z", "stop")])
    jt = [{"symbol": "X", "day": "2026-09-28", "entry_time": "t", "status": "closed", "gross_pnl": "2.00",
           "net_pnl_modeled": "1.00", "modeled_commission": "0", "modeled_slippage": "0"}]
    kinds = {(i["kind"], i["symbol"]) for i in compare(closed, [], jt)}
    assert ("missing_in_journal", "Y") in kinds and ("value_mismatch", "X") in kinds


def test_compare_trips_preserves_multiple_same_symbol_same_day_round_trips():
    first, _ = reconstruct([o("a", "X", "buy", 1, 10, "2026-09-28T14:00:00Z"),
                            o("b", "X", "sell", 1, 11, "2026-09-28T14:01:00Z", "stop")])
    second, _ = reconstruct([o("c", "X", "buy", 1, 10, "2026-09-28T15:00:00Z"),
                             o("d", "X", "sell", 1, 11, "2026-09-28T15:01:00Z", "stop")])
    assert compare_trips(first + second, second + first) == []
    assert [r["kind"] for r in compare_trips(second, first + second)] == ["missing_at_broker"]
    assert [r["kind"] for r in compare_trips(first + second, second)] == ["missing_in_journal"]
