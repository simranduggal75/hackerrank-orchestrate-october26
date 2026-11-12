"""Produce and validate the submission file ``output.csv`` (repository root).

Rows are decided by the production ``DecisionEngine`` only and written after every output invariant has passed;
nothing is written otherwise. Rows are sorted by ``case_id`` as plain strings (the literal reading of "sorted by
case_id"; the ids are not zero-padded, so this differs from numeric order). Output is UTF-8 with ``\\n`` line endings.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from engine import DecisionEngine
from loaders import Dataset
from validate_output import HEADER, Validator

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = ROOT / "output.csv"


def build_rows(ds: Dataset, engine: DecisionEngine | None = None) -> list[tuple[str, str, str]]:
    engine = engine or DecisionEngine(ds)
    decisions = {case.case_id: engine.decide(case) for case in ds.requests}
    return [(cid, d.status, d.steps_json) for cid, d in sorted(decisions.items())]


def validate_rows(ds: Dataset, rows: list[tuple[str, str, str]]) -> list[str]:
    """Every contract invariant, plus: a COMPLETE row must contain evidence-backed verification steps."""
    cases = {c.case_id: c for c in ds.requests}
    validator = Validator(ds)
    problems = Validator.check_rows(rows, [c.case_id for c in ds.requests])
    if len(rows) != 518:
        problems.append(f"expected 518 rows, got {len(rows)}")
    for case_id, status, steps_json in rows:
        problems += validator.check_row(case_id, status, steps_json, cases[case_id].audit_at)
        if status == "COMPLETE" and json.loads(steps_json) == []:
            problems.append(f"{case_id}: COMPLETE without any verification step")
    return problems


def write_output(path: Path, rows: list[tuple[str, str, str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(HEADER)
        writer.writerows(rows)


def read_output(path: Path) -> list[tuple[str, str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if header != HEADER:
            raise ValueError(f"bad header {header}")
        return [tuple(row) for row in reader]


def run(ds: Dataset, path: Path = DEFAULT_OUTPUT) -> tuple[int, list[str]]:
    """Build, validate, write and re-read the file. Returns (row count, problems); writes nothing if validation fails."""
    rows = build_rows(ds)
    problems = validate_rows(ds, rows)
    if problems:
        return len(rows), problems
    write_output(path, rows)
    if read_output(path) != rows:
        problems.append("the written file does not round-trip to the validated rows")
    return len(rows), problems
