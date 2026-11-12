"""Synthetic OCR output that mimics the console screenshots' geometry (no real OCR needed)."""

from evidence.ocr import OcrPass, OcrWord

WIDTH, HEIGHT = 1800, 1150
SPACE = 5.0


def words(text: str, x: float, y: float, h: float = 13.0) -> list[OcrWord]:
    """Lay text out as OCR words left to right starting at (x, y); glyph width follows the font height."""
    out = []
    char = 0.5 * h if h <= 16 else 0.57 * h
    for part in text.split(" "):
        w = char * len(part)
        out.append(OcrWord(part, x, y, x + w, y + h))
        x += w + SPACE
    return out


def screen(app: str, *, title="", badge=None, subtitle="", kpis=(), header=(), rows=(), declared=None,
           panel_title="", panel=(), searchbar=None) -> list[OcrWord]:
    """Build one screenshot's words.

    header: [(column name, x)]; rows: list of lists of cell texts aligned with header (None = empty cell);
    kpis: [(label, value, x)]; panel: [(label, value)] (a value may contain '|' for wrapped lines).
    """
    out = words(app, 244, 23, 14)
    if searchbar:
        out += words(searchbar, 468, 24, 14)
    if title:
        out += words(title, 247, 105, 22)
    if badge:
        out += words(badge, 247 + 0.57 * 22 * len(title) + 40, 107, 12)
    if subtitle:
        out += words(subtitle, 247, 138, 13)
    for label, value, x in kpis:
        out += words(label, x, 234, 11) + words(value, x, 254, 16)
    if declared is not None or rows:
        out += words(f"Updated moments ago · {declared if declared is not None else len(rows)} records", 1221, 334, 11)
    for name, x in header:
        out += words(name, x, 374, 12)
    for i, row in enumerate(rows):
        y = 410 + 41 * i
        out += words(str(i + 1), 264, y + 2, 11)  # the row-number badge
        for (name, x), cell in zip(header, row):
            if cell:
                out += words(cell, 284 if x < 300 else x, y, 12)
    if panel_title:
        out += words(panel_title, 1442, 333, 14)
        y = 380
        for label, value in panel:
            out += words(label, 1442, y, 11)
            for k, line in enumerate(value.split("|")):
                out += words(line, 1557, y + 16 * k, 12)
            y += 33 + 16 * (len(value.split("|")) - 1)
        out += words("Activity", 1442, y + 30, 13)
        out += words("25 Apr backup restored", 1464, y + 60, 11)
    return out


def passes_from(base: list[OcrWord], *overrides: dict[str, str | None]) -> list[OcrPass]:
    """One OCR pass per override dict. A dict maps a word's exact text to its replacement
    (None drops the word), imitating pass-specific OCR errors."""
    result = []
    for k, sub in enumerate(overrides or ({},), start=2):
        kept = []
        for w in base:
            if w.text in sub:
                if sub[w.text] is None:
                    continue
                w = OcrWord(sub[w.text], w.x0, w.y0, w.x1, w.y1)
            kept.append(w)
        result.append(OcrPass(k, WIDTH, HEIGHT, tuple(kept)))
    return result


BACKUP_HEADER = [("Archive ID", 258), ("Account", 454), ("Region", 752), ("Created", 921), ("Size", 1072),
                 ("Encryption", 1188), ("Tier", 1321)]


def backup_screen(**kw) -> list[OcrWord]:
    rows = [["backup_9291", "vendor847@example.test", "ap-southeast-1", "26 Apr 02:00", "102.3 GB", "AES-256", "Archive"],
            ["backup_9288", "supplier.group@example.test", "us-west-2", "26 Apr 02:00", "67.1 GB", "AES-256", "Archive"],
            ["backup_9287", "bm4273@example.test", "eu-west-1", "26 Apr 02:00", "84.7 GB", "AES-256", "Archive"]]
    return screen("Vault Recovery Manager", title="Customer-platform archives", badge="Healthy",
                  subtitle="Northstar Production · weekly snapshots", header=BACKUP_HEADER, rows=rows,
                  panel_title="backup_9287",
                  panel=[("Account", "bm4273@example.test"), ("Path", "s3://northstar-backups/customer-|platform/weekly/segment-42-|20260426.tar.zst"),
                         ("Retention end", "26 Jul 2026")], **kw)


SEARCH_HEADER = [("_id", 258), ("Display name", 464), ("Email", 640), ("Customer reference", 904), ("Score", 1146), ("Last indexed", 1244)]


def search_screen(executed="01 Sep 2026 06:00:14 UTC") -> list[OcrWord]:
    rows = [["search_428", "Rowan Parsons", "r.pl27@example.test", "user_13", "9.84", "05:58:21 UTC"],
            ["search_1874", None, "r.pl27.jr@example.test", None, "4.12", "05:55:03 UTC"]]
    return screen("Search Console", title="Customer Search", badge="2 hits", subtitle="customer_directory_v3 / prod-eu",
                  searchbar="email:r.pl27@example.test", kpis=[("Executed", executed, 260), ("Latency", "184 ms", 568)],
                  header=SEARCH_HEADER, rows=rows, panel_title="Raw document · search_428",
                  panel=[("_id", "search_428"), ("email", "r.pl27@example.test")])


AUDIT_HEADER = [("Time (UTC)", 258), ("Event", 427), ("Category", 689), ("Subject", 882), ("Actor", 973), ("Integrity", 1158), ("Reference", 1256)]


def audit_screen(subject_cell="user_03") -> list[OcrWord]:
    rows = [["25 Aug 04:03", "Account closure recorded", "Account lifecycle", subject_cell, "Automated workflow", "Valid", "audit_record_003"],
            ["25 Aug 03:21", "Session revoked", "Session management", subject_cell, "Security system", "Valid", "audit_record_002"]]
    return screen("Sentinel", title="Account events", badge="6 signed events", subtitle="Subject filter: user_03 · 18-26 Aug 2026 UTC",
                  header=AUDIT_HEADER, rows=rows, panel_title="audit_record_003 · Raw event",
                  panel=[("event_type", "account_closure_recorded"), ("subject", "user_03")])


def account_screen(subtitle="user_11 · account_5762") -> list[OcrWord]:
    return screen("Customer Operations", title="Hazel Stone", badge="Silver", subtitle=subtitle,
                  kpis=[("Email", "mk.48297@example.test", 260), ("Phone", "+81 90 5550 1234", 516), ("Market", "JP", 1030)],
                  panel_title="Customer details", panel=[("Default address", "5-24-8 Sakuraoka")])
