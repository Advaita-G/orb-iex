import json

from orb_iex.research_ledger import ResearchLedger, fingerprint

H = {"kind": "hypothesis", "id": "H1", "day": "2026-10-27", "title": "t", "rationale": "because",
     "status": "proposed", "priority": 2}
SPEC, WIN = {"or": 1}, {"start": "2016-01-04", "end": "2023-12-29"}


def exp(id_, **kw):
    return {"kind": "experiment", "id": id_, "hypothesis": "H1", "day": "2026-10-27", "spec": SPEC,
            "data_window": WIN, "verdict": "rejected", **kw}


def ledger(tmp_path, rows):
    p = tmp_path / "l.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return ResearchLedger(p).read()


def test_missing_file_is_reported_not_an_error(tmp_path):
    assert ResearchLedger(tmp_path / "none.jsonl").read()["state"] == "missing"


def test_latest_hypothesis_line_wins_and_backlog_is_ranked(tmp_path):
    b = ledger(tmp_path, [H, {**H, "id": "H2", "priority": 1}, {**H, "status": "rejected"}])
    assert [h["id"] for h in b["backlog"]] == ["H2"] and b["summary"]["rejected"] == 1 and not b["errors"]


def test_identical_rerun_needs_new_evidence(tmp_path):
    bad = ledger(tmp_path, [H, exp("E1"), exp("E2")])
    assert "rerun of E1" in bad["errors"][0]["error"]
    ok = ledger(tmp_path, [H, exp("E1"), exp("E2", new_evidence="2026-10 data added")])
    assert not ok["errors"]
    assert fingerprint("H1", SPEC, WIN) == ok["experiments"][0]["fingerprint"]


def test_second_holdout_look_needs_new_evidence(tmp_path):
    other = {"start": "2024-01-02", "end": "2026-10-27"}
    rows = [H, exp("E1", holdout_used=True), {**exp("E2", holdout_used=True), "data_window": other}]
    assert "holdout" in ledger(tmp_path, rows)["errors"][0]["error"]


def test_supported_must_report_2x_costs_and_holdout(tmp_path):
    assert ledger(tmp_path, [H, exp("E1", verdict="supported", result={"cost_2x": -0.1})])["errors"]
    assert not ledger(tmp_path, [H, exp("E1", verdict="supported", result={"cost_2x": 0.1, "holdout": 0.1})])["errors"]


def test_malformed_lines_are_reported_with_line_numbers(tmp_path):
    p = tmp_path / "l.jsonl"
    p.write_text("not json\n" + json.dumps({**H, "priority": 0}) + "\n")
    errs = ResearchLedger(p).read()["errors"]
    assert [e["line"] for e in errs] == [1, 2]


def test_blocked_hypothesis_leaves_the_backlog(tmp_path):
    b = ledger(tmp_path, [H, {"kind": "decision", "id": "D1", "hypothesis": "H1", "day": "2026-10-27",
                              "decision": "blocked"}])
    assert b["backlog"] == [] and not b["errors"]
