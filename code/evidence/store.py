"""Run every parser over the evidence manifest and expose time-filtered, exactly-matched facts."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from identity import SubjectIdentifiers
from loaders import DATASET_DIR, EvidenceFile

from . import image_parser, json_parser, log_parser, pdf_parser, sql_parser, txt_parser
from .facts import Coverage, Fact, ParseIssue, ParseResult

PARSERS = {
    ".sql": sql_parser.parse,
    ".json": json_parser.parse,
    ".log": log_parser.parse,
    ".txt": txt_parser.parse,
    ".pdf": pdf_parser.parse,
    ".png": image_parser.parse,
}

MATCH_KINDS = ("user_id", "email", "email_hash", "phone")


def extract_file(ev: EvidenceFile, dataset_dir: Path = DATASET_DIR) -> ParseResult:
    suffix = Path(ev.file_path).suffix.lower()
    path = dataset_dir / ev.file_path
    parser = PARSERS.get(suffix)
    if parser is None:
        result = ParseResult(ev.file_id, "none")
        result.issue("ERROR", "UNSUPPORTED_FORMAT", "file", f"no parser for {suffix or 'extensionless'} files")
        return result
    if not path.is_file():
        result = ParseResult(ev.file_id, suffix.lstrip("."))
        result.issue("ERROR", "MISSING_FILE", "file", f"{ev.file_path} not found")
        return result
    try:
        result = parser(path, ev)
    except Exception as exc:  # noqa: BLE001 - a parser bug must surface as an issue, not vanish
        result = ParseResult(ev.file_id, suffix.lstrip("."))
        result.issue("ERROR", "PARSER_CRASH", "file", f"{type(exc).__name__}: {exc}")
        return result
    _flag_late_facts(result, ev)
    return result


def _flag_late_facts(result: ParseResult, ev: EvidenceFile) -> None:
    """A record stamped after its own file's capture time is suspicious; say so once per record."""
    already = {i.locator for i in result.issues if i.code in ("LINE_AFTER_CAPTURE", "RECORD_AFTER_CAPTURE")}
    for fact in result.facts:
        if fact.observed_at > ev.captured_at and fact.provenance.locator not in already:
            result.issue("WARN", "RECORD_AFTER_CAPTURE", fact.provenance.locator,
                         f"record time {fact.observed_at} is after file capture {ev.captured_at}; "
                         "it is excluded for audits before that time")


@dataclass(frozen=True, slots=True)
class Match:
    fact: Fact
    matched: tuple[tuple[str, str], ...]  # (identifier field, kind) pairs that hit exactly


class EvidenceStore:
    def __init__(self, results: Iterable[ParseResult], manifest: Iterable[EvidenceFile]):
        self.manifest = {e.file_id: e for e in manifest}
        self.results = {r.evidence_id: r for r in results}
        self.facts: list[Fact] = [f for r in self.results.values() for f in r.facts]
        self.coverage: list[Coverage] = [c for r in self.results.values() for c in r.coverage]
        self.issues: list[ParseIssue] = [i for r in self.results.values() for i in r.issues]
        self._by_resource: dict[str, list[Fact]] = defaultdict(list)
        for fact in self.facts:
            if fact.resource_id:
                self._by_resource[fact.resource_id].append(fact)

    # ------------------------------------------------------------------ queries

    def available_facts(self, audit_at: str) -> list[Fact]:
        """Facts whose file AND record time are at or before ``audit_at``."""
        return [f for f in self.facts if f.available_by(audit_at)]

    def facts_for_resource(self, resource_id: str, audit_at: str) -> list[Fact]:
        return [f for f in self._by_resource.get(resource_id, []) if f.available_by(audit_at)]

    def facts_for_subject(self, subject: SubjectIdentifiers, audit_at: str | None = None) -> list[Match]:
        """Facts carrying an identifier that exactly equals one of the subject's identifiers."""
        cutoff = audit_at or subject.as_of
        hits = []
        for fact in self.facts:
            if not fact.available_by(cutoff):
                continue
            matched = tuple(
                (i.field, subject.match(i.value)) for i in fact.identifiers
                if i.kind in MATCH_KINDS and subject.match(i.value))
            if matched:
                hits.append(Match(fact, matched))
        return hits

    def coverage_at(self, audit_at: str) -> list[Coverage]:
        return [c for c in self.coverage if c.captured_at <= audit_at and c.observed_at <= audit_at]

    # ------------------------------------------------------------------- report

    def summary(self) -> list[str]:
        lines = []
        by_suffix: dict[str, list[ParseResult]] = defaultdict(list)
        for ev_id, result in self.results.items():
            by_suffix[Path(self.manifest[ev_id].file_path).suffix.lower()].append(result)
        lines.append(f"{'format':<7}{'files':>6}{'facts':>8}{'coverage':>10}{'issues':>8}  notes")
        for suffix, results in sorted(by_suffix.items()):
            codes = Counter(i.code for r in results for i in r.issues)
            lines.append(f"{suffix:<7}{len(results):>6}{sum(len(r.facts) for r in results):>8}"
                         f"{sum(len(r.coverage) for r in results):>10}{sum(len(r.issues) for r in results):>8}  {dict(codes)}")
        lines.append(f"total: {len(self.facts)} facts, {len(self.coverage)} coverage entries, {len(self.issues)} issue(s) "
                     f"from {len(self.results)} files")
        types = Counter((f.record_type, f.state) for f in self.facts)
        lines.append("record_type/state: " + ", ".join(f"{t}/{s}={n}" for (t, s), n in sorted(types.items())))
        return lines


def extract_all(manifest: list[EvidenceFile], dataset_dir: Path = DATASET_DIR) -> EvidenceStore:
    return EvidenceStore([extract_file(ev, dataset_dir) for ev in manifest], manifest)
