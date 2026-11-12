"""Output invariants from the challenge contract. Used by tests, by the evaluation, and (later) before writing output.csv.

Each function returns a list of human-readable violations; an empty list means the output is valid.
"""

from __future__ import annotations

import json

import policy
from loaders import Dataset

HEADER = ["case_id", "deletion_status", "steps_json"]


class Validator:
    def __init__(self, ds: Dataset):
        self.evidence = {e.file_id: e.captured_at for e in ds.evidence_files}
        self.activity = {a.activity_id: a.event_time for a in ds.activity}
        self.audit = {a.audit_id: a.event_time for a in ds.audit_log}

    # ------------------------------------------------------------------ citations
    def citation_problem(self, evidence_id: str, audit_at: str) -> str | None:
        """None when the id exists and was captured/recorded at or before audit_at."""
        for table, label in ((self.evidence, "evidence file"), (self.activity, "activity"), (self.audit, "audit")):
            if evidence_id in table:
                return None if table[evidence_id] <= audit_at else f"{label} {evidence_id} is after audit_at"
        return f"{evidence_id!r} is not a known file_id, activity_id or audit_id"

    # ----------------------------------------------------------------------- row
    def check_row(self, case_id: str, status: str, steps_json: str, audit_at: str) -> list[str]:
        problems: list[str] = []
        say = lambda msg: problems.append(f"{case_id}: {msg}")
        if status not in policy.STATUSES:
            say(f"bad status {status!r}")
        try:
            steps = json.loads(steps_json)
        except ValueError as exc:
            return problems + [f"{case_id}: steps_json is not valid JSON ({exc})"]
        if not isinstance(steps, list):
            return problems + [f"{case_id}: steps_json is not a JSON array"]
        if status == "NOT_APPLICABLE" and steps:
            say("NOT_APPLICABLE must have an empty steps array")
        keys = set()
        previous = None
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict) or list(step) != list(policy.STEP_FIELDS) and set(step) != set(policy.STEP_FIELDS):
                say(f"step {index} does not have exactly the contract fields")
                continue
            if step["step_no"] != index:
                say(f"step numbers must be consecutive from 1 (step {index} has {step['step_no']})")
            phase, op, state = step["phase"], step["operation"], step["state"]
            if (phase, op, state) not in policy.ALLOWED_PAIRS:
                say(f"step {index}: ({phase}, {op}, {state}) is not an allowed phase/operation/state")
                continue
            if not step["system_id"] or not step["resource_id"]:
                say(f"step {index}: system_id and resource_id must be non-empty")
            key = (phase, step["system_id"], step["resource_id"], op)
            if key in keys:
                say(f"step {index} duplicates {key}")
            keys.add(key)
            order = (policy.PHASE_RANK[phase], policy.OP_RANK[op], step["system_id"], step["resource_id"])
            if previous is not None and order < previous:
                say(f"step {index} is out of order (phase, operation, system_id, resource_id)")
            previous = order
            self._check_citation(say, status, step, index, audit_at)
        return problems

    def _check_citation(self, say, status, step, index, audit_at) -> None:
        phase, cite = step["phase"], step["evidence_id"]
        if status == "COMPLETE" and phase != "VERIFICATION":
            say(f"step {index}: a COMPLETE case may contain only verification steps")
        must_cite = phase == "FINDING" or status == "COMPLETE"
        if status == "INCOMPLETE" and phase != "FINDING" and cite != "":
            say(f"step {index}: {phase} steps of an INCOMPLETE case must have an empty evidence_id")
        if must_cite:
            if not cite:
                say(f"step {index}: a {phase} step here must cite evidence")
            else:
                problem = self.citation_problem(cite, audit_at)
                if problem:
                    say(f"step {index}: {problem}")
        elif cite:
            problem = self.citation_problem(cite, audit_at)
            if problem:
                say(f"step {index}: {problem}")

    # ------------------------------------------------------------------------ file
    @staticmethod
    def check_rows(rows: list[tuple[str, str, str]], expected_case_ids: list[str]) -> list[str]:
        problems = []
        ids = [r[0] for r in rows]
        if len(ids) != len(set(ids)):
            problems.append("duplicate case_id rows")
        if set(ids) != set(expected_case_ids):
            missing, extra = set(expected_case_ids) - set(ids), set(ids) - set(expected_case_ids)
            problems.append(f"case ids differ from the request file (missing {len(missing)}, unexpected {len(extra)})")
        if ids != sorted(ids):
            problems.append("rows are not sorted by case_id")
        return problems
