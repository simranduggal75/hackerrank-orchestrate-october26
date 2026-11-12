"""Evaluate the decision engine on the public samples (the only labelled data).

The samples are used for VALIDATION only: nothing here feeds back into decision rules. The 32 cases are
split deterministically by the parity of the case number into a development half (odd) and a blind half
(even); during rule work only the aggregate numbers of the blind half are looked at.
"""

from __future__ import annotations

import csv

from engine import DecisionEngine
from evaluation import scoring
from loaders import DATASET_DIR, Dataset
from validate_output import Validator


def split_of(case_id: str) -> str:
    return "development" if int(case_id.rsplit("_", 1)[1]) % 2 else "blind"


def evaluate_samples(ds: Dataset, engine: DecisionEngine | None = None) -> dict:
    engine = engine or DecisionEngine(ds)
    with open(DATASET_DIR / "sample_output.csv", encoding="utf-8", newline="") as handle:
        reference = scoring.load_reference(list(csv.DictReader(handle)))
    cases = {c.case_id: c for c in ds.sample_requests}
    decisions = {cid: engine.decide(cases[cid]) for cid in reference}
    predicted = {cid: (d.status, d.steps) for cid, d in decisions.items()}
    validator = Validator(ds)
    problems = [p for cid, d in decisions.items() for p in validator.check_row(cid, d.status, d.steps_json, cases[cid].audit_at)]

    def explain(cid: str, step: dict, predicted_steps: list[dict]) -> str:
        assessment, footprint = engine.assess(cases[cid])
        rid = step["resource_id"]
        verdict = next((v for v in assessment.verdicts if v.resource_id == rid), None)
        if verdict is None:
            hints = sorted({u.kind for u in footprint.unresolved if u.resource_id == rid})
            return "resource not linked to the subject: " + (", ".join(hints) if hints else "no link evidence at or before audit_at")
        near = [p for p in predicted_steps if p["resource_id"] == rid and p["operation"] == step["operation"]]
        if near:
            return "same step emitted with a different system_id/state"
        if verdict.kind == "UNRESOLVED":
            return "resource linked but withheld as unresolved: " + (verdict.reasons[0].code if verdict.reasons else "?")
        if verdict.kind == "COMPLIANT" and not verdict.verify:
            return "resource linked and compliant, verification not required by the policy"
        return "resource linked; different treatment (" + verdict.kind + ")"

    result = {"all": scoring.score(reference, predicted), "problems": problems,
              "triage": scoring.triage(reference, predicted, explain)}
    for half in ("development", "blind"):
        subset = {cid for cid in reference if split_of(cid) == half}
        result[half] = scoring.score({c: reference[c] for c in subset}, {c: predicted[c] for c in subset})
    result["reachability"] = _reachability(reference, decisions)
    result["status_misses"] = _status_misses(reference, decisions)
    result["engine"] = engine  # for the report's dataset-wide gap counts; not part of the metrics
    return result


def _status_misses(reference, decisions) -> dict:
    """Wrong statuses grouped by (reference, predicted, failed gates): aggregate causes, no per-case listing."""
    misses: dict[str, int] = {}
    for cid, (status, _) in reference.items():
        d = decisions[cid]
        if d.status != status:
            gates = ",".join(g.gate for g in d.assessment.failed_gates) or "none"
            key = f"{status} -> {d.status} ({d.assessment.assessment}; failed gates: {gates})"
            misses[key] = misses.get(key, 0) + 1
    return dict(sorted(misses.items()))


def _reachability(reference, decisions) -> dict:
    """How much of the reference is reachable at all: reference steps whose resource the engine owns."""
    total = reachable = 0
    for cid, (_, steps) in reference.items():
        owned = {v.resource_id for v in decisions[cid].assessment.verdicts}
        for s in steps:
            total += 1
            reachable += s["resource_id"] in owned
    return {"reference_steps": total, "steps_whose_resource_is_owned": reachable,
            "ceiling_share": reachable / total if total else 0.0}
