"""The preregistered experiment matrix (docs/ORB-RESEARCH-PREREG-2026-10-05.md). Do not edit after results exist."""
from __future__ import annotations

from dataclasses import replace

from .orb import OrbSpec

B0 = OrbSpec()
M = lambda h, m: h * 60 + m  # noqa: E731

SPECS: dict[str, OrbSpec] = {
    "B0": B0,
    "E1_or1": replace(B0, or_minutes=1),
    "E1_or10": replace(B0, or_minutes=10),
    "E1_or15": replace(B0, or_minutes=15),
    "E2_cut0950": replace(B0, cutoff_minute=M(9, 50)),
    "E2_cut1000": replace(B0, cutoff_minute=M(10, 0)),
    "E2_cut1030": replace(B0, cutoff_minute=M(10, 30)),
    "E3_rvol2": replace(B0, min_rvol=2.0),
    "E3_rvol3": replace(B0, min_rvol=3.0),
    "E4_atrpct": replace(B0, min_atr_pct=0.015),
    "E5_stop025": replace(B0, stop_atr_frac=0.25),
    "E5_stop050": replace(B0, stop_atr_frac=0.50),
    "E5_stoporb": replace(B0, stop_kind="or"),
    "E6_target4r": replace(B0, target_r=4.0),
    "E7_etf": replace(B0, include="etf"),
    "E7_stocks": replace(B0, include="stocks"),
    "E9_optimistic": replace(B0, same_bar="optimistic"),
}

# specs whose candidate set differs from B0's, i.e. those that need their own intraday paths
SELECTION_SPECS = ["B0", "E1_or1", "E1_or10", "E1_or15", "E4_atrpct", "E7_etf", "E7_stocks"]
