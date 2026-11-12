"""Scoring of predicted output rows against reference rows (the public samples).

Metrics (all micro-averaged over the scored cases unless noted):

* status accuracy and confusion matrix;
* step-level: identity = (phase, system_id, resource_id, operation, state); precision, recall, F1. A second,
  stricter pass also requires the same evidence_id;
* field-level: predicted and reference steps are aligned by (phase, resource_id, operation, system_id when
  there are several) and each field is compared (system_id, state, evidence_id, step_no). Reported on the
  aligned steps and strictly over all reference steps (an unaligned reference step counts as a miss);
* sequence: exact ordered match of the step identities per case, normalised LCS, and pairwise order
  agreement among the steps both outputs contain;
* citation accuracy: over reference steps that carry a citation, and agreement on the empty-citation steps.

Misses are triaged by cause class, never case by case, so a low score caused by evidence that cannot reach
a resource (the linking ceiling) is not mistaken for a rule error.
"""

from __future__ import annotations

import json
from collections import Counter
from itertools import combinations

FIELDS = ("system_id", "state", "evidence_id", "step_no")


def identity(step: dict) -> tuple:
    return (step["phase"], step["system_id"], step["resource_id"], step["operation"], step["state"])


def full(step: dict) -> tuple:
    return identity(step) + (step["evidence_id"],)


def _lcs(a: list, b: list) -> int:
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a, 1):
        for j, y in enumerate(b, 1):
            table[i][j] = table[i - 1][j - 1] + 1 if x == y else max(table[i - 1][j], table[i][j - 1])
    return table[-1][-1]


def _align(expected: list[dict], predicted: list[dict]) -> list[tuple[dict, dict]]:
    """Pair steps by (phase, resource_id, operation); prefer an equal system_id when several qualify."""
    pool = list(predicted)
    pairs = []
    for e in expected:
        candidates = [p for p in pool if (p["phase"], p["resource_id"], p["operation"]) == (e["phase"], e["resource_id"], e["operation"])]
        if not candidates:
            continue
        pick = next((p for p in candidates if p["system_id"] == e["system_id"]), candidates[0])
        pool.remove(pick)
        pairs.append((e, pick))
    return pairs


def score(reference: dict[str, tuple[str, list[dict]]], predicted: dict[str, tuple[str, list[dict]]]) -> dict:
    """reference/predicted: case_id -> (status, steps). Returns a plain dict of metrics."""
    cases = sorted(reference)
    confusion: Counter = Counter()
    tp = fp = fn = 0
    ftp = 0
    field_hits = Counter()
    field_total = Counter()
    aligned_total = ref_total = 0
    exact_sequences = 0
    lcs_sum = 0.0
    pair_agree = pair_total = 0
    cite_total = cite_hit = empty_total = empty_hit = 0
    for cid in cases:
        ref_status, ref_steps = reference[cid]
        pred_status, pred_steps = predicted.get(cid, ("<missing>", []))
        confusion[(ref_status, pred_status)] += 1
        ref_ids, pred_ids = [identity(s) for s in ref_steps], [identity(s) for s in pred_steps]
        rc, pc = Counter(ref_ids), Counter(pred_ids)
        hit = sum((rc & pc).values())
        tp, fp, fn = tp + hit, fp + sum(pc.values()) - hit, fn + sum(rc.values()) - hit
        ftp += sum((Counter(full(s) for s in ref_steps) & Counter(full(s) for s in pred_steps)).values())
        pairs = _align(ref_steps, pred_steps)
        aligned_total, ref_total = aligned_total + len(pairs), ref_total + len(ref_steps)
        for e, p in pairs:
            for f in FIELDS:
                field_total[f] += 1
                field_hits[f] += e[f] == p[f]
        exact_sequences += ref_ids == pred_ids
        lcs_sum += _lcs(ref_ids, pred_ids) / max(len(ref_ids), len(pred_ids), 1)
        common = [x for x in ref_ids if x in set(pred_ids)]
        position = {x: i for i, x in enumerate(pred_ids)}
        for a, b in combinations(common, 2):
            pair_total += 1
            pair_agree += position[a] < position[b]
        by_pred = {identity(s): s for s in pred_steps}
        for s in ref_steps:
            match = by_pred.get(identity(s))
            if s["evidence_id"]:
                cite_total += 1
                cite_hit += bool(match) and match["evidence_id"] == s["evidence_id"]
            else:
                empty_total += 1
                empty_hit += bool(match) and match["evidence_id"] == ""
    p, r = tp / (tp + fp) if tp + fp else 0.0, tp / (tp + fn) if tp + fn else 0.0
    ratio = lambda a, b: a / b if b else 0.0
    return {
        "cases": len(cases),
        "status_correct": sum(v for (a, b), v in confusion.items() if a == b),
        "status_accuracy": ratio(sum(v for (a, b), v in confusion.items() if a == b), len(cases)),
        "confusion": {f"{a} -> {b}": v for (a, b), v in sorted(confusion.items())},
        "steps": {"reference": tp + fn, "predicted": tp + fp, "matched": tp, "precision": p, "recall": r,
                  "f1": ratio(2 * p * r, p + r), "matched_with_citation": ftp, "recall_with_citation": ratio(ftp, tp + fn)},
        "fields": {"aligned": aligned_total, "reference": ref_total,
                   "aligned_accuracy": {f: ratio(field_hits[f], field_total[f]) for f in FIELDS},
                   "strict_accuracy": {f: ratio(field_hits[f], ref_total) for f in FIELDS}},
        "sequence": {"exact_case_sequences": exact_sequences, "exact_rate": ratio(exact_sequences, len(cases)),
                     "mean_normalised_lcs": ratio(lcs_sum, len(cases)), "pairwise_order_agreement": ratio(pair_agree, pair_total)},
        "citations": {"reference_cited": cite_total, "cited_correct": cite_hit, "accuracy": ratio(cite_hit, cite_total),
                      "reference_empty": empty_total, "empty_agreement": ratio(empty_hit, empty_total)},
    }


def triage(reference: dict[str, tuple[str, list[dict]]], predicted: dict[str, tuple[str, list[dict]]], explain) -> dict:
    """Count missed and extra steps by cause class. ``explain(case_id, step)`` names why a reference step is
    missing (the engine knows whether the resource was linked and what policy decided)."""
    missed: Counter = Counter()
    extra: Counter = Counter()
    for cid, (_, ref_steps) in sorted(reference.items()):
        pred_steps = predicted.get(cid, ("", []))[1]
        pred_ids = Counter(identity(s) for s in pred_steps)
        ref_ids = Counter(identity(s) for s in ref_steps)
        for s in ref_steps:
            if identity(s) not in pred_ids:
                missed[explain(cid, s, pred_steps)] += 1
        counterpart = {(r["phase"], r["resource_id"], r["operation"]) for r in ref_steps}
        for s in pred_steps:
            if identity(s) not in ref_ids:
                kind = ("same step, different system_id" if (s["phase"], s["resource_id"], s["operation"]) in counterpart
                        else "no counterpart in the reference")
                extra[f"{kind}: {s['phase']}/{s['operation']}"] += 1
    return {"missed_by_cause": dict(missed.most_common()), "extra_by_step": dict(extra.most_common())}


def load_reference(rows: list[dict]) -> dict[str, tuple[str, list[dict]]]:
    return {r["case_id"]: (r["deletion_status"], json.loads(r["steps_json"])) for r in rows}
