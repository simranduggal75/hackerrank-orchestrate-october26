# Ghost Data Still Exists - deletion-audit agent

A deterministic Python program that audits every request in `dataset/deletion_requests.csv` and writes `output.csv`
(`case_id,deletion_status,steps_json`). It decides whether the person asked for deletion, reconstructs what each system
held at `audit_at` from the supplied evidence, applies `deletion_rules.csv`, and returns `COMPLETE`, `INCOMPLETE` or
`NOT_APPLICABLE` with the ordered findings, remediation and verification steps.

It makes **no model, network or paid-API call** and needs **no API key or secret**. Output is reproducible byte for byte.

## Requirements

* Python 3.11 (developed and tested on 3.11.0).
* `pymupdf` for reading PDF evidence: `python -m pip install -r code/requirements.txt`.
* Optional: Windows with PowerShell, used to read the 11 screenshots with the built-in Windows OCR engine
  (`code/evidence/ocr_windows.ps1`). Screenshot facts are provisional and the submission is identical without them
  (tested: `tests/test_ocr_independence.py`).

## Run (from the repository root)

```bash
python -m pip install -r code/requirements.txt
python code/main.py
```

This reads `dataset/`, decides all 518 requests, validates every output invariant, and writes `output.csv` in the
repository root (about 30 seconds). Nothing is written if validation fails. Use `--output PATH` to write elsewhere.

Other commands (all read-only except where stated):

| Command | Purpose |
|---|---|
| `python code/main.py check` | load and validate the dataset, resolve identities |
| `python code/main.py evidence` | run every evidence parser, print coverage and issues |
| `python code/main.py footprint --case case_05` | resources tied to a subject and their state at `audit_at` |
| `python code/main.py decide [--case case_05]` | assessment, gates and planned steps; never writes `output.csv` |
| `python code/main.py evaluate [--write-report]` | score the 32 public samples; `--write-report` rewrites `code/evaluation/evaluation_report.md` |
| `python code/main.py package` | build `code.zip` (code, README, reports; no dataset, caches or secrets) |

Tests: `python -m unittest discover -s code/tests -t code` (tests that need `dataset/` skip when it is absent).

Test data is synthetic: fixtures use placeholder addresses (`@example.test`) and invented names and phone numbers. The few tests that
check the real screenshots compare SHA-256 digests of the expected text instead of storing it, and tests that need a real case
look it up by id in `dataset/`. `tests/test_privacy.py` fails if any shipped file contains an email, name or phone number from the dataset.

Optional environment variable: `GHOST_OCR_CACHE` - a directory outside the repository where raw OCR output is cached by image
hash. It only saves time; results do not depend on it.

## How it decides

1. **Intent** (`intent.py`): the request wording alone decides whether deletion is authorized (multilingual lexicon;
   negation, keep-the-account and marketing-only limiters take precedence). Non-deletion requests are `NOT_APPLICABLE` with `[]`.
2. **Identity** (`identity.py`): current and historical emails, hashes and phones, valid at `audit_at`.
3. **Evidence** (`evidence/`): SQL snapshots (executed with SQLite), JSON, logs, text, PDFs and OCR'd screenshots become
   normalised facts with provenance and two timestamps. A fact is usable only if its file was captured **and** its record observed
   at or before `audit_at`.
4. **Footprint** (`footprint.py`): a resource belongs to a person only through an exact identifier, an explicit link event, a
   source-resource reference or an alias. Never by naming pattern, timestamp proximity or activity adjacency.
5. **State** (`state.py`): the newest firm observation per store, replica drift, anchored table-dump absence; activity rows are
   claims and never establish state; screenshots are provisional.
6. **Policy and assessment** (`policy.py`, `assessment.py`): the rules as data, rule conditions evaluated from evidence, and a
   completion gate G0-G8. Four internal assessments are kept distinct: `NOT_AUTHORIZED`, `PROVEN_VIOLATION`, `UNRESOLVED`
   (both map to `INCOMPLETE`) and `PROVEN_COMPLETE`. `COMPLETE` needs every gate, including an account record proven absent (G1).
7. **Planner** (`planner.py`): contract order (phase, operation, `system_id`, `resource_id`), finding citations, empty
   `evidence_id` for all remediation and post-remediation checks of an `INCOMPLETE` case, cited verification for `COMPLETE`.
8. **Validation** (`validate_output.py`, `submission.py`): header, 518 rows, sort order, statuses, JSON, step schema,
   operation/state pairs, phase order, consecutive numbering, and every cited id exists and is not after `audit_at`.

## Results and limitations

Measured results, custom cases, failures found and known limitations are in `code/evaluation/evaluation_report.md`
(generated from the run); the run's usage is in `code/evaluation/usage_report.md`. In brief: on the 32 public samples the
engine gets 23/32 statuses, 99% step precision and 69% step recall with no invariant violations. All nine status errors are
reference-`COMPLETE` cases predicted `INCOMPLETE`, because no account record can be tied to the person by evidence available at
`audit_at`; the engine reports `INCOMPLETE` rather than claim completion it cannot prove. Eleven `INCOMPLETE` rows have an
empty `steps_json` for the same reason. Rows are sorted by `case_id` as plain strings.

## Layout

```
code/main.py          entry point            code/submission.py   build, validate, write output.csv
code/loaders.py       dataset loading        code/validate_output.py  output invariants
code/identity.py      identity resolution    code/policy.py       rules as data
code/evidence/        parsers + OCR          code/assessment.py   verdicts and gates
code/activity.py      activity claims/links  code/planner.py      steps and ordering
code/footprint.py     ownership              code/engine.py       request -> decision
code/state.py         state at audit_at      code/intent.py       authorization wording
code/evaluation/      scoring, experiments, reports, custom cases
code/tests/           automated tests
```
