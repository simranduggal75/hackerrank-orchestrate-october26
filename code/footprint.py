"""Per-subject resource footprint: which resources are this person's, and what state each is in.

Ownership comes only from evidence that names the subject:

* **record**  - a fact carries one of the subject's identifiers (exact, normalised equality);
* **ocr**     - the same, read from a screenshot and *confirmed* (uncertain reads are never used);
* **trace**   - a log lookup key tied to a single resource by trace id;
* **text**    - the subject's email appears inside free text: a *mention*, not ownership;
* **link_event** - the data catalog explicitly links a resource to the subject's email and user id;
* **source_resource** - a search document names an owned account as its source;
* **alias**   - a screenshot row whose file name equals the alias in a link event for the same system.

Not used, on purpose: naming conventions (``account_001`` for ``user_01``), temporal proximity to the
request (measured at ~21% precision because cases share timestamps), and any search *query* (a query
is context; a lookalike hit must not inherit it). These appear in ``Footprint.unresolved`` as
candidates when the data suggests them.

Time: facts must satisfy the both-times rule. ``link_policy="strict"`` (default) also forbids using a
file captured after ``audit_at`` to learn ownership. ``"record_time"`` allows ownership (never state or
citations) to be learned from a later file whose record is itself stamped by ``audit_at``.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field

from activity import ActivityIndex, IdentityLink
from evidence.facts import Fact, Identifier
from evidence.store import EvidenceStore
from identity import (
    IdentityIndex, Resolution, SubjectIdentifiers, normalize_email, normalize_hash, normalize_phone,
)
from state import ResourceState, reconstruct

KEYED = {
    "email": normalize_email,
    "email_hash": normalize_hash,
    "phone": normalize_phone,
    "user_id": lambda value: value.strip(),
}
POLICIES = ("strict", "record_time")
ALIAS_ATTRIBUTES = ("file_name", "statement")


@dataclass(frozen=True, slots=True)
class OwnerLink:
    resource_id: str | None
    user_id: str
    role: str  # "owner" | "mention"
    via: str  # record | ocr | trace | text | link_event | source_resource | alias
    evidence_id: str | None
    activity_id: str | None
    locator: str
    observed_at: str
    late: bool = False  # learned from a file captured after audit_at (record_time policy only)


@dataclass(frozen=True, slots=True)
class Unresolved:
    kind: str
    resource_id: str | None
    detail: str
    reference: str | None = None  # an evidence or activity id


@dataclass(frozen=True, slots=True)
class OwnedResource:
    resource_id: str
    links: tuple[OwnerLink, ...]
    roles: frozenset[str]  # the subject's roles on this resource
    owners: frozenset[str]  # every user with owner-role evidence on this resource
    shared: bool  # someone else also owns it
    exclusive: bool  # a link event says no other subject is linked
    state: ResourceState


@dataclass(slots=True)
class Footprint:
    user_id: str | None
    as_of: str
    link_policy: str
    resources: dict[str, OwnedResource] = field(default_factory=dict)
    unlinked_facts: list[Fact] = field(default_factory=list)  # name the subject but carry no resource id
    unresolved: list[Unresolved] = field(default_factory=list)

    def of_type(self, record_type: str) -> list[OwnedResource]:
        return [r for r in self.resources.values() if r.state.record_type == record_type]


class FootprintBuilder:
    def __init__(self, store: EvidenceStore, identity: IdentityIndex, activity: ActivityIndex):
        self.store, self.identity, self.activity = store, identity, activity
        self._by_key: dict[tuple[str, str], list[tuple[Fact, Identifier]]] = defaultdict(list)
        for fact in store.facts:
            for ident in fact.identifiers:
                norm = KEYED.get(ident.kind)
                if norm is not None:
                    self._by_key[(ident.kind, norm(ident.value))].append((fact, ident))
        self._by_source: dict[str, list[Fact]] = defaultdict(list)
        for fact in store.facts:
            source = fact.attributes.get("source_resource_id")
            if source:
                self._by_source[source].append(fact)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _keys(subject: SubjectIdentifiers) -> list[tuple[str, str]]:
        keys = [("user_id", subject.user_id)]
        keys += [("email", v.norm) for v in subject.emails]
        keys += [("email_hash", v.norm) for v in subject.email_hashes]
        keys += [("phone", v.norm) for v in subject.phones]
        return keys

    def _users_for(self, ident: Identifier, as_of: str) -> list[str]:
        if ident.kind == "user_id":
            return [ident.value] if ident.value in self.identity.users else []
        return self.identity.find_user(ident.kind, ident.value, as_of=as_of)

    def _owners(self, resource_id: str, facts: list[Fact], as_of: str) -> dict[str, set[str]]:
        owners: dict[str, set[str]] = defaultdict(set)
        for fact in facts:
            for ident in fact.identifiers:
                if ident.kind in KEYED and ident.via != "text":
                    for user in self._users_for(ident, as_of):
                        owners[user].add(ident.via)
        for link in self.activity.links_for_resource(resource_id, as_of):
            owners[link.user_id].add("link_event")
        return owners

    # -------------------------------------------------------------------- build

    def build(self, resolution: Resolution, policy: str = "strict") -> Footprint:
        if policy not in POLICIES:
            raise ValueError(f"link_policy must be one of {POLICIES}")
        subject = resolution.identifiers
        if subject is None:
            return Footprint(None, "", policy, unresolved=[Unresolved("subject_unresolved", None, f"request email {resolution.status}")])
        audit = subject.as_of
        footprint = Footprint(subject.user_id, audit, policy)
        links: dict[str, list[OwnerLink]] = defaultdict(list)
        anchors: set[tuple[str, str, str]] = set()  # (file, system, record type) the subject demonstrably appears in

        # 1. exact identifiers on facts
        seen_facts: set[int] = set()
        late_only: dict[tuple[str, str], Fact] = {}
        for key in self._keys(subject):
            for fact, ident in self._by_key.get(key, ()):
                if ident.kind == "uncertain":
                    continue
                available = fact.available_by(audit)
                late = not available and fact.observed_at <= audit
                role = "mention" if ident.via == "text" else "owner"
                if not (available or (late and policy == "record_time")):
                    if late and fact.resource_id:
                        late_only.setdefault((fact.resource_id, fact.evidence_id), fact)
                    continue
                if role == "owner" and available:
                    anchors.add((fact.evidence_id, fact.system, fact.record_type))
                if fact.resource_id is None:
                    if available and id(fact) not in seen_facts:
                        seen_facts.add(id(fact))
                        footprint.unlinked_facts.append(fact)
                    continue
                links[fact.resource_id].append(OwnerLink(
                    fact.resource_id, subject.user_id, role, ident.via, fact.evidence_id, None,
                    fact.provenance.locator, fact.observed_at, late=late))

        # 2. explicit identity links (and aliases that name a screenshot row)
        emails = {v.norm for v in subject.emails}
        alias_facts: list[Fact] = []
        for link in self.activity.links_for_user(subject.user_id, emails, audit):
            links[link.resource_id].append(OwnerLink(
                link.resource_id, subject.user_id, "owner", "link_event", None, link.activity_id, link.details[:80], link.event_time))
            alias_facts += self._alias_facts(link, audit, footprint, anchors)
        for link in self.activity.later_links_for_user(subject.user_id, emails, audit):
            footprint.unresolved.append(Unresolved(
                "late_link_event", link.resource_id, f"identity link recorded {link.event_time}, after audit_at", link.activity_id))

        for (resource_id, evidence_id), fact in sorted(late_only.items()):
            if resource_id not in links:  # only resources that no allowed evidence links at all
                footprint.unresolved.append(Unresolved(
                    "late_file_link", resource_id,
                    f"the record is stamped {fact.observed_at} but its file was captured {fact.captured_at}, after audit_at; "
                    "it would link this subject under link_policy='record_time'", evidence_id))

        # 3. search documents that name an owned account as their source
        for account in [r for r in list(links) if r.startswith("account_")]:
            for fact in self._by_source.get(account, ()):
                if fact.resource_id and fact.available_by(audit) and fact.resource_id not in links:
                    links[fact.resource_id].append(OwnerLink(
                        fact.resource_id, subject.user_id, "owner", "source_resource", fact.evidence_id, None,
                        fact.provenance.locator, fact.observed_at))
                    anchors.add((fact.evidence_id, fact.system, fact.record_type))

        # 4. state of every linked resource, ambiguity, and what could not be resolved
        coverage = self.store.coverage_at(audit)
        for resource_id, owned_links in sorted(links.items()):
            facts = self.store.facts_for_resource(resource_id, audit) + [f for f in alias_facts if f.resource_id == resource_id]
            usable = tuple(l for l in owned_links if not l.late or policy == "record_time")
            link_system = next((a.system for a in self.activity.links_for_resource(resource_id, audit)), None)
            state = reconstruct(resource_id, facts, coverage, anchors, self.activity.claims_for(resource_id, audit), link_system)
            owners = self._owners(resource_id, facts, audit)
            roles = frozenset(l.role for l in usable)
            if "owner" in roles:
                owners.setdefault(subject.user_id, set()).add("subject")
            shared = bool(set(owners) - {subject.user_id}) and "owner" in roles
            exclusive = any(l.exclusive for l in self.activity.links_for_resource(resource_id, audit))
            footprint.resources[resource_id] = OwnedResource(
                resource_id, usable, roles, frozenset(owners), shared, exclusive, state)
            self._note_resource(footprint, footprint.resources[resource_id], owners)

        self._note_uncertain_and_candidates(footprint, subject, links)
        return footprint

    # ----------------------------------------------------------------- details

    def _alias_facts(self, link: IdentityLink, audit: str, footprint: Footprint, anchors: set[tuple[str, str, str]]) -> list[Fact]:
        """Screenshot/document rows that carry no resource id but whose file name or statement number
        equals this link's alias are treated as observations of the linked resource."""
        if not link.alias:
            return []
        found = []
        for fact in self.store.available_facts(audit):
            if fact.resource_id is not None:
                continue
            if not any(fact.attributes.get(a) == link.alias for a in ALIAS_ATTRIBUTES):
                continue
            if fact.system != link.system:
                footprint.unresolved.append(Unresolved(
                    "alias_other_system", link.resource_id,
                    f"{fact.record_type} '{link.alias}' matches the alias of {link.resource_id}, but it is a {fact.system} record, "
                    f"not {link.system}; not merged", fact.evidence_id))
                continue
            attrs = dict(fact.attributes, resource_id_source=f"alias link {link.activity_id}")
            found.append(dataclasses.replace(fact, resource_id=link.resource_id, attributes=attrs))
            anchors.add((fact.evidence_id, fact.system, fact.record_type))
        return found

    def _note_resource(self, footprint: Footprint, owned: OwnedResource, owners: dict[str, set[str]]) -> None:
        rid, state = owned.resource_id, owned.state
        if owned.shared:
            others = sorted(set(owners) - {footprint.user_id})
            footprint.unresolved.append(Unresolved(
                "ambiguous_owner", rid, f"also tied by exact identifiers/links to {', '.join(others)}"
                + ("; the link event says no other subject is linked" if owned.exclusive else "")))
        if not state.observations:
            footprint.unresolved.append(Unresolved("no_observation", rid, "owned, but no evidence at or before audit_at shows it"))
        if state.provisional and state.observations:
            footprint.unresolved.append(Unresolved("provisional_only", rid, "its state rests only on screenshots, which are provisional"))
        if state.absence_candidates:
            latest = max(state.absence_candidates, key=lambda c: c.observed_at)
            footprint.unresolved.append(Unresolved(
                "absence_unproven", rid,
                f"{len(state.absence_candidates)} newer table dump(s), latest {latest.observed_at} ({latest.store}), lack the "
                "resource but show no record of this subject of that type, so their scope is unproven", latest.evidence_id))
        if state.provisional_conflict:
            footprint.unresolved.append(Unresolved("provisional_contradiction", rid, "; ".join(state.conflicts)))
        elif state.state == "UNKNOWN" and state.conflicts:
            footprint.unresolved.append(Unresolved("stale_conflict", rid, "; ".join(state.conflicts)))
        if owned.roles == frozenset({"mention"}):
            footprint.unresolved.append(Unresolved("mention_only", rid, "the subject's identifier appears in the record's text only"))

    def _note_uncertain_and_candidates(self, footprint: Footprint, subject: SubjectIdentifiers, links) -> None:
        # unconfirmed screenshot identifiers that WOULD match, for the record
        for fact in self.store.available_facts(footprint.as_of):
            for ident in fact.identifiers:
                if ident.kind == "uncertain" and subject.match(ident.value):
                    supported = [l.activity_id for l in self.activity.links_for_resource(fact.resource_id or "", footprint.as_of)
                                 if l.user_id == subject.user_id]
                    footprint.unresolved.append(Unresolved(
                        "uncertain_identifier", fact.resource_id,
                        f"a screenshot reads {ident.value!r} (unconfirmed) which would match; not used"
                        + (f"; independently supported by link event {supported[0]}" if supported else ""), fact.evidence_id))
        # naming-convention candidate: reported, never applied
        digits = subject.user_id.removeprefix("user_")
        if digits.isdigit() and len(digits) <= 2:
            guess = f"account_{int(digits):03d}"
            if guess not in links and self.activity.claims_for(guess, footprint.as_of):
                claim = self.activity.claims_for(guess, footprint.as_of)[0]
                footprint.unresolved.append(Unresolved(
                    "convention_candidate", guess,
                    f"{guess} fits the account_<NNN>/user_<NN> naming pattern and the activity log has a claim "
                    f"({claim.event_type} {claim.result}), but no identifier or link event ties it to this subject", claim.activity_id))
