"""Append-only experiment ledger: ``docs/experiment-ledger.jsonl``. Stdlib only.

One JSON object per line, three kinds. The latest line for a hypothesis ``id`` wins.

hypothesis  id, day, title, rationale (the economic reason), status, priority (1 = first)
experiment  id, hypothesis, day, spec (dict), data_window {start, end, holdout?}, costs,
            verdict, result (dict), holdout_used (bool), new_evidence (str, see below)
decision    id, hypothesis, day, decision, note   ("blocked" drops it from the backlog; promotion needs
            the owner and is recorded, never applied)

Anti-tuning rules, enforced by ``validate``:
* An experiment's fingerprint is a hash of hypothesis + spec + data window. A second experiment
  with the same fingerprint must carry ``new_evidence`` (what changed); otherwise it is a rerun.
* A hypothesis may touch its locked holdout once (``holdout_used``). A second holdout look needs a
  different data window, which changes the fingerprint and must also state ``new_evidence``.
* A ``supported`` verdict must report the 2x-cost result and the holdout, never only the best period.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HYPOTHESIS_STATUS = ("proposed", "preregistered", "testing", "rejected", "inconclusive", "promotion_candidate")
OPEN_STATUS = ("proposed", "preregistered", "testing")
VERDICTS = ("rejected", "inconclusive", "supported", "invalid")


def fingerprint(hypothesis: str, spec: dict, data_window: dict) -> str:
    blob = json.dumps({"h": hypothesis, "s": spec, "w": data_window}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _text(row: dict, key: str) -> bool:
    return isinstance(row.get(key), str) and bool(row[key].strip())


def _problem(row: Any) -> str | None:
    if not isinstance(row, dict):
        return "entry is not an object"
    kind = row.get("kind")
    if kind not in ("hypothesis", "experiment", "decision"):
        return "kind must be hypothesis, experiment or decision"
    for key in ("id", "day"):
        if not _text(row, key):
            return f"missing {key}"
    if kind == "hypothesis":
        if not _text(row, "title") or not _text(row, "rationale"):
            return "a hypothesis needs a title and an economic rationale"
        if row.get("status") not in HYPOTHESIS_STATUS:
            return f"status must be one of {HYPOTHESIS_STATUS}"
        if not isinstance(row.get("priority"), int) or row["priority"] < 1:
            return "priority must be an integer >= 1"
    elif kind == "experiment":
        if not _text(row, "hypothesis"):
            return "missing hypothesis"
        if not isinstance(row.get("spec"), dict) or not isinstance(row.get("data_window"), dict):
            return "spec and data_window must be objects"
        if row.get("verdict") not in VERDICTS:
            return f"verdict must be one of {VERDICTS}"
        if row["verdict"] == "supported":
            res = row.get("result") or {}
            if not isinstance(res, dict) or "cost_2x" not in res or "holdout" not in res:
                return "a supported verdict must report result.cost_2x and result.holdout"
    elif not _text(row, "hypothesis") or not _text(row, "decision"):
        return "a decision needs a hypothesis and a decision"
    return None


class ResearchLedger:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"state": "missing", "path": str(self.path), "hypotheses": [], "experiments": [],
                    "decisions": [], "backlog": [], "errors": [], "summary": _summary([], [])}
        rows, errors = [], []
        for n, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                errors.append({"line": n, "error": f"not JSON: {exc}"})
                continue
            problem = _problem(row)
            if problem:
                errors.append({"line": n, "error": problem})
                continue
            rows.append((n, row))
        latest: dict[str, dict] = {}
        for _, r in rows:
            if r["kind"] == "hypothesis":
                latest.pop(r["id"], None)
                latest[r["id"]] = r
        exps = [r for _, r in rows if r["kind"] == "experiment"]
        seen: dict[str, str] = {}
        holdouts: dict[str, int] = {}
        for n, r in rows:
            if r["kind"] != "experiment":
                continue
            fp = fingerprint(r["hypothesis"], r["spec"], r["data_window"])
            r["fingerprint"] = fp
            if fp in seen and not _text(r, "new_evidence"):
                errors.append({"line": n, "error": f"rerun of {seen[fp]} (same fingerprint {fp}) without new_evidence"})
            seen.setdefault(fp, r["id"])
            if r.get("holdout_used"):
                holdouts[r["hypothesis"]] = holdouts.get(r["hypothesis"], 0) + 1
                if holdouts[r["hypothesis"]] > 1 and not _text(r, "new_evidence"):
                    errors.append({"line": n, "error": f"second holdout look for {r['hypothesis']} without new_evidence"})
        hyps = list(latest.values())
        decided = {}
        for _, r in rows:
            if r["kind"] == "decision":
                decided[r["hypothesis"]] = r["decision"]
        backlog = sorted((h for h in hyps if h["status"] in OPEN_STATUS and decided.get(h["id"]) != "blocked"),
                         key=lambda h: (h["priority"], h["day"]))
        return {"state": "ok", "path": str(self.path), "hypotheses": hyps, "experiments": exps,
                "decisions": [r for _, r in rows if r["kind"] == "decision"],
                "backlog": backlog, "errors": errors, "summary": _summary(hyps, exps)}


def _summary(hyps: list[dict], exps: list[dict]) -> dict[str, int]:
    return {"hypotheses": len(hyps), "open": sum(h["status"] in OPEN_STATUS for h in hyps),
            "rejected": sum(h["status"] == "rejected" for h in hyps),
            "promotion_candidates": sum(h["status"] == "promotion_candidate" for h in hyps),
            "experiments": len(exps), "supported": sum(e["verdict"] == "supported" for e in exps)}
