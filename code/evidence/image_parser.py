"""Screenshots -> facts, using OCR and explicit uncertainty.

The 11 PNGs are admin-console screens that share one layout: a top bar, a title and subtitle,
KPI cards, a table, and a right-hand detail panel. For each screenshot:

  1. ``ocr.run_ocr`` returns word boxes from several passes (the image upscaled 2x, 3x, 4x).
  2. Each pass is turned into a ``Layout`` (title, subtitle, KPI cards, table rows, detail panel).
  3. Every value a fact needs is *voted* across passes (``vote``). A value is
       confirmed  - at least two passes produced the same text, and it matches the expected shape
       single     - only one pass produced a well-formed value
       conflict   - passes produced different well-formed values
       unread     - no pass produced a well-formed value (raw candidates are kept for review)
     "Confirmed" means independent OCR passes agree; it is evidence of a correct read, not proof.
  4. Only confirmed values become the fact's resource id, attributes and normal identifiers.
     Single readings become ``uncertain`` identifiers, which exact matching never uses; conflicts
     and unread text are kept only as ``uncertain_text`` candidates and reported as issues.

Nothing is repaired: an OCR string such as ``user_ll`` is never turned into ``user_11``.
Every screenshot fact is ``provisional``: its state is an interpretation of what the screen shows
(``state_basis`` says how) and must be corroborated by other evidence before it proves anything.
For example, a backup that is merely listed does not establish restore risk, and a screenshot alone
can never prove a restore is blocked.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from loaders import EvidenceFile

from .facts import (
    Fact, Identifier, ParseResult, Provenance, classify_value, valid_timestamp,
)
from .ocr import OcrFailed, OcrPass, OcrUnavailable, OcrWord, run_ocr

PARSER = "image"

# Emails in this dataset are all-lowercase (or, rarely, all-uppercase). A mixed-case OCR read such as
# "r.pI27@example.test" (capital I for l) is therefore malformed, not a valid reading.
_EMAIL_LOWER = r"[a-z0-9._+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+"
_EMAIL_UPPER = r"[A-Z0-9._+-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+"
EMAIL = rf"(?:{_EMAIL_LOWER}|{_EMAIL_UPPER})"
USER = r"user_\d+"
PHONE = r"\+\d[\d -]{6,}\d"
WORDS = r"[A-Za-z][A-Za-z0-9 ()/.'-]*"  # a short label such as "Account lifecycle" or "Automated workflow"
NAME = r"[A-Za-z][A-Za-z .'-]*[A-Za-z]"
DAY_TIME = r"\d{2} [A-Za-z]{3} \d{2}:\d{2}"
CLOCK = r"\d{2}:\d{2}:\d{2}"
SIZE = r"[\d.]+ ?(?:KB|MB|GB|TB)"
REGION = r"[a-z]{2}-[a-z]+-\d"
DATETIME_RE = re.compile(r"(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{2}):(\d{2})(?::(\d{2}))? UTC", re.I)
MONTHS = {m: i for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), start=1)}

GAP_JOIN = 14.0  # words closer than this (original px) belong to one cell
GAP_TOUCH = 1.5  # ... and are joined without a space when they nearly touch
ROW_TOL = 9.0
BADGE_X1 = 280.0  # the row-number badge sits left of the first column text
VALUE_OFFSET = 130.0  # detail-panel value column, relative to the panel's left edge


# ------------------------------------------------------------------------- layout


@dataclass(frozen=True, slots=True)
class Cell:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (round(self.x0), round(self.y0), round(self.x1), round(self.y1))


def _join(words: list[OcrWord]) -> Cell:
    text = words[0].text
    for prev, word in zip(words, words[1:]):
        text += ("" if word.x0 - prev.x1 <= GAP_TOUCH else " ") + word.text
    return Cell(text, min(w.x0 for w in words), min(w.y0 for w in words), max(w.x1 for w in words), max(w.y1 for w in words))


def cluster_rows(words: list[OcrWord], tol: float = ROW_TOL) -> list[list[OcrWord]]:
    rows: list[list[OcrWord]] = []
    for word in sorted(words, key=lambda w: (w.yc, w.x0)):
        if rows and abs(word.yc - sum(w.yc for w in rows[-1]) / len(rows[-1])) <= tol:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [sorted(r, key=lambda w: w.x0) for r in rows]


def segment(words: list[OcrWord]) -> list[Cell]:
    """Group words into cells: same row, small horizontal gaps."""
    cells: list[Cell] = []
    for row in cluster_rows(words):
        group = [row[0]]
        for word in row[1:]:
            if word.x0 - group[-1].x1 <= GAP_JOIN:
                group.append(word)
            else:
                cells.append(_join(group))
                group = [word]
        cells.append(_join(group))
    return cells


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _similar(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.78)


@dataclass(slots=True)
class Layout:
    width: int
    height: int
    cells: list[Cell]
    title: Cell | None = None
    badge: Cell | None = None
    subtitle: Cell | None = None
    searchbar: Cell | None = None
    kpis: list[tuple[Cell, Cell]] = field(default_factory=list)  # (label, value)
    panel_title: Cell | None = None
    panel: list[tuple[Cell, Cell]] = field(default_factory=list)  # (label, value)
    header: dict[str, Cell] = field(default_factory=dict)
    rows: list[dict[str, Cell]] = field(default_factory=list)
    declared_rows: int | None = None
    app_text: str = ""

    def kpi(self, label: str) -> Cell | None:
        return next((v for k, v in self.kpis if _similar(k.text, label)), None)

    def panel_value(self, label: str) -> Cell | None:
        return next((v for k, v in self.panel if _similar(k.text, label)), None)


def read_layout(ocr: OcrPass, headers: tuple[str, ...] = ()) -> Layout:
    """Turn one OCR pass into regions. ``headers`` are the table's expected column names."""
    words = [w for w in ocr.words if w.text.strip()]
    cells = segment(words)
    layout = Layout(ocr.width, ocr.height, cells)
    main = [c for c in cells if c.x0 >= 232]

    layout.app_text = " ".join(c.text for c in sorted(cells, key=lambda c: (c.y0, c.x0)) if c.y1 < 60 and 232 <= c.x0 < 470)
    bars = [c for c in cells if c.y1 < 60 and 455 <= c.x0 < 480]
    layout.searchbar = bars[0] if bars else None

    heading = [c for c in main if 318 <= c.y0 <= 348 and c.x0 > 0.55 * ocr.width]
    panel_left = None
    if heading:
        layout.panel_title = max(heading, key=lambda c: c.x0)
        panel_left = layout.panel_title.x0 - 14
    limit = panel_left if panel_left is not None else ocr.width

    top = [c for c in main if 92 <= c.y0 <= 125 and 238 <= c.x0 < limit]
    top.sort(key=lambda c: c.x0)
    if top:
        layout.title = top[0]
        layout.badge = top[1] if len(top) > 1 else None
    sub = [c for c in main if 128 <= c.y0 <= 152 and 238 <= c.x0 < limit]
    layout.subtitle = min(sub, key=lambda c: c.x0) if sub else None

    labels = [c for c in main if 226 <= c.y0 <= 243 and c.x0 < limit]
    values = [c for c in main if 246 <= c.y0 <= 268 and c.x0 < limit]
    for label in sorted(labels, key=lambda c: c.x0):
        match = [v for v in values if abs(v.x0 - label.x0) <= 12]
        if match:
            layout.kpis.append((label, match[0]))

    for cell in main:
        m = re.search(r"(\d+)\s+records", cell.text)
        if m and 318 <= cell.y0 <= 348:
            layout.declared_rows = int(m.group(1))

    if panel_left is not None:
        _read_panel(layout, words, panel_left)
    if headers:
        _read_table(layout, words, headers, limit)
    return layout


