"""Reconstruct one resource's state as of ``audit_at`` from evidence.

Inputs are facts already filtered by the both-times rule, coverage entries, and activity claims.
This module only *describes* what the evidence supports. It applies no deletion policy.

Rules (each is deliberately conservative):

1. **Observations.** Every fact about the resource is an observation from a *store*: the primary
   system (all non-replica sources share one role, so a later observation supersedes an earlier one),
   or a named replica (read_replica, failover_replica, ...). Per store, the latest observation wins.
2. **Absence is proven only two ways.**
   * explicit: a lookup said NOT_FOUND for this resource (404, ``rows=0``, a NOT_FOUND row); or
   * table dump: a SQL table of the right system and record type, captured by ``audit_at``, does not
     contain the resource, *and* the same file shows that table family is populated for this subject
     (the subject has a record of that same system and type in the file, e.g. in a replica sibling).
     A dump that never shows the subject's records of that type might simply be out of scope for them,
     so its silence is reported as an unproven candidate, not as absence.
   JSON collection listings, logs and screenshots never prove absence by omission.
3. **Several stores.** If a store still shows the resource while another reports it absent, the copy
   remains (replication drift) provided the positive observation is not older than the absence.
   A positive that is *older* than an absence reported by another store is stale: nobody re-checked
   it, so the state is UNKNOWN and the conflict is kept.
4. **Worst case.** When fresh stores show different non-absent states, the most exposed one is reported
   and the others are listed as conflicts (``EXPOSURE``).
5. **Provisional evidence.** Screenshot facts are provisional. They decide a state only when nothing
   else observed the resource, and the result is then flagged ``provisional``. A screenshot taken
   *after* the firm evidence that contradicts it (e.g. a record that a lookup reported absent is
   displayed again) cannot be ignored and cannot be believed alone: the state becomes UNKNOWN and the
   contradiction is kept (``provisional_conflict``). A screenshot that merely adds detail ("exists",
   when the firm evidence already says BLOCKED) is compatible and changes nothing.
6. **Claims never count.** Activity-log results ("SUCCESS", "completed") are attached for context only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from activity import ActivityClaim
from evidence.facts import Coverage, Fact

# Most exposed first. Anything not listed ranks last.
EXPOSURE = {
    "ANALYTICS_PROFILE": ("IDENTIFIABLE", "ANONYMIZED", "NOT_FOUND"),
    "SUPPORT_MESSAGE": ("IDENTIFIABLE", "NO_DIRECT_IDENTIFIER", "REDACTED", "NOT_FOUND"),
    "ACCOUNT_TOMBSTONE": ("IDENTIFIABLE", "REDACTED", "NOT_FOUND"),
    "DATA_FLOW": ("ACTIVE", "DISABLED", "NOT_FOUND"),
    "BACKUP_ARCHIVE": ("ACTIVE", "FOUND", "BLOCKED", "NOT_FOUND"),
}
DEFAULT_EXPOSURE = ("FOUND", "NOT_FOUND")

# resource-id prefix -> (system, record type); a type of None means the prefix is ambiguous
PREFIX_TYPES = {
    "account": ("account_service", "USER_PROFILE"),
    "search": ("search_service", "SEARCH_INDEX_ENTRY"),
    "analytics": ("analytics_platform", "ANALYTICS_PROFILE"),
    "export": ("export_service", "USER_EXPORT"),
    "data_flow": ("integration_service", "DATA_FLOW"),
    "backup": ("backup_service", "BACKUP_ARCHIVE"),
    "audit_record": ("security_service", "SECURITY_AUDIT_LOG"),
    "tombstone": ("identity_service", "ACCOUNT_TOMBSTONE"),
    "legal_hold": ("legal_service", "LEGAL_HOLD_RECORD"),
    "marketing": ("marketing_platform", "MARKETING_PROFILE"),
    "support": ("support_platform", None),  # a message or an attachment
    "object": ("object_storage", None),
    "cache": ("cache_service", "CACHE_ENTRY"),
    "email_delivery": ("email_delivery_service", "EMAIL_DELIVERY_LOG"),
}


def infer_type(resource_id: str) -> tuple[str | None, str | None]:
    prefix = resource_id.rsplit("_", 1)[0]
    return PREFIX_TYPES.get(prefix, (None, None))


def role_of_store(store: str | None) -> str:
    """'primary' for the live system; the replica's own name otherwise."""
    if not store or store.endswith("primary") or "replica" not in store:
        return "primary"
    return store.removeprefix("customer_data_").removeprefix("customer_profiles_")


