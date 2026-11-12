"""Ghost Data Still Exists - deletion-audit agent entry point.

Usage (from the repository root):
    python code/main.py [run] [--output PATH]
                                  # DEFAULT: decide all 518 requests, validate, and write output.csv (repository root)
    python code/main.py package   # build code.zip (code, README, requirements, reports; no dataset/caches/secrets)
    python code/main.py check     # load + validate the dataset, resolve identities (read-only)
    python code/main.py evidence  # run every evidence parser and print coverage + issues (read-only)
    python code/main.py footprint [--case case_05] [--policy strict|record_time]
                                  # per-subject resource footprint and state at audit_at (read-only)
    python code/main.py decide [--case case_05]
                                  # assessment, gates and planned steps (read-only; does not write output.csv)
    python code/main.py evaluate [--write-report]
                                  # score the decisions on the 32 public samples; --write-report rewrites
                                  # code/evaluation/evaluation_report.md

No model or network call is made. Screenshots are read with the Windows OCR engine (see evidence/ocr.py); set
GHOST_OCR_CACHE to a directory outside the repository to reuse raw OCR output between runs (optional).
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from activity import ActivityIndex
from evidence.store import extract_all
from footprint import POLICIES, FootprintBuilder
from identity import IdentityIndex, normalize_email
from loaders import SchemaError, load_dataset


def run_check() -> int:
    try:
        ds = load_dataset()
    except SchemaError as exc:
        print(f"SCHEMA ERROR: {exc}")
        return 1

    print("Dataset loaded (UTF-8, schema validated):")
    for name in ("requests", "sample_requests", "identities", "audit_log", "activity", "rules", "evidence_files"):
        print(f"  {name:<16}{len(getattr(ds, name)):>6} rows")
    for issue in ds.issues:
        print(f"  [{issue.level}] {issue.message}")
    if not ds.issues:
        print("  no cross-file issues")

    index = IdentityIndex(ds.identities, ds.audit_log)
    print(f"\nIdentity index: {len(index.users)} users, {len(index.issues)} consistency issue(s)")
    for issue in index.issues:
        print(f"  [WARN] {issue}")

    for label, requests in (("evaluation", ds.requests), ("sample", ds.sample_requests)):
        results = [index.resolve_request(r) for r in requests]
        print(f"\nResolution of {len(results)} {label} requests: {dict(Counter(r.status for r in results))}")
        noted = Counter(n.split(" by ")[0].split(" at ")[0] for r in results for n in r.notes)
        for note, count in sorted(noted.items()):
            print(f"  note x{count}: {note}")
        for r in results:
            if r.status not in ("CURRENT", "HISTORICAL"):
                print(f"  UNRESOLVED {r.case_id} {r.request_email} -> {r.status} {r.notes}")
        by_email = Counter(normalize_email(r.email) for r in requests)
        for email, count in by_email.items():
            if count > 1:
                cases = [r.case_id for r in requests if normalize_email(r.email) == email]
                print(f"  repeated email in {cases}: resolved separately per case")
    return 0 if not any(i.level == "ERROR" for i in ds.issues) else 1


def run_evidence() -> int:
    try:
        ds = load_dataset()
    except SchemaError as exc:
        print(f"SCHEMA ERROR: {exc}")
        return 1
    store = extract_all(ds.evidence_files)
    print("\n".join(store.summary()))
    shown = [i for i in store.issues if i.level in ("ERROR", "WARN")]
    print(f"\n{len(shown)} warning/error issue(s) (INFO issues omitted):")
    for issue in shown:
        print(f"  [{issue.level}] {issue.code} {issue.evidence_id} {issue.locator}: {issue.message}")
    return 1 if any(i.level == "ERROR" for i in store.issues) else 0


def _print_footprint(case, fp) -> None:
    print(f"\n{case.case_id} {case.email} -> {fp.user_id}  audit_at={fp.as_of}  link_policy={fp.link_policy}")
    for rid, owned in sorted(fp.resources.items()):
        s = owned.state
        flags = [f for f, on in (("provisional", s.provisional), ("shared", owned.shared), ("exclusive", owned.exclusive)) if on]
        via = ",".join(sorted({link.via for link in owned.links}))
        print(f"  {rid:<20}{(s.record_type or '?'):<20}{s.state:<22}{'absent:' + s.absence if s.absence else '':<18}"
              f"roles={'/'.join(sorted(owned.roles))} via={via} {' '.join(flags)}")
        for conflict in s.conflicts:
            print(f"      conflict: {conflict}")
    for fact in fp.unlinked_facts:
        print(f"  (no resource id) {fact.record_type} in {fact.evidence_id}: {fact.provenance.locator}")
    for item in fp.unresolved:
        print(f"  ? {item.kind} {item.resource_id or ''}: {item.detail}")


def run_footprint(case_id: str | None, policy: str | None) -> int:
    try:
        ds = load_dataset()
    except SchemaError as exc:
        print(f"SCHEMA ERROR: {exc}")
        return 1
    identity = IdentityIndex(ds.identities, ds.audit_log)
    store = extract_all(ds.evidence_files)
    activity = ActivityIndex(ds.activity)
    builder = FootprintBuilder(store, identity, activity)
    for problem in activity.problems:
        print(f"[WARN] {problem}")
    cases = ds.requests + ds.sample_requests
    if case_id:
        case = next((c for c in cases if c.case_id == case_id), None)
        if case is None:
            print(f"unknown case {case_id}")
            return 2
        for pol in ([policy] if policy else POLICIES):
            _print_footprint(case, builder.build(identity.resolve_request(case), pol))
        return 0
    for pol in ([policy] if policy else POLICIES):
        prints = [(c, builder.build(identity.resolve_request(c), pol)) for c in cases]
        states = Counter((r.state.record_type, r.state.state) for _, fp in prints for r in fp.resources.values())
        kinds = Counter(u.kind for _, fp in prints for u in fp.unresolved)
        with_account = sum(1 for _, fp in prints if any(r.state.record_type == "USER_PROFILE" for r in fp.resources.values()))
        print(f"\n== link_policy={pol}: {len(prints)} cases, {sum(len(fp.resources) for _, fp in prints)} owned resources, "
              f"{with_account} cases with an account resource, {sum(1 for _, fp in prints if not fp.resources)} cases with none")
        print("   unresolved:", dict(sorted(kinds.items())))
        print("   states:", ", ".join(f"{t}/{s}={n}" for (t, s), n in sorted(states.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))))
    return 0


def run_decide(case_id: str | None) -> int:
    """Decide cases and print the result. Never writes output.csv."""
    from engine import DecisionEngine
    from validate_output import Validator
    try:
        ds = load_dataset()
    except SchemaError as exc:
        print(f"SCHEMA ERROR: {exc}")
        return 1
    engine = DecisionEngine(ds)
    validator = Validator(ds)
    cases = ds.requests + ds.sample_requests
    if case_id:
        case = next((c for c in cases if c.case_id == case_id), None)
        if case is None:
            print(f"unknown case {case_id}")
            return 2
        decision = engine.decide(case)
        a = decision.assessment
        print(f"{case.case_id} {case.email}  audit_at={case.audit_at}\n  assessment={a.assessment} -> {decision.status}")
        for gate in a.gates:
            why = "; ".join(f"{r.code}({r.resource_id or '-'})" for r in gate.reasons)
            print(f"  gate {gate.gate}: {'pass' if gate.passed else 'FAIL'} {why}")
        for note in a.notes:
            print(f"  note: {note}")
        for step in decision.planned:
            extra = f" [{step.check_class}; current state shown by {step.supporting_evidence}]" if step.check_class else ""
            print(f"  {step.phase:<12}{step.operation:<24}{step.system_id:<26}{step.resource_id:<22}{step.state:<11}{step.evidence_id or '-'}{extra}")
        problems = validator.check_row(case.case_id, decision.status, decision.steps_json, case.audit_at)
        print("  invariants:", "ok" if not problems else problems)
        return 0
    decisions = [(c, engine.decide(c)) for c in cases]
    print("assessments:", dict(sorted(Counter(d.assessment.assessment for _, d in decisions).items())))
    print("statuses:   ", dict(sorted(Counter(d.status for _, d in decisions).items())))
    problems = [p for c, d in decisions for p in validator.check_row(c.case_id, d.status, d.steps_json, c.audit_at)]
    print(f"invariant violations: {len(problems)}")
    for problem in problems[:20]:
        print("  ", problem)
    return 0 if not problems else 1


def run_submission(output: str | None) -> int:
    """Decide all 518 evaluation requests with the production engine, validate, and write output.csv (repository root)."""
    from pathlib import Path

    import submission
    try:
        ds = load_dataset()
    except SchemaError as exc:
        print(f"SCHEMA ERROR: {exc}")
        return 1
    path = Path(output) if output else submission.DEFAULT_OUTPUT
    count, problems = submission.run(ds, path)
    if problems:
        print(f"NOT WRITTEN: {len(problems)} problem(s)")
        for problem in problems[:20]:
            print("  ", problem)
        return 1
    rows = submission.read_output(path)
    print(f"wrote {count} rows to {path}")
    print("statuses:", dict(sorted(Counter(r[1] for r in rows).items())))
    return 0


def run_package() -> int:
    """Build code.zip (repository root) from the code, README, requirements and reports; refuse on a missing file or secret."""
    import packaging
    problems = packaging.build()
    if problems:
        print("NOT BUILT / FAILED INSPECTION:")
        for problem in problems:
            print("  ", problem)
        return 1
    import zipfile
    with zipfile.ZipFile(packaging.DEFAULT_ARCHIVE) as zf:
        print(f"wrote {packaging.DEFAULT_ARCHIVE} ({len(zf.namelist())} files, inspection clean)")
    return 0


def run_evaluate(write_report: bool) -> int:
    """Score the engine on the public samples; optionally regenerate the evaluation report. Read-only otherwise."""
    from evaluation.report import render, summary_lines
    from evaluation.samples import evaluate_samples
    try:
        ds = load_dataset()
    except SchemaError as exc:
        print(f"SCHEMA ERROR: {exc}")
        return 1
    result = evaluate_samples(ds)
    print("\n".join(summary_lines(result)))
    if write_report:
        from pathlib import Path
        path = Path(__file__).resolve().parent / "evaluation" / "evaluation_report.md"
        path.write_text(render(result, ds), encoding="utf-8", newline="\n")
        print(f"report written to {path}")
    return 0 if not result["problems"] else 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252; text is multilingual
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["run", "package", "check", "evidence", "footprint", "decide", "evaluate"],
                        help="run (default): decide all requests and write output.csv; check: validate data; evidence: run evidence parsers; footprint: ownership and state; "
                             "decide: assess and plan (never writes output.csv); evaluate: score on the public samples")
    parser.add_argument("--case", help="footprint/decide: show one case in detail")
    parser.add_argument("--policy", choices=POLICIES, help="footprint: link policy (default: both)")
    parser.add_argument("--output", help="run: output path (default: output.csv in the repository root)")
    parser.add_argument("--write-report", action="store_true", help="evaluate: rewrite code/evaluation/evaluation_report.md")
    args = parser.parse_args(argv)
    if args.command in (None, "run"):
        return run_submission(args.output)
    if args.command == "package":
        return run_package()
    if args.command == "check":
        return run_check()
    if args.command == "evidence":
        return run_evidence()
    if args.command == "footprint":
        return run_footprint(args.case, args.policy)
    if args.command == "decide":
        return run_decide(args.case)
    if args.command == "evaluate":
        return run_evaluate(args.write_report)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