def _read_panel(layout: Layout, words: list[OcrWord], panel_left: float) -> None:
    value_x = panel_left + VALUE_OFFSET
    body = [w for w in words if w.x0 >= panel_left and w.y0 > layout.panel_title.y1 + 6]
    last: list | None = None
    for row in cluster_rows(body):
        label_words = [w for w in row if w.x0 < value_x - 8]
        value_words = [w for w in row if w.x0 >= value_x - 8]
        if label_words and _norm(_join(label_words).text) == "activity":
            break
        if label_words:
            label, value = _join(label_words), (_join(value_words) if value_words else None)
            last = [label, value]
            layout.panel.append((label, value if value else Cell("", label.x1, label.y0, label.x1, label.y1)))
        elif value_words and layout.panel:
            label, prev = layout.panel[-1]
            extra = _join(value_words)
            glue = "" if prev.text.endswith(("-", "/")) else " "  # a soft line break inside a path or id
            merged = Cell((prev.text + glue + extra.text).strip(), min(prev.x0, extra.x0) if prev.text else extra.x0,
                          prev.y0 if prev.text else extra.y0, max(prev.x1, extra.x1), extra.y1)
            layout.panel[-1] = (label, merged)


def _read_table(layout: Layout, words: list[OcrWord], headers: tuple[str, ...], limit: float) -> None:
    area = [w for w in words if 232 <= w.x0 < limit and w.y0 > 350]
    best = None
    for row in cluster_rows(area):
        found = {}
        for cell in segment(row):
            hit = next((h for h in headers if h not in found and _similar(cell.text, h)), None)
            if hit:
                found[hit] = cell
        if len(found) >= max(2, len(headers) // 2) and (best is None or len(found) > len(best[1])):
            best = (row, found)
    if best is None:
        return
    header_row, found = best
    layout.header = found
    header_y = sum(w.yc for w in header_row) / len(header_row)
    columns = sorted(found.items(), key=lambda kv: kv[1].x0)
    body = [w for w in area if w.yc > header_y + 14 and w.x1 > BADGE_X1]
    for row in cluster_rows(body, tol=18):
        by_col: dict[str, list[OcrWord]] = {}
        for word in row:
            name = None
            for h, cell in columns:
                if cell.x0 - 10 <= word.x0:
                    name = h
            if name is not None:
                by_col.setdefault(name, []).append(word)
        if by_col:
            layout.rows.append({h: _join(ws) for h, ws in by_col.items()})


# ------------------------------------------------------------------------- voting


@dataclass(frozen=True, slots=True)
class Reading:
    value: str | None
    status: str  # confirmed | single | conflict | unread | empty (no pass saw any text)
    candidates: tuple[str, ...] = ()
    bbox: tuple[int, int, int, int] | None = None

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


UNREAD = Reading(None, "unread")


def vote(seen: list[Cell | None], pattern: str | None = None, transform: Callable[[str], str | None] | None = None) -> Reading:
    """Combine one reading per pass. ``pattern`` (fullmatch) defines a well-formed value."""
    valid: list[tuple[str, Cell]] = []
    invalid: list[str] = []
    for cell in seen:
        if cell is None or not cell.text.strip():
            continue
        text = cell.text.strip()
        if transform is not None:
            text = transform(text) or ""
        if text and (pattern is None or re.fullmatch(pattern, text)):
            valid.append((text, cell))
        elif cell.text.strip():
            invalid.append(cell.text.strip())
    if not valid:
        if not invalid:
            return Reading(None, "empty")
        return Reading(None, "unread", tuple(dict.fromkeys(invalid)))
    counts = Counter(t for t, _ in valid)
    best, n = counts.most_common(1)[0]
    bbox = next(c.bbox for t, c in valid if t == best)
    candidates = tuple(dict.fromkeys(list(counts) + invalid))
    if n >= 2 and sum(1 for c in counts.values() if c == n) == 1:
        return Reading(best, "confirmed", candidates, bbox)
    if len(counts) == 1:
        return Reading(best, "single", candidates, bbox)
    return Reading(None, "conflict", candidates, bbox)


def grep(pattern: "str | re.Pattern") -> Callable[[str], str | None]:
    """A transform that keeps the first regex match inside a cell's text."""
    source = pattern.pattern if isinstance(pattern, re.Pattern) else pattern
    flags = pattern.flags if isinstance(pattern, re.Pattern) else 0
    # Token boundaries: never accept the clean tail of a garbled token (e.g. "pI27@x.com" -> "27@x.com").
    compiled = re.compile(rf"(?<![A-Za-z0-9_.+@-])(?:{source})(?![A-Za-z0-9_-])", flags)

    def pick(text: str) -> str | None:
        m = compiled.search(text)
        return m.group(0) if m else None
    return pick


def iso_from_text(text: str) -> str | None:
    """'07 Aug 2026 08:00 UTC' -> '2026-08-07T08:00:00Z' (None when it is not a real time)."""
    m = DATETIME_RE.search(text)
    if not m or m.group(2).title() not in MONTHS:
        return None
    day, mon, year, hh, mm, ss = m.groups()
    stamp = f"{int(year):04d}-{MONTHS[mon.title()]:02d}-{int(day):02d}T{hh}:{mm}:{ss or '00'}Z"
    return stamp if valid_timestamp(stamp) else None


# -------------------------------------------------------------------- fact context


class Screen:
    """All passes for one screenshot plus the helpers extractors use to emit facts."""

    def __init__(self, layouts: list[Layout], ev: EvidenceFile, result: ParseResult):
        self.layouts, self.ev, self.result = layouts, ev, result
        self.n = len(layouts)

    # -- reading helpers
    def read(self, getter: Callable[[Layout], Cell | None], pattern: str | None = None, transform=None) -> Reading:
        return vote([getter(layout) for layout in self.layouts], pattern, transform)

    def aligned_rows(self) -> list[list[dict[str, Cell]]]:
        """Per-pass row lists restricted to passes whose row count agrees with the screen's own count."""
        declared = [l.declared_rows for l in self.layouts if l.declared_rows is not None]
        want = Counter(declared).most_common(1)[0][0] if declared else Counter(len(l.rows) for l in self.layouts).most_common(1)[0][0]
        good = [l.rows for l in self.layouts if len(l.rows) == want]
        if len(good) < len(self.layouts):
            self.result.issue("WARN", "ROW_COUNT_MISMATCH", "table",
                              f"{len(self.layouts) - len(good)} of {len(self.layouts)} OCR passes found a different number of rows than the expected {want}; those passes were ignored")
        return good

    # -- emit
    def emit(self, *, system: str, record_type: str, locator: str, state: str, basis: str,
             resource: Reading | None = None, idents: tuple[tuple[str, Reading], ...] = (),
             attrs: dict[str, Reading | str] | None = None, observed: Reading | None = None) -> None:
        identifiers: list[Identifier] = []
        # Screenshot-derived states are provisional interpretations until other evidence corroborates them.
        attributes: dict = {"source": "screenshot_ocr", "provisional": True, "state_basis": basis}
        statuses: dict[str, str] = {}
        uncertain: dict[str, list[str]] = {}
        boxes: dict[str, str] = {}

        def note(name: str, reading: Reading, required: bool = False) -> None:
            statuses[name] = reading.status
            if reading.bbox:
                boxes[name] = "{},{}-{},{}".format(*reading.bbox)
            if reading.status == "empty" and not required:
                return  # nothing was read (e.g. a dash); that is not evidence of anything
            if not reading.confirmed:
                uncertain[name] = list(reading.candidates)
                self.result.issue("WARN", "UNCERTAIN_TEXT", f"{locator} / {name}",
                                  f"{name} is {reading.status}; OCR candidates {list(reading.candidates)}")

        resource_id = None
        if resource is not None:
            note("resource_id", resource, required=True)
            resource_id = resource.value if resource.confirmed else None
        for name, reading in idents:
            note(name, reading)
            if reading.value is None:
                continue
            if reading.confirmed:
                identifiers.append(Identifier(name, reading.value, classify_value(name, reading.value), "ocr"))
            else:
                identifiers.append(Identifier(name, reading.value, "uncertain", "ocr"))
        for name, value in (attrs or {}).items():
            if isinstance(value, Reading):
                if value.confirmed:
                    attributes[name] = value.value
                    boxes.setdefault(name, "{},{}-{},{}".format(*value.bbox) if value.bbox else "")
                elif value.status != "empty":
                    uncertain[name] = list(value.candidates)
                    statuses[name] = value.status
            else:
                attributes[name] = value
        observed_at = None
        if observed is not None:
            note("observed_at", observed)
            observed_at = iso_from_text(observed.value) if observed.confirmed and observed.value else None
        attributes["ocr"] = statuses
        attributes["ocr_boxes"] = boxes
        if uncertain:
            attributes["uncertain_text"] = uncertain
        bbox = (resource.bbox if resource is not None and resource.bbox else None) or next(
            (r.bbox for _, r in idents if r.bbox), None)
        where = locator + (" @ ({},{})-({},{})".format(*bbox) if bbox else "")
        self.result.facts.append(Fact(
            evidence_id=self.ev.file_id, captured_at=self.ev.captured_at, observed_at=observed_at or self.ev.captured_at,
            system=system, resource_id=resource_id, record_type=record_type, state=state,
            identifiers=tuple(identifiers), attributes=attributes, store=None,
            provenance=Provenance(self.ev.file_id, self.ev.file_path, where, PARSER)))
        self.result.count("facts")


# ------------------------------------------------------------------ screen extractors


def x_account_console(s: Screen) -> None:
    sub = lambda l: l.subtitle
    resource = s.read(sub, r"account_\d+", grep(r"account_\d+"))
    user = s.read(sub, USER, grep(USER))
    email = s.read(lambda l: l.kpi("Email"), EMAIL, grep(EMAIL))
    phone = s.read(lambda l: l.kpi("Phone"), PHONE, grep(PHONE))
    name = s.read(lambda l: l.title, NAME)
    s.emit(system="account_service", record_type="USER_PROFILE", locator="customer overview", state="FOUND",
           basis="customer record is displayed in the account console", resource=resource,
           idents=(("customer_reference", user), ("email", email), ("phone", phone), ("full_name", name)),
           attrs={"tier": s.read(lambda l: l.badge, r"[A-Za-z]+"), "market": s.read(lambda l: l.kpi("Market"), r"[A-Z]{2}"),
                  "signup_channel": s.read(lambda l: l.kpi("Signup channel"), r"[a-z_]+")})


def x_analytics(s: Screen) -> None:
    sub = lambda l: l.subtitle
    resource = s.read(sub, r"analytics_\d+", grep(r"analytics_\d+"))
    user = s.read(sub, USER, grep(USER))
    email = s.read(sub, EMAIL, grep(EMAIL))
    any_text = any(r.value or r.candidates for r in (user, email))
    s.emit(system="analytics_platform", record_type="ANALYTICS_PROFILE", locator="user profile",
           state="IDENTIFIABLE" if any_text else "UNKNOWN",
           basis="profile header shows user id and/or email" if any_text else "no identifier could be read",
           resource=resource, idents=(("user_id", user), ("email", email), ("display_name", s.read(lambda l: l.title, NAME))),
           attrs={"window": s.read(lambda l: l.badge, WORDS)})


def x_marketing(s: Screen) -> None:
    sub = lambda l: l.subtitle
    s.emit(system="marketing_platform", record_type="MARKETING_PROFILE", locator="audience profile", state="FOUND",
           basis="marketing profile is displayed", resource=s.read(sub, r"marketing_\d+", grep(r"marketing_\d+")),
           idents=(("email", s.read(sub, EMAIL, grep(EMAIL))), ("phone", s.read(sub, PHONE, grep(PHONE))),
                   ("display_name", s.read(lambda l: l.title, NAME))),
           attrs={"status": s.read(lambda l: l.badge, r"[A-Za-z]+"),
                  "email_opt_in": s.read(lambda l: l.kpi("Email opt-in"), r"Yes|No"),
                  "sms_opt_in": s.read(lambda l: l.kpi("SMS opt-in"), r"Yes|No"),
                  "last_refreshed": s.read(lambda l: l.kpi("Last refreshed"), None, grep(DATETIME_RE))})


def x_support(s: Screen) -> None:
    s.emit(system="support_platform", record_type="SUPPORT_MESSAGE", locator="ticket header", state="IDENTIFIABLE",
           basis="ticket header shows the requester's email and name",
           resource=s.read(lambda l: l.subtitle, r"support_\d+", grep(r"support_\d+")),
           idents=(("requester_email", s.read(lambda l: l.kpi("Email"), EMAIL, grep(EMAIL))),
                   ("requester_name", s.read(lambda l: l.kpi("Customer"), NAME))),
           attrs={"ticket": s.read(lambda l: l.title, r"CS-\d+", grep(r"CS-\d+")),
                  "order": s.read(lambda l: l.panel_title, r"ORD-\d+", grep(r"ORD-\d+"))})


def x_registry(s: Screen) -> None:
    user = s.read(lambda l: l.title, USER, grep(USER))
    linked = []
    rows = s.aligned_rows()
    if rows:
        for i in range(len(rows[0])):
            reading = vote([r[i].get("Record reference") for r in rows if i < len(r)], r"[A-Z]{2,4}-\d+", grep(r"[A-Z]{2,4}-\d+"))
            if reading.confirmed:
                linked.append(reading.value)
    s.emit(system="identity_service", record_type="ACCOUNT_TOMBSTONE", locator="customer reference", state="REDACTED",
           basis="only the stable user reference is shown (labelled 'Retained reference'); no name, email, phone or hash is displayed",
           resource=None, idents=(("subject_reference", user),),
           attrs={"label": s.read(lambda l: l.badge, WORDS), "linked_records": ",".join(linked)})


def _rows_for(s: Screen):
    """Yield (index, per-pass cells for that row) over passes with agreeing row counts."""
    rows = s.aligned_rows()
    count = len(rows[0]) if rows else 0
    for i in range(count):
        yield i, [r[i] for r in rows]


def _row(passes: list[dict[str, Cell]], header: str, pattern: str | None = None, transform=None) -> Reading:
    return vote([r.get(header) for r in passes], pattern, transform)


def x_backups(s: Screen) -> None:
    selected = s.read(lambda l: l.panel_title, r"backup_\d+", grep(r"backup_\d+"))
    path = s.read(lambda l: l.panel_value("Path"), r"s3://\S+( \S+)*")
    retention = s.read(lambda l: l.panel_value("Retention end"), r"\d{1,2} [A-Za-z]{3} \d{4}")
    for i, passes in _rows_for(s):
        rid = _row(passes, "Archive ID", r"backup_\d+", grep(r"backup_\d+"))
        extra = {"archive_path": path, "retention_end": retention} if rid.confirmed and rid.value == selected.value else {}
        s.emit(system="backup_service", record_type="BACKUP_ARCHIVE", locator=f"archives table row {i + 1}", state="FOUND",
               basis="archive is listed; the screen does not show whether restore is allowed or blocked",
               resource=rid, idents=(("customer_email", _row(passes, "Account", EMAIL, grep(EMAIL))),),
               attrs={"restore_access": "not shown", "region": _row(passes, "Region", REGION), "created": _row(passes, "Created", DAY_TIME),
                      "encryption": _row(passes, "Encryption", r"AES-\d+"), "tier": _row(passes, "Tier", r"[A-Za-z]+"), **extra})


def x_connectors(s: Screen) -> None:
    observed = s.read(lambda l: l.subtitle, None, grep(DATETIME_RE))
    status = s.read(lambda l: l.panel_value("Status"), r"[A-Za-z]+")
    selected = s.read(lambda l: l.panel_title, r"data_flow_\d+", grep(r"data_flow_\d+"))
    skipped = 0
    for i, passes in _rows_for(s):
        rid = _row(passes, "Connector", r"data_flow_\d+", grep(r"data_flow_\d+"))
        if rid.value is None and not rid.candidates:
            skipped += 1
            continue
        if rid.value is None and not re.match(r"data.?flow", " ".join(rid.candidates), re.I):
            skipped += 1  # a named connector without a data-flow id (e.g. "Order notifications")
            continue
        mode, events = _row(passes, "Mode", r"[A-Za-z]+"), _row(passes, "Events/hour", r"[\d,]+")
        shown = {"status": status} if rid.confirmed and rid.value == selected.value else {}
        s.emit(system="integration_service", record_type="DATA_FLOW", locator=f"topology table row {i + 1}", state="ACTIVE",
               basis="listed with a continuous mode and live events/hour; the screen shows no explicit enabled/disabled flag",
               resource=rid, idents=(("subject_selector", _row(passes, "Selector", EMAIL, grep(EMAIL))),),
               attrs={"mode": mode, "events_per_hour": events, "destination": _row(passes, "Destination", r"[a-z_]+"),
                      "last_contact": _row(passes, "Last contact", CLOCK), **shown}, observed=observed)
    if skipped:
        s.result.count("rows_without_resource_id", skipped)
        s.result.issue("INFO", "ROW_WITHOUT_RESOURCE_ID", "topology table", f"{skipped} connector row(s) have a name but no data_flow id and were not extracted")


def x_exports(s: Screen) -> None:
    selected = s.read(lambda l: l.panel_title, r"export_\d+", grep(r"export_\d+"))
    detail = {"expires": s.read(lambda l: l.panel_value("Expires"), None, grep(DATETIME_RE)),
              "download_count": s.read(lambda l: l.panel_value("Download count"), r"\d+")}
    for i, passes in _rows_for(s):
        rid = _row(passes, "Export ID", r"export_\d+", grep(r"export_\d+"))
        extra = detail if rid.confirmed and rid.value == selected.value else {}
        s.emit(system="export_service", record_type="USER_EXPORT", locator=f"exports table row {i + 1}", state="FOUND",
               basis="export job is listed", resource=rid,
               idents=(("owner_email", _row(passes, "Owner", EMAIL, grep(EMAIL))),),
               attrs={"file_name": _row(passes, "Filename", r"customer_export_\d+\.zip"), "status": _row(passes, "Status", r"[A-Za-z]+"),
                      "created": _row(passes, "Created", r"\d{2} [A-Za-z]{3} \d{2}:\d{2} UTC"), **extra})


def _file_type(name: str) -> str | None:
    lower = name.lower()
    if lower.startswith("customer_export") and lower.endswith(".zip"):
        return "USER_EXPORT"
    if lower.endswith((".pdf", ".jpg", ".jpeg", ".png")):
        return "SUPPORT_ATTACHMENT"
    return None


def x_storage(s: Screen) -> None:
    observed = s.read(lambda l: l.kpi("Captured"), None, grep(DATETIME_RE))
    for i, passes in _rows_for(s):
        name = _row(passes, "Name", r"[\w.-]+\.(?:pdf|zip|jpg|jpeg|png)")
        record_type = _file_type(name.value) if name.value else None
        if record_type is None:
            s.result.count("rows_unmapped")
            s.result.issue("WARN", "UNCERTAIN_TEXT", f"objects table row {i + 1}", f"object name not read reliably: {list(name.candidates)}")
            continue
        s.emit(system="object_storage", record_type=record_type, locator=f"objects table row {i + 1}", state="FOUND",
               basis="object is listed; record type is inferred from the file name and the support-attachments path; "
                     "the screen shows no resource id or owner",
               resource=None, attrs={"file_name": name, "size": _row(passes, "Size", SIZE), "modified": _row(passes, "Modified (UTC)", DAY_TIME),
                                     "storage_class": _row(passes, "Storage class", r"[A-Za-z]+"), "version": _row(passes, "Version", r"v\d+")},
               observed=observed)


def x_search(s: Screen) -> None:
    observed = s.read(lambda l: l.kpi("Executed"), None, grep(DATETIME_RE))
    query = s.read(lambda l: l.searchbar, rf"email:{EMAIL}", grep(rf"email:{EMAIL}"))
    for i, passes in _rows_for(s):
        s.emit(system="search_service", record_type="SEARCH_INDEX_ENTRY", locator=f"results table row {i + 1}", state="FOUND",
               basis="document is returned by the index query", resource=_row(passes, "_id", r"search_\d+", grep(r"search_\d+")),
               idents=(("email", _row(passes, "Email", EMAIL, grep(EMAIL))),
                       ("customer_reference", _row(passes, "Customer reference", USER, grep(USER))),
                       ("display_name", _row(passes, "Display name", NAME))),
               attrs={"query": query, "score": _row(passes, "Score", r"\d+\.\d+"),
                      "last_indexed": _row(passes, "Last indexed", rf"{CLOCK} UTC")},
               observed=observed)


def x_audit(s: Screen) -> None:
    # The Subject column loses its underscore in OCR ("user 03"). The page header states the filter
    # ("Subject filter: user_03") in clearer text; it is used only when a row's own cell is unreadable.
    page_filter = s.read(lambda l: l.subtitle, USER, grep(USER))
    for i, passes in _rows_for(s):
        cell = _row(passes, "Subject", USER, grep(USER))
        use_filter = not cell.confirmed and page_filter.confirmed
        subject = page_filter if use_filter else cell
        s.emit(system="security_service", record_type="SECURITY_AUDIT_LOG", locator=f"account events table row {i + 1}", state="FOUND",
               basis="signed audit event is listed", resource=_row(passes, "Reference", r"audit_record_\d+", grep(r"audit_record_\d+")),
               idents=(("subject_reference", subject),),
               attrs={"subject_source": "page subject filter (the table cell was unreadable)" if use_filter else "table cell",
                      "event": _row(passes, "Event", WORDS), "category": _row(passes, "Category", WORDS),
                      "actor": _row(passes, "Actor", WORDS), "integrity": _row(passes, "Integrity", r"[A-Za-z]+"),
                      "event_time": _row(passes, "Time (UTC)", DAY_TIME)})


@dataclass(frozen=True, slots=True)
class App:
    key: str
    phrases: tuple[str, ...]
    headers: tuple[str, ...]
    extract: Callable[[Screen], None]


APPS = (
    App("account_console", ("customer operations",), (), x_account_console),
    App("analytics", ("pulse analytics",), (), x_analytics),
    App("backups", ("vault recovery",), ("Archive ID", "Account", "Region", "Created", "Size", "Encryption", "Tier"), x_backups),
    App("connectors", ("bridgeops",), ("Connector", "Destination", "Selector", "Mode", "Events/hour", "Last contact"), x_connectors),
    App("exports", ("customer download center",), ("Export ID", "Filename", "Owner", "Status", "Created"), x_exports),
    App("registry", ("customer reference",), ("System", "Record reference", "Relationship", "Created (UTC)", "Status"), x_registry),
    App("marketing", ("audience operations",), (), x_marketing),
    App("storage", ("nimbus object storage",), ("Name", "Size", "Modified (UTC)", "Storage class", "Version"), x_storage),
    App("search", ("search console",), ("_id", "Display name", "Email", "Customer reference", "Score", "Last indexed"), x_search),
    App("audit", ("sentinel",), ("Time (UTC)", "Event", "Category", "Subject", "Actor", "Integrity", "Reference"), x_audit),
    App("support", ("helpline console",), (), x_support),
)


def detect_app(ocr: OcrPass) -> App | None:
    text = _norm(" ".join(c.text for c in segment([w for w in ocr.words if w.y1 < 60 and 232 <= w.x0 < 470])))
    hits = [a for a in APPS if any(_norm(p) in text for p in a.phrases)]
    return hits[0] if len(hits) == 1 else None


# ----------------------------------------------------------------------- parse


def parse(path: Path, ev: EvidenceFile, ocr=run_ocr) -> ParseResult:
    result = ParseResult(ev.file_id, PARSER)
    try:
        passes = ocr(path)
    except OcrUnavailable as exc:
        result.issue("ERROR", "OCR_UNAVAILABLE", "file", f"image text could not be read: {exc}")
        return result
    except (OcrFailed, OSError) as exc:
        result.issue("ERROR", "OCR_FAILED", "file", f"OCR failed: {exc}")
        return result
    result.count("ocr_passes", len(passes))
    result.count("ocr_words", sum(len(p.words) for p in passes))
    usable = [p for p in passes if len(p.words) >= 10]
    if len(usable) < len(passes):
        result.issue("WARN", "EMPTY_OCR_PASS", "file", f"{len(passes) - len(usable)} OCR pass(es) returned almost no text")
    if not usable:
        result.issue("ERROR", "NO_TEXT", "file", "OCR found no readable text in the screenshot")
        return result
    if len(usable) < 2:
        result.issue("WARN", "TOO_FEW_PASSES", "file", "fewer than two usable OCR passes; no value can be confirmed")

    apps = Counter(a.key if (a := detect_app(p)) else None for p in usable)
    key = apps.most_common(1)[0][0]
    app = next((a for a in APPS if a.key == key), None)
    if app is None:
        result.issue("ERROR", "UNRECOGNIZED_SCREEN", "header",
                     f"screenshot application not recognised from {_norm(' '.join(w.text for w in usable[0].words if w.y1 < 60 and w.x0 >= 232))[:40]!r}")
        return result
    layouts = [read_layout(p, app.headers) for p in usable]
    app.extract(Screen(layouts, ev, result))
    if not result.facts:
        result.issue("ERROR", "NO_FACTS", "file", f"recognised {app.key} screen but no record could be read")
    return result