@dataclass(frozen=True, slots=True)
class Observation:
    role: str
    observed_at: str
    captured_at: str
    state: str
    kind: str  # "fact" | "table_absence"
    evidence_id: str
    locator: str
    parser: str
    provisional: bool
    store: str | None


@dataclass(frozen=True, slots=True)
class AbsenceCandidate:
    """A table dump that lacks the resource but cannot be shown to concern this subject."""

    evidence_id: str
    store: str
    observed_at: str
    reason: str


@dataclass(frozen=True, slots=True)
class ResourceState:
    resource_id: str
    system: str | None
    record_type: str | None
    state: str  # a fact state, or UNKNOWN
    basis: str
    provisional: bool
    absence: str | None  # "explicit" | "table_dump" when state is NOT_FOUND
    observations: tuple[Observation, ...]
    conflicts: tuple[str, ...]
    claims: tuple[ActivityClaim, ...]
    absence_candidates: tuple[AbsenceCandidate, ...] = ()
    notes: tuple[str, ...] = field(default=())
    provisional_conflict: bool = False  # a later screenshot contradicts the firm evidence

    @property
    def exists(self) -> bool | None:
        if self.state == "UNKNOWN":
            return None
        return self.state != "NOT_FOUND"


def _rank(record_type: str | None, state: str) -> int:
    order = EXPOSURE.get(record_type or "", DEFAULT_EXPOSURE)
    return order.index(state) if state in order else len(order)


def _from_fact(fact: Fact) -> Observation:
    role = fact.attributes.get("replica_role") or role_of_store(fact.store)
    return Observation(
        role=role if role != "" else "primary", observed_at=fact.observed_at, captured_at=fact.captured_at,
        state=fact.state, kind="fact", evidence_id=fact.evidence_id, locator=fact.provenance.locator,
        parser=fact.provenance.parser, provisional=bool(fact.attributes.get("provisional")), store=fact.store)


def table_absences(
    resource_id: str, system: str | None, record_type: str | None, facts: list[Fact],
    coverage: list[Coverage], anchors: set[tuple[str, str, str]],
) -> tuple[list[Observation], list[AbsenceCandidate]]:
    """Absence observations from SQL tables that cover this system and type but lack the resource."""
    if system is None or record_type is None:
        return [], []
    present_in = {(f.evidence_id, f.store) for f in facts}
    observations, candidates = [], []
    for cov in coverage:
        if cov.provenance.parser != "sql" or cov.system != system or cov.record_type != record_type:
            continue
        if (cov.evidence_id, cov.store) in present_in:
            continue
        if (cov.evidence_id, system, record_type) in anchors:
            observations.append(Observation(
                role=role_of_store(cov.store), observed_at=cov.observed_at, captured_at=cov.captured_at,
                state="NOT_FOUND", kind="table_absence", evidence_id=cov.evidence_id,
                locator=f"table {cov.store} (no row for {resource_id})", parser="sql", provisional=False, store=cov.store))
        else:
            candidates.append(AbsenceCandidate(
                cov.evidence_id, cov.store, cov.observed_at,
                "the table lacks the resource, but the file shows no record of this subject of that type, so its scope is unproven"))
    return observations, candidates


def _contradicts(firm_state: str, provisional_state: str) -> bool:
    """Does a (later) screenshot state disagree with the firm state?"""
    if firm_state == provisional_state:
        return False
    if firm_state == "NOT_FOUND" or provisional_state == "NOT_FOUND":
        return True
    return provisional_state != "FOUND"  # "exists" is compatible with any more specific live state


def aggregate(record_type: str | None, observations: list[Observation]) -> tuple[str, str, list[str], bool, str | None, list[str], bool]:
    """-> (state, basis, conflicts, provisional, absence kind, notes, provisional_conflict)"""
    result = _aggregate(record_type, observations)
    state, basis, conflicts, provisional, absence, notes = result
    firm = [o for o in observations if not o.provisional]
    if firm and state != "UNKNOWN":
        newest_firm = max(o.observed_at for o in firm)
        later = [o for o in observations if o.provisional and o.observed_at > newest_firm and _contradicts(state, o.state)]
        if later:
            conflicts = list(conflicts) + [
                f"a screenshot at {o.observed_at} shows {o.state} ({o.evidence_id}), contradicting the firm evidence ({state})" for o in later]
            return ("UNKNOWN", "a later screenshot contradicts the firm evidence and screenshots are provisional",
                    conflicts, False, None, notes, True)
    return state, basis, conflicts, provisional, absence, notes, False


