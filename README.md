# Ghost Data Still Exists - deletion-audit agent

My solution to **HackerRank Orchestrate, October 2026**. The challenge: a deletion workflow says "Complete", but personal
data may still sit in replicas, logs, search indexes, exports, support attachments and backups. For each request the agent
has to decide whether deletion was actually requested, work out what every system held at the audit time, and return
`COMPLETE`, `INCOMPLETE` or `NOT_APPLICABLE` with the ordered findings, remediation and verification steps.

**Result: ranked 832 of 4,094 participants (top 21%).**

The original challenge brief is in [`problem_statement.md`](./problem_statement.md) and HackerRank's starter README is kept as
[`HACKERRANK_README.md`](./HACKERRANK_README.md). The dataset in `dataset/` is HackerRank's and is entirely fictional.

## Approach in one paragraph

A deterministic Python pipeline with **no model calls, no network access and no API keys**. It parses every evidence file
(SQL snapshots, JSON, logs, text, PDFs, screenshots) into facts with provenance and two timestamps, ties a resource to a person
only through exact identifiers or explicit link events, reconstructs each resource's state at `audit_at`, applies the deletion
rules as data, and only answers `COMPLETE` when a nine-part evidence gate passes. When the evidence cannot prove an answer, it
says `INCOMPLETE` rather than guess.

## Run it

```bash
python -m pip install -r code/requirements.txt
python code/main.py
```

This reads `dataset/`, decides all 518 requests, validates every output rule and writes `output.csv` (about 30-60 seconds).
The result is reproducible byte for byte. Python 3.11; the only dependency is `pymupdf`.

```bash
python -m unittest discover -s code/tests -t code     # 517 tests
python code/main.py evaluate                          # score against the 32 public samples
python code/main.py decide --case case_05             # explain one decision: gates, evidence, steps
```

## Results

Measured on the 32 public samples (the only labelled data):

| Measure | Result |
|---|---|
| Status accuracy | 23 / 32 |
| Step precision | 99.2% |
| Step recall | 69% |
| `system_id` and `state` on matched steps | 100% |
| Output-contract violations | 0 |

All nine status errors go the same way: the reference says `COMPLETE` and the agent says `INCOMPLETE`, because no account
record could be tied to the person by evidence available at the audit time. The full numbers, the alternatives that were
tested and rejected, custom test cases and known limitations are in
[`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md).

## What is in this repository

| Path | What it is |
|---|---|
| [`code/`](./code) | The solution. [`code/README.md`](./code/README.md) explains each stage and the layout. |
| [`code/tests/`](./code/tests) | 517 automated tests, including output-contract, anti-hardcoding and privacy checks. |
| [`code/evaluation/`](./code/evaluation) | Scoring, experiments, the evaluation report and the usage report (zero model calls). |
| [`output.csv`](./output.csv) | The submitted answers for the 518 requests. |
| [`log.txt`](./log.txt) | The development log the challenge required: every prompt and what was done in response. |
| [`dataset/`](./dataset) | HackerRank's challenge data (fictional), needed to run the code. |

Line references such as `README.md:59` inside the evaluation report and code comments point to HackerRank's README, which is
now [`HACKERRANK_README.md`](./HACKERRANK_README.md).

## Honest limitations

* The agent is strict by design, so it under-reports `COMPLETE`: it will not claim completion it cannot prove.
* Eleven `INCOMPLETE` answers have no steps, because nothing could be tied to the person by the evidence.
* About 18% of the steps in the sample answers name a resource that cannot be linked to the person without guessing from
  naming patterns or timestamps, which this solution refuses to do.
* Screenshots are read with the Windows OCR engine; the answers are identical without it.

## Credits

Challenge, dataset and starter repository by [HackerRank](https://github.com/interviewstreet/hackerrank-orchestrate-october26).
Built with Claude Code as the coding agent; the full working log is in `log.txt`.
