"""PDF business documents -> facts.

Four document templates exist (identified by their title line):
  * DELIVERY ADDRESS CORRECTION REQUEST   a support attachment (resource support_NNN)
  * COMPLIANCE JOURNAL - SIGNED RECORD EXTRACT   a retained security audit record
  * DISPUTED ORDERS AND TAX RECORDS REGISTER   rows of retained order records
  * NORTHSTAR SHOP - CUSTOMER STATEMENT   a customer statement with name/email/order data

Text is read with PyMuPDF, whitespace is collapsed, and fields are pulled out by exact
patterns, because the PDF layout splits labels and values across lines. Missing PyMuPDF,
unreadable PDFs, and unrecognised templates are reported, never skipped silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from loaders import EvidenceFile

from .facts import (
    EMAIL_TOKEN_RE, Identifier, ParseResult, Provenance, make_identifier, record_fact,
)

PARSER = "pdf"
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_DATETIME = r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4}) (\d{2}) (\d{2}) UTC"
_DATE = r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})"
_RESOURCE_RE = re.compile(
    r"\b(?:account|search|analytics|support|export|data_flow|backup|audit_record|tombstone|legal_hold"
    r"|cache|email_delivery|marketing|object)_\d+\b")
_USER_RE = re.compile(r"\buser_\d+\b")
_PHONE_RE = re.compile(r"\+\d[\d ]{7,}\d")


def _backend():
    try:
        import pymupdf  # type: ignore
        return pymupdf
    except ImportError:
        try:
            import fitz  # type: ignore
            return fitz
        except ImportError:
            return None


def _iso(day, mon, year, hh="00", mm="00") -> str | None:
    if mon not in _MONTHS:
        return None
    return f"{int(year):04d}-{_MONTHS[mon]:02d}-{int(day):02d}T{hh}:{mm}:00Z"


def _stamp(pattern: str, text: str) -> str | None:
    m = re.search(pattern + r" " + _DATETIME, text)
    return _iso(*m.groups()) if m else None


def _date_after(label: str, text: str) -> str | None:
    m = re.search(label + r" " + _DATE, text)
    return _iso(*m.groups()) if m else None


def read_pages(path: Path, result: ParseResult) -> list[str] | None:
    backend = _backend()
    if backend is None:
        result.issue("ERROR", "PDF_BACKEND_MISSING", "file", "PyMuPDF is not installed; PDF evidence cannot be read")
        return None
    try:
        with backend.open(str(path)) as doc:
            return [page.get_text("text") for page in doc]
    except Exception as exc:  # noqa: BLE001 - any backend failure means an unreadable PDF
        result.issue("ERROR", "UNREADABLE_PDF", "file", f"PDF could not be opened: {exc}")
        return None


def parse(path: Path, ev: EvidenceFile) -> ParseResult:
    result = ParseResult(ev.file_id, PARSER)
    pages = read_pages(path, result)
    if pages is None:
        return result
    result.count("pages", len(pages))
    text = re.sub(r"\s+", " ", " ".join(pages)).strip()
    if not text:
        result.issue("ERROR", "NO_TEXT", "file", "PDF has no extractable text layer")
        return result
    title = text[:80]
    before = len(result.facts)
    if title.startswith("DELIVERY ADDRESS CORRECTION REQUEST"):
        _attachment(text, ev, result)
    elif title.startswith("COMPLIANCE JOURNAL"):
        _journal(text, ev, result)
    elif title.startswith("DISPUTED ORDERS AND TAX RECORDS REGISTER"):
        _register(text, ev, result)
    elif title.startswith("NORTHSTAR SHOP - CUSTOMER STATEMENT"):
        _statement(text, ev, result)
    else:
        result.issue("WARN", "UNRECOGNIZED_TEMPLATE", "page 1", f"unknown document title {title[:40]!r}; generic extraction used")
        _generic(text, ev, result)
    if len(result.facts) == before:
        result.issue("ERROR", "NO_FACTS", "file", "recognised document yielded no facts")
    result.count("facts", len(result.facts))
    return result


# ------------------------------------------------------------------- templates


def _prov(ev, locator):
    return Provenance(ev.file_id, ev.file_path, locator, PARSER)


def _idents(text, extra=()):
    found = [Identifier("document_text", m.group(0), "email", "text") for m in EMAIL_TOKEN_RE.finditer(text)]
    found += [Identifier("document_text", p.strip(), "phone", "text") for p in _PHONE_RE.findall(text)]
    found += [make_identifier("document_text", u, "text") for u in dict.fromkeys(_USER_RE.findall(text))]
    return tuple(dict.fromkeys(i for i in [*found, *extra] if i))


def _fact(ev, *, system, record_type, resource_id, identifiers, observed, attrs, locator, state="FOUND"):
    fact = record_fact(
        evidence_id=ev.file_id, captured_at=ev.captured_at, observed_at=observed, system=system,
        record_type=record_type, raw={}, store="pdf_document", provenance=_prov(ev, locator),
        resource_id=resource_id, extra_identifiers=identifiers, state=state, extra_attributes=attrs)
    return fact


def _attachment(text, ev, result):
    resources = _RESOURCE_RE.findall(text)
    if not resources:
        result.issue("ERROR", "MISSING_FIELD", "page 1", "no support resource id found in the attachment")
        return
    attrs = {k: m.group(1) for k, p in {
        "ticket": r"ticket (CS-\d+)", "file": r"file (\S+\.pdf)", "uploader": r"uploader (\w+)",
        "order": r"order (ORD-\d+)"}.items() if (m := re.search(p, text))}
    result.facts.append(_fact(
        ev, system="support_platform", record_type="SUPPORT_ATTACHMENT", resource_id=resources[0],
        identifiers=_idents(text), observed=_stamp("captured", text) or ev.captured_at, attrs=attrs,
        locator="document"))


def _journal(text, ev, result):
    resource = re.search(r"Event reference (audit_record_\d+)", text)
    subject = re.search(r"subject reference (user_\d+)", text)
    if not resource:
        result.issue("ERROR", "MISSING_FIELD", "page 1", "no audit_record event reference found")
        return
    attrs = {"event": (m.group(1).strip() if (m := re.search(r"event (.+?)(?: Detail)? actor", text)) else None),
             "recorded_at": _stamp("recorded", text), "retain_until": _stamp("retain until", text),
             "journal_reference": (m.group(1) if (m := re.search(r"Reference (CJX-[\w-]+)", text)) else None)}
    ids = (make_identifier("subject_reference", subject.group(1)),) if subject else ()
    result.facts.append(_fact(
        ev, system="security_service", record_type="SECURITY_AUDIT_LOG", resource_id=resource.group(1),
        identifiers=ids, observed=_stamp("issued", text) or ev.captured_at, attrs=attrs, locator="document"))


def _register(text, ev, result):
    generated = _stamp("generated", text) or ev.captured_at
    rows = re.finditer(
        r"(ORD-\d+) \| (user_\d+) \| ([^|]+?) \| " + _DATE + r" \| ([A-Z]{3} [\d.,]+) \| Retain until " + _DATE + r" \| (\w+)",
        text)
    for n, m in enumerate(rows, start=1):
        order, user, reason = m.group(1), m.group(2), m.group(3).strip()
        result.facts.append(_fact(
            ev, system="legal_service", record_type="LEGAL_HOLD_RECORD", resource_id=None,
            identifiers=(make_identifier("subject_reference", user),), observed=generated,
            attrs={"order_reference": order, "reason": reason, "order_date": _iso(*m.groups()[3:6]),
                   "amount": m.group(7), "retain_until": _iso(*m.groups()[7:10]), "owner": m.group(11)},
            locator=f"register row {n}"))


def _statement(text, ev, result):
    users = list(dict.fromkeys(_USER_RE.findall(text)))
    emails = [m.group(0) for m in EMAIL_TOKEN_RE.finditer(text)]
    if not users and not emails:
        result.issue("ERROR", "MISSING_FIELD", "page 1", "statement has no user id or email")
        return
    attrs = {k: m.group(1) for k, p in {
        "statement": r"Statement (NST-[\w-]+)", "order": r"order (ORD-\d+)"}.items() if (m := re.search(p, text))}
    result.facts.append(_fact(
        ev, system="account_history_service", record_type="CUSTOMER_STATEMENT", resource_id=None,
        identifiers=_idents(text), observed=ev.captured_at, attrs=attrs, locator="document"))


def _generic(text, ev, result):
    resources = list(dict.fromkeys(_RESOURCE_RE.findall(text)))
    result.facts.append(_fact(
        ev, system="unknown", record_type="DOCUMENT", resource_id=resources[0] if resources else None,
        identifiers=_idents(text), observed=ev.captured_at, attrs={"resources_mentioned": ",".join(resources)},
        locator="document", state="UNKNOWN"))
