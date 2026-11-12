# Usage report

Summary of the final full-dataset run that produced `output.csv`.

## The run

| Item | Value |
|---|---|
| Command | `python code/main.py` (from the repository root, no `GHOST_OCR_CACHE`, cold run) |
| Date | 2026-10-25 |
| Requests processed | 518 (`dataset/deletion_requests.csv`) |
| Python | 3.11.0, `pymupdf` 1.28.2 |
| Wall-clock time | about 31 seconds |
| Statuses | COMPLETE 51, INCOMPLETE 457, NOT_APPLICABLE 10 |
| `output.csv` SHA-256 | `f8f382d280ebdaf77ce62303b19785f01bf61b4bffc59075f5e0716857cf2bff` |

The same hash was produced by a second cold run and by a run with a populated OCR cache.

## Model usage

The pipeline is deterministic rules code. It does not call any language model, embedding model, vision model or
other paid API, and it makes no network request.

| Model provider | Model | Calls | Input tokens | Output tokens | Total tokens | Estimated cost (USD) |
|---|---|---|---|---|---|---|
| none | none | 0 | 0 | 0 | 0 | 0.00 |
| **Overall** | | **0** | **0** | **0** | **0** | **0.00** |

| Per-request figure | Value |
|---|---|
| Total tokens per request (518 requests) | 0 |
| Average tokens per request | 0 |
| Estimated total cost | USD 0.00 |
| Estimated cost per request | USD 0.00 |

## Notes

* Screenshots (11 PNG files) are read locally with the Windows built-in OCR engine through PowerShell. It is an operating-system
  component, not a model service: it consumes no tokens and has no per-call cost. The output is identical when it is unavailable
  (`code/tests/test_ocr_independence.py`).
* No API key, token or credential is read, required or stored. The only optional environment variable is `GHOST_OCR_CACHE`, a
  directory path for caching raw OCR output.
* This report covers the run that produced `output.csv` only. It does not include the coding agent (Claude Code) used to write
  and test the system; that session's transcript is `log.txt`, and its token usage was not measured here, so none is claimed.
