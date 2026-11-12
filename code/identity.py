"""Identifier normalisation and current/historical identity resolution.

The identity snapshot says what a user looks like now. ``user_audit_log`` says
how each field got there. Together they give, for every user and field, a
timeline of values with the time each one was introduced and retired. Looking
at that timeline *as of* a case's ``audit_at`` means identity changes made after
the audit cutoff are never used.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from loaders import AuditEvent, Identity, Request

FIELDS = ("email", "email_hash", "phone")

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_USER_ID_RE = re.compile(r"^user_\d+$")


# --------------------------------------------------------------------- normalisation


def normalize_email(value: str) -> str:
    """Case-insensitive, whitespace-trimmed, Unicode-normalised email key."""
    return unicodedata.normalize("NFKC", value).strip().lower()


def normalize_phone(value: str) -> str:
    """Keep a leading ``+`` and the digits; drop spaces, dashes and brackets."""
    text = unicodedata.normalize("NFKC", value).strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    return ("+" if text.startswith("+") else "") + digits


def normalize_hash(value: str) -> str:
    return value.strip().lower()


def email_hash(email: str) -> str:
    """SHA-256 of the normalised email; matches ``user_identities.email_hash``."""
    return hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()


_NORMALIZERS = {"email": normalize_email, "email_hash": normalize_hash, "phone": normalize_phone}


def _natural_key(identifier: str) -> tuple:
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"(\d+)", identifier))


# ------------------------------------------------------------------------ timelines


@dataclass(frozen=True, slots=True)
class Version:
    """One value of one field. ``start``/``end`` are None when unbounded."""

    raw: str
    norm: str
    start: str | None  # when this value was introduced (inclusive)
    end: str | None  # when it was replaced (exclusive)
    introduced_by: str | None  # audit_id that set it, None if it predates the log
    retired_by: str | None  # audit_id that replaced it, None if still in force

    def active_at(self, when: str) -> bool:
        return (self.start is None or self.start <= when) and (self.end is None or when < self.end)

    def known_by(self, when: str) -> bool:
        """The value had come into existence at or before ``when``."""
        return self.start is None or self.start <= when


def _build_timeline(
    field: str, snapshot_value: str, events: list[AuditEvent], issues: list[str], user_id: str
) -> list[Version]:
    norm = _NORMALIZERS[field]
    if not events:
        return [Version(snapshot_value, norm(snapshot_value), None, None, None, None)]
    events = sorted(events, key=lambda e: (e.event_time, _natural_key(e.audit_id)))
    versions: list[Version] = []
    previous_value, previous_start, previous_by = events[0].old_value, None, None
    for event in events:
        if norm(event.old_value) != norm(previous_value):
            issues.append(f"{user_id}.{field}: {event.audit_id} old_value does not follow the previous change")
        versions.append(Version(previous_value, norm(previous_value), previous_start,
                                event.event_time, previous_by, event.audit_id))
        previous_value, previous_start, previous_by = event.new_value, event.event_time, event.audit_id
    versions.append(Version(previous_value, norm(previous_value), previous_start, None, previous_by, None))
    if norm(previous_value) != norm(snapshot_value):
        issues.append(f"{user_id}.{field}: last audit value differs from the identity snapshot")
    return versions


# --------------------------------------------------------------------- resolved views


@dataclass(frozen=True, slots=True)
class SubjectIdentifiers:
    """Every identifier a user had at or before ``as_of``, with validity windows."""

    user_id: str
    name: str
    as_of: str
    emails: tuple[Version, ...]
    email_hashes: tuple[Version, ...]
    phones: tuple[Version, ...]
    current: dict  # field -> value in force at as_of

    def match(self, value: str) -> str | None:
        """Classify an identifier seen in evidence: user_id, email, email_hash, phone or None.

        Exact (normalised) comparison only. Names and partial strings never match,
        so lookalike emails such as ``x.jr@example.test`` do not hit ``x@example.test``.
        """
        text = value.strip()
        if _USER_ID_RE.match(text):
            return "user_id" if text == self.user_id else None
        if "@" in text:
            norm = normalize_email(text)
            return "email" if any(v.norm == norm for v in self.emails) else None
        if _HEX64_RE.match(normalize_hash(text)):
            norm = normalize_hash(text)
            return "email_hash" if any(v.norm == norm for v in self.email_hashes) else None
        if text.startswith("+") or any(ch.isdigit() for ch in text):
            norm = normalize_phone(text)
            return "phone" if norm and any(v.norm == norm for v in self.phones) else None
        return None


@dataclass(frozen=True, slots=True)
class Resolution:
    """Outcome of resolving one request's email.

    status is one of:
      CURRENT      the email was the user's live email at audit_at
      HISTORICAL   the email belonged to the user earlier and had been replaced by audit_at
      FUTURE_ONLY  the email only came into use after audit_at (not usable for this case)
      AMBIGUOUS    the email maps to more than one user
      UNRESOLVED   the email is unknown
    """

    case_id: str
    status: str
    user_id: str | None
    request_email: str
    matched: Version | None
    identifiers: SubjectIdentifiers | None
    notes: tuple[str, ...]


class IdentityIndex:
    def __init__(self, identities: Iterable[Identity], audit_log: Iterable[AuditEvent]):
        audit_log = list(audit_log)
        self.issues: list[str] = []
        self.users: dict[str, Identity] = {i.user_id: i for i in identities}
        events: dict[tuple[str, str], list[AuditEvent]] = defaultdict(list)
        for event in audit_log:
            events[(event.user_id, event.field_name)].append(event)
        self._timelines: dict[str, dict[str, list[Version]]] = {}
        self._lookup: dict[str, dict[str, list[tuple[str, Version]]]] = {f: defaultdict(list) for f in FIELDS}
        for user_id, ident in self.users.items():
            snapshot = {"email": ident.email, "email_hash": ident.email_hash, "phone": ident.phone}
            self._timelines[user_id] = {
                f: _build_timeline(f, snapshot[f], events.get((user_id, f), []), self.issues, user_id)
                for f in FIELDS
            }
            for f in FIELDS:
                for version in self._timelines[user_id][f]:
                    self._lookup[f][version.norm].append((user_id, version))
            derived = email_hash(self._timelines[user_id]["email"][-1].raw)
            if derived != normalize_hash(ident.email_hash):
                self.issues.append(f"{user_id}: email_hash is not sha256 of the current email")
        for event in audit_log:
            if event.user_id not in self.users:
                self.issues.append(f"{event.audit_id}: unknown user_id {event.user_id}")
        for norm, owners in self._lookup["email"].items():
            if len({u for u, _ in owners}) > 1:
                self.issues.append(f"email {norm} belongs to more than one user over time")

    # ------------------------------------------------------------------ lookups

    def timeline(self, user_id: str, field: str) -> tuple[Version, ...]:
        return tuple(self._timelines[user_id][field])

    def snapshot_at(self, user_id: str, as_of: str) -> dict:
        """Field values in force at ``as_of`` (later audit-log changes are unwound)."""
        values = {"user_id": user_id, "name": self.users[user_id].name}
        for f in FIELDS:
            active = [v for v in self._timelines[user_id][f] if v.active_at(as_of)]
            values[f] = active[-1].raw if active else None
        return values

    def identifiers(self, user_id: str, as_of: str) -> SubjectIdentifiers:
        def known(field: str) -> tuple[Version, ...]:
            return tuple(v for v in self._timelines[user_id][field] if v.known_by(as_of))

        return SubjectIdentifiers(
            user_id=user_id,
            name=self.users[user_id].name,
            as_of=as_of,
            emails=known("email"),
            email_hashes=known("email_hash"),
            phones=known("phone"),
            current=self.snapshot_at(user_id, as_of),
        )

    def find_user(self, field: str, value: str, as_of: str | None = None) -> list[str]:
        """User ids that ever held ``value`` for ``field`` (optionally only by ``as_of``)."""
        norm = _NORMALIZERS[field](value)
        owners = self._lookup[field].get(norm, [])
        return sorted({u for u, v in owners if as_of is None or v.known_by(as_of)})

    # --------------------------------------------------------------- resolution

    def resolve_email(self, email: str, as_of: str, case_id: str = "") -> Resolution:
        norm = normalize_email(email)
        owners = self._lookup["email"].get(norm, [])
        known = [(u, v) for u, v in owners if v.known_by(as_of)]
        notes: list[str] = []
        if email != norm:
            notes.append("request email normalised (case/whitespace)")
        if not owners:
            return Resolution(case_id, "UNRESOLVED", None, email, None, None, tuple(notes))
        if not known:
            users = sorted({u for u, _ in owners})
            notes.append(f"email first used after audit_at by {', '.join(users)}")
            return Resolution(case_id, "FUTURE_ONLY", None, email, None, None, tuple(notes))
        users = sorted({u for u, _ in known})
        if len(users) > 1:
            notes.append(f"email maps to several users: {', '.join(users)}")
            return Resolution(case_id, "AMBIGUOUS", None, email, None, None, tuple(notes))
        user_id = users[0]
        versions = [v for u, v in known if u == user_id]
        live = [v for v in versions if v.active_at(as_of)]
        matched = live[-1] if live else versions[-1]
        status = "CURRENT" if live else "HISTORICAL"
        if status == "HISTORICAL":
            notes.append(f"email retired at {matched.end} by {matched.retired_by}")
        later = {
            v.introduced_by for f in FIELDS for v in self._timelines[user_id][f]
            if v.start is not None and v.start > as_of
        }
        if later:
            notes.append(f"{len(later)} later identity change(s) ignored (after audit_at)")
        return Resolution(case_id, status, user_id, email, matched,
                          self.identifiers(user_id, as_of), tuple(notes))

    def resolve_request(self, request: Request) -> Resolution:
        """Resolve a request against the identities valid at its audit_at."""
        return self.resolve_email(request.email, request.audit_at, request.case_id)
