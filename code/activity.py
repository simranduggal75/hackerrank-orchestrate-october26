"""System-activity links and claims.

``system_activity.csv`` mixes three very different things, and this module keeps them apart:

* **Identity links** (``LINK_RESOURCE_IDENTITY``): the data catalog explicitly states that a resource
  belongs to an email and a user id. These are the only activity rows that can establish ownership.
* **Claims**: everything else (deletion-worker results, syncs, cache refreshes, ...). A claim says what
  a process *reported*. It never changes a resource's state and never links a resource to a person.
* **Classifications** (``CLASSIFY_REQUEST``): intake decided a request was not a deletion request.

Activity has no subject column, and events from different people interleave in time, so neither
timestamps nor row order are used to guess whose a resource is.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from identity import normalize_email
from loaders import Activity

_LINK_RE = re.compile(
    r"(?P<resource>[a-z_]+_\d+) \((?P<alias>[^)]*)\) (?P<verb>to|belongs only to) "
    r"(?P<email>[^\s]+?) and (?P<user>user_\d+)\b")

RECREATION_TYPES = frozenset({"RESTORE_BACKUP"})


@dataclass(frozen=True, slots=True)
class IdentityLink:
    activity_id: str
    event_time: str
    resource_id: str
    system: str  # the activity's target_system
    alias: str | None  # the name in brackets, e.g. a file name or a statement number
    email: str  # normalised
    user_id: str
    exclusive: bool  # the catalog says no other subject is linked
    details: str


@dataclass(frozen=True, slots=True)
class ActivityClaim:
    activity_id: str
    event_time: str
    event_type: str
    source_system: str
    target_system: str
    resource_id: str
    result: str  # SUCCESS | FAILED | PARTIAL, as reported
    kind: str  # worker | check | recreation | replication | classification | other
    details: str


def parse_identity_links(activity: list[Activity]) -> tuple[list[IdentityLink], list[str]]:
    """Explicit identity links, plus a message for every link row that could not be read."""
    links, problems = [], []
    for row in activity:
        if row.event_type != "LINK_RESOURCE_IDENTITY":
            continue
        match = _LINK_RE.search(row.details)
        if match is None or match.group("resource") != row.resource_id:
            problems.append(f"{row.activity_id}: identity link not understood: {row.details[:80]!r}")
            continue
        alias = match.group("alias").strip()
        links.append(IdentityLink(
            activity_id=row.activity_id, event_time=row.event_time, resource_id=row.resource_id,
            system=row.target_system, alias=None if not alias or alias == row.resource_id else alias,
            email=normalize_email(match.group("email").rstrip(".;,")), user_id=match.group("user"),
            exclusive="no other subject is linked" in row.details, details=row.details))
    return links, problems


def _kind(row: Activity) -> str:
    if row.event_type == "CLASSIFY_REQUEST":
        return "classification"
    if row.event_type in RECREATION_TYPES or "repopulated" in row.details:
        return "recreation"
    if row.target_system in ("account_replication_cluster", "account_failover_replica"):
        return "replication"
    if row.event_type.startswith("CHECK_"):
        return "check"
    if row.source_system == "deletion_worker":
        return "worker"
    return "other"


class ActivityIndex:
    """Identity links and per-resource claims, filterable by time."""

    def __init__(self, activity: list[Activity]):
        self.links, self.problems = parse_identity_links(activity)
        self._claims: dict[str, list[ActivityClaim]] = defaultdict(list)
        for row in activity:
            if row.event_type == "LINK_RESOURCE_IDENTITY":
                continue
            self._claims[row.resource_id].append(ActivityClaim(
                row.activity_id, row.event_time, row.event_type, row.source_system, row.target_system,
                row.resource_id, row.reported_result, _kind(row), row.details))
        for claims in self._claims.values():
            claims.sort(key=lambda c: (c.event_time, c.activity_id))
        self.resources = frozenset(self._claims) | frozenset(link.resource_id for link in self.links)

    def claims_for(self, resource_id: str, as_of: str) -> list[ActivityClaim]:
        """Claims reported at or before ``as_of`` (a claim is never evidence of state)."""
        return [c for c in self._claims.get(resource_id, ()) if c.event_time <= as_of]

    def links_for_user(self, user_id: str, emails: set[str], as_of: str) -> list[IdentityLink]:
        """Links naming this user id or one of these (normalised) emails, recorded by ``as_of``."""
        return [l for l in self.links if l.event_time <= as_of and (l.user_id == user_id or l.email in emails)]

    def links_for_resource(self, resource_id: str, as_of: str) -> list[IdentityLink]:
        return [l for l in self.links if l.resource_id == resource_id and l.event_time <= as_of]

    def later_links_for_user(self, user_id: str, emails: set[str], as_of: str) -> list[IdentityLink]:
        """Links recorded after ``as_of``: unusable for this case, reported for transparency."""
        return [l for l in self.links if l.event_time > as_of and (l.user_id == user_id or l.email in emails)]
