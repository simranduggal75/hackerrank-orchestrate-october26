"""Experimental alternatives to gate G1 (account established and proven absent). NOT used by any decision.

``ExperimentEngine`` is a ``DecisionEngine`` whose only difference is one post-processing step: for a case whose
sole obstacle to ``COMPLETE`` is G1, it may waive G1 for specific evidence situations (``evaluation.diagnostics``).
Everything else is production code, so the baseline variant reproduces production decisions exactly (tested).

Variants (each is a reading of "every relevant copy", problem_statement.md lines 24-29, not a new rule):

  baseline        production: an account record must be linked to the subject and proven absent
  empty_store     REJECTED after review (evaluation report, "empty_store review"): waive G1 when a firm, in-time file that holds
                  the subject's own records reports an account collection with 0 rows
  store_absence   ... or a non-empty account collection (a point-in-time snapshot) with no row for the subject
  closed_world    ... or only per-resource lookups, or no account evidence at all in the files that hold the subject's records
                  (absence of any identifier-bearing account record, relying on every FOUND profile carrying identifiers)
  record_time     contract-EXCLUDED reference point: learn ownership from files captured after audit_at (AGENTS.md line 298)

The late-file and account-owned causes are never waived, so strict audit-time constraints are unchanged.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass

from assessment import GateResult
from engine import DecisionEngine
from evaluation import diagnostics, scoring
from evidence.store import extract_all
from footprint import Footprint
from intent import classify_intent
from loaders import DATASET_DIR, Dataset
from validate_output import Validator


@dataclass(frozen=True)
class Variant:
    name: str
    waive: frozenset[str]
    link_policy: str = "strict"
    contract_supported: bool = True


VARIANTS = {v.name: v for v in (
    Variant("baseline", frozenset()),
    Variant("empty_store", frozenset({diagnostics.EMPTY_STORE})),
    Variant("store_absence", frozenset({diagnostics.EMPTY_STORE, diagnostics.STORE_WITHOUT_SUBJECT})),
    Variant("closed_world", frozenset({diagnostics.EMPTY_STORE, diagnostics.STORE_WITHOUT_SUBJECT, diagnostics.LOOKUP_ONLY,
                                       diagnostics.NO_EVIDENCE})),
    Variant("record_time", frozenset(), link_policy="record_time", contract_supported=False),
)}


def waive_g1(assessment, footprint, store, waive: frozenset[str]) -> bool:
    """Waive G1 when its failure cause is in ``waive`` and at least one owned resource can be verified with a citation. Only an
    UNRESOLVED case can become COMPLETE: a proven violation, a non-authorized request and any other failed gate (including
    G8's late-file signal) are untouched."""
    if not waive or assessment.assessment != "UNRESOLVED":
        return False
    if diagnostics.g1_cause(assessment, footprint, store) not in waive:
        return False
    # A COMPLETE answer contains only verification steps that cite the evidence proving the state (problem_statement.md:91,
    # AGENTS.md:314). With nothing owned to verify, the answer would be an empty array with no proof, so G1 is not waived.
    if not any(v.kind == "COMPLIANT" and v.verify and v.compliant_evidence for v in assessment.verdicts):
        return False
    assessment.gates = tuple(GateResult("G1", True, ()) if g.gate == "G1" else g for g in assessment.gates)
    if assessment.failed_gates:
        return False
    assessment.assessment = "PROVEN_COMPLETE"
    return True


class ExperimentEngine(DecisionEngine):
    def __init__(self, ds: Dataset, store=None, variant: str = "baseline"):
        super().__init__(ds, store)
        self.variant = VARIANTS[variant]

    def assess(self, case):
        intent = classify_intent(case.request_text)
        resolution = self.identity.resolve_request(case)
        policy = self.variant.link_policy
        footprint = (self._footprints.build(resolution, policy) if intent.authorized
                     else Footprint(None, case.audit_at, policy))
        assessment = self._assessor.assess(case, resolution, footprint, intent)
        if waive_g1(assessment, footprint, self.store, self.variant.waive):
            assessment.notes.append("experiment: G1 waived (" + self.variant.name + ")")
        return assessment, footprint


# ------------------------------------------------------------------------------------- measurement


def _reference() -> dict:
    with open(DATASET_DIR / "sample_output.csv", encoding="utf-8", newline="") as handle:
        return scoring.load_reference(list(csv.DictReader(handle)))


def _decide(engine: ExperimentEngine, cases) -> dict:
    return {c.case_id: engine.decide(c) for c in cases}


def run(ds: Dataset) -> dict:
    store = extract_all(ds.evidence_files)
    reference = _reference()
    cases = {c.case_id: c for c in ds.requests + ds.sample_requests}
    validator = Validator(ds)
    results: dict[str, dict] = {}
    baseline_eval: dict = {}
    for name in VARIANTS:
        engine = ExperimentEngine(ds, store, name)
        samples = _decide(engine, ds.sample_requests)
        evals = _decide(engine, ds.requests)
        predicted = {cid: (d.status, d.steps) for cid, d in samples.items()}
        metrics = scoring.score(reference, predicted)
        problems = [p for cid, d in {**samples, **evals}.items()
                    for p in validator.check_row(cid, d.status, d.steps_json, cases[cid].audit_at)]
        false_complete = [cid for cid, (st, _) in reference.items() if st != "COMPLETE" and samples[cid].status == "COMPLETE"]
        missed_complete = [cid for cid, (st, _) in reference.items() if st == "COMPLETE" and samples[cid].status != "COMPLETE"]
        account_missing = other_missing = 0
        for cid, (st, steps) in reference.items():
            if st == "COMPLETE" and samples[cid].status == "COMPLETE":
                got = {scoring.identity(s) for s in samples[cid].steps}
                for s in steps:
                    if scoring.identity(s) not in got:
                        if s["system_id"] == "account_service":
                            account_missing += 1
                        else:
                            other_missing += 1
        if name == "baseline":
            baseline_eval = evals
        flips = [cid for cid, d in evals.items() if d.status != baseline_eval[cid].status]
        flipped = [evals[cid] for cid in flips]
        results[name] = {
            "variant": VARIANTS[name],
            "samples": metrics, "sample_status": {cid: d.status for cid, d in samples.items()}, "false_complete": false_complete, "missed_complete": missed_complete,
            "complete_steps_missing": {"account": account_missing, "other": other_missing},
            "invariant_violations": len(problems),
            "eval_status": dict(sorted(Counter(d.status for d in evals.values()).items())),
            "eval_complete": sum(d.status == "COMPLETE" for d in evals.values()),
            "complete_with_no_steps": sum(d.status == "COMPLETE" and not d.steps for d in {**samples, **evals}.values()),
            "eval_flips": len(flips),
            "eval_flips_with_no_steps": sum(not d.steps for d in flipped),
            "eval_flips_with_account_step": sum(any(s["system_id"] == "account_service" for s in d.steps) for d in flipped),
            "eval_flips_mean_steps": (sum(len(d.steps) for d in flipped) / len(flipped)) if flipped else 0.0,
        }
    base = ExperimentEngine(ds, store, "baseline")
    results["nine"] = {}
    for cid, (status, _) in sorted(reference.items()):
        case = cases[cid]
        a, fp = base.assess(case)
        if status == "COMPLETE" and a.status != "COMPLETE":
            results["nine"][cid] = {"cause": diagnostics.g1_cause(a, fp, store),
                                    "flips_under": [n for n in VARIANTS if n != "baseline" and results[n]["sample_status"][cid] == "COMPLETE"]}
    results["temporal_window"] = temporal_window(ds, reference)
    results["finding_reachability"] = finding_reachability(base, ds, reference)
    results["empty_store_review"] = empty_store_review(ds, store)
    return results


def finding_reachability(engine: ExperimentEngine, ds: Dataset, reference: dict) -> dict:
    """Validation only (uses sample labels): in reference-INCOMPLETE samples, is every finding's resource one that strict
    ownership reaches? If so, a violation hidden from the footprint has not been observed, which is what a G1 waiver relies on."""
    cases = {c.case_id: c for c in ds.sample_requests}
    incomplete = all_owned = findings = owned_findings = 0
    for cid, (status, steps) in reference.items():
        if status != "INCOMPLETE":
            continue
        footprint = engine.assess(cases[cid])[1]
        resources = {s["resource_id"] for s in steps if s["phase"] == "FINDING"}
        incomplete += 1
        findings += len(resources)
        owned_findings += len(resources & set(footprint.resources))
        all_owned += resources <= set(footprint.resources)
    return {"incomplete_cases": incomplete, "cases_with_every_finding_owned": all_owned,
            "finding_resources": findings, "finding_resources_owned": owned_findings}


def temporal_window(ds: Dataset, reference: dict) -> dict:
    """Diagnostic for linking an account by time (the contract names timestamps among the grounded links).

    Candidates are account-service DELETE_RECORD activity rows between ``requested_at`` and ``reported_at``. A link is
    useful only if the candidate is unique. Reported: the candidate-count distribution, how often it is unique, and
    (samples only, for validation) how often the unique candidate is in fact the account the reference verifies.
    """
    deletes = [a for a in ds.activity if a.event_type == "DELETE_RECORD" and a.target_system == "account_service"]

    def candidates(case):
        return sorted({a.resource_id for a in deletes if case.requested_at <= a.event_time <= case.reported_at})

    def spread(cases):
        sizes = Counter(min(len(candidates(c)), 6) for c in cases)
        return {("6+" if k == 6 else str(k)): v for k, v in sorted(sizes.items())}

    unique_correct = unique_total = 0
    for case in ds.sample_requests:
        found = candidates(case)
        if len(found) == 1:
            unique_total += 1
            truth = {s["resource_id"] for s in reference[case.case_id][1] if s["system_id"] == "account_service"}
            unique_correct += found[0] in truth
    return {"sample_candidate_counts": spread(ds.sample_requests), "eval_candidate_counts": spread(ds.requests),
            "sample_unique": unique_total, "sample_unique_correct": unique_correct}


# ------------------------------------------------------------------------------ empty_store review


def empty_store_review(ds: Dataset, store) -> dict:
    """Measured facts behind the review of ``empty_store`` (see the evaluation report). Read-only."""
    import json
    from datetime import datetime

    from loaders import DATASET_DIR as root

    def hours(a: str, b: str) -> float:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return abs((datetime.strptime(a, fmt) - datetime.strptime(b, fmt)).total_seconds()) / 3600

    collections = [cv for r in store.results.values() for cv in r.coverage
                   if cv.record_type == "USER_PROFILE" and not diagnostics.is_lookup_store(cv.store)]
    empties = [cv for cv in collections if cv.row_count == 0]
    snapshot_keys, repeats = set(), 0
    for entry in ds.evidence_files:
        if not entry.file_path.endswith(".json"):
            continue
        doc = json.loads((root / entry.file_path).read_text(encoding="utf-8"))
        if isinstance(doc, dict) and "snapshots" in doc:
            snapshot_keys |= {tuple(sorted(k for k in s if k != "records")) for s in doc["snapshots"]}
            seen = {}
            for s in doc["snapshots"]:
                seen.setdefault((s["data_store"], s["collection"]), set()).add(len(s["records"]))
            repeats += sum(len(v) > 1 for v in seen.values())
    base = ExperimentEngine(ds, store, "baseline")
    variant = ExperimentEngine(ds, store, "empty_store")
    pairs = corroborated = 0
    seen_users: set = set()
    flipped_ops: Counter = Counter()
    flipped = flipped_with_account_step = 0
    account_files = {cv.evidence_id: cv for cv in collections}
    for case in ds.requests + ds.sample_requests:
        before, footprint = base.assess(case)
        if footprint.user_id not in seen_users:
            seen_users.add(footprint.user_id)
            in_file = {o.evidence_id for r in footprint.resources.values() for o in r.state.observations}
            for fid in in_file & set(account_files):
                pairs += 1
                near = [o for r in footprint.resources.values() if r.state.record_type == "USER_PROFILE" for o in r.state.observations
                        if o.evidence_id != fid and not o.provisional and hours(o.observed_at, account_files[fid].observed_at) <= 48]
                corroborated += bool(near)
        if before.assessment == "UNRESOLVED" and variant.decide(case).assessment.assessment == "PROVEN_COMPLETE":
            flipped += 1
            steps = variant.decide(case).steps
            flipped_ops.update(s["operation"] for s in steps)
            flipped_with_account_step += any(s["system_id"] == "account_service" for s in steps)
    return {"account_collections": len(collections), "empty_collections": len(empties),
            "empty_files": sorted({cv.evidence_id for cv in empties}),
            "max_rows_in_a_collection": max((cv.row_count for cv in collections), default=0), "identities": len(ds.identities),
            "snapshot_key_sets": sorted(snapshot_keys), "stores_with_varying_counts_in_one_bundle": repeats,
            "subject_file_pairs": pairs, "pairs_with_independent_account_evidence": corroborated,
            "flipped": flipped, "flipped_ops": dict(sorted(flipped_ops.items())), "flipped_with_account_step": flipped_with_account_step}