def _aggregate(record_type: str | None, observations: list[Observation]) -> tuple[str, str, list[str], bool, str | None, list[str]]:
    notes: list[str] = []
    firm = [o for o in observations if not o.provisional]
    used = firm or observations
    provisional = bool(observations) and not firm
    if firm and len(firm) < len(observations):
        notes.append(f"{len(observations) - len(firm)} screenshot observation(s) disregarded for state (provisional)")
    if not used:
        return "UNKNOWN", "no usable observation at or before audit_at", [], False, None, notes

    per_role: dict[str, tuple[str, str]] = {}  # role -> (observed_at, state)
    for role in sorted({o.role for o in used}):
        mine = [o for o in used if o.role == role]
        newest = max(o.observed_at for o in mine)
        states = sorted({o.state for o in mine if o.observed_at == newest}, key=lambda s: _rank(record_type, s))
        known = [s for s in states if s != "UNKNOWN"]
        per_role[role] = (newest, (known or states)[0])
        if len(known) > 1:
            notes.append(f"{role}: observations at {newest} disagree ({', '.join(known)}); the most exposed is used")

    informative = {r: v for r, v in per_role.items() if v[1] != "UNKNOWN"}
    if not informative:
        return "UNKNOWN", "every observation is itself UNKNOWN", [], provisional, None, notes
    negatives = {r: v for r, v in informative.items() if v[1] == "NOT_FOUND"}
    positives = {r: v for r, v in informative.items() if v[1] != "NOT_FOUND"}
    newest_negative = max((v[0] for v in negatives.values()), default=None)
    fresh = {r: v for r, v in positives.items() if newest_negative is None or v[0] >= newest_negative}
    stale = {r: v for r, v in positives.items() if r not in fresh}

    conflicts: list[str] = [f"{r} reports absent at {t}" for r, (t, _) in sorted(negatives.items()) if fresh]
    conflicts += [f"{r} last showed {s} at {t}, older than an absence reported at {newest_negative}" for r, (t, s) in sorted(stale.items())]

    if fresh:
        worst = min((v[1] for v in fresh.values()), key=lambda s: _rank(record_type, s))
        others = [f"{r} shows {s}" for r, (_, s) in sorted(fresh.items()) if s != worst]
        conflicts += others
        basis = (f"latest observation per store; {len(fresh)} store(s) still show it"
                 + ("; most exposed state reported" if others else ""))
        return worst, basis, conflicts, provisional, None, notes
    if stale:
        return "UNKNOWN", "positive observations are older than an absence from another store", conflicts, provisional, None, notes
    kinds = {o.kind for o in used if o.state == "NOT_FOUND"}
    return "NOT_FOUND", "every store that observed the resource reports it absent", [], provisional, \
        ("explicit" if "fact" in kinds else "table_dump"), notes


def reconstruct(
    resource_id: str, facts: list[Fact], coverage: list[Coverage], anchors: set[tuple[str, str, str]],
    claims: list[ActivityClaim], system_hint: str | None = None,
) -> ResourceState:
    """State of ``resource_id`` from facts/coverage that already satisfy the time rule."""
    system = next((f.system for f in facts), None) or system_hint or infer_type(resource_id)[0]
    record_type = next((f.record_type for f in facts), None) or infer_type(resource_id)[1]
    observations = [_from_fact(f) for f in facts]
    extra, candidates = table_absences(resource_id, system, record_type, facts, coverage, anchors)
    observations += extra
    state, basis, conflicts, provisional, absence, notes, provisional_conflict = aggregate(record_type, observations)
    # An unproven dump matters only if it is newer than the latest real sighting (an older one says
    # nothing about later removal) and the state is not already established as absent.
    newest = max((o.observed_at for o in observations if o.kind == "fact"), default="")
    candidates = [c for c in candidates if c.observed_at > newest] if state != "NOT_FOUND" else []
    if candidates:
        notes.append(f"{len(candidates)} newer table dump(s) lack the resource but their scope is unproven; not counted as absence")
    return ResourceState(
        resource_id=resource_id, system=system, record_type=record_type, state=state, basis=basis,
        provisional=provisional, absence=absence,
        observations=tuple(sorted(observations, key=lambda o: (o.observed_at, o.role))),
        conflicts=tuple(conflicts), claims=tuple(claims), absence_candidates=tuple(candidates), notes=tuple(notes),
        provisional_conflict=provisional_conflict)
