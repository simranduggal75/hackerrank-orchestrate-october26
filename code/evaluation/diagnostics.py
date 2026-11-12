"""Diagnostics only: why gate G1 (account established and proven absent) fails, by the account evidence that exists.

Nothing here feeds a decision. It classifies a failure so a policy discussion can rest on counts of evidence
situations rather than on individual cases. The classes are ordered from most to least informative:

  account_owned_but_not_proven_absent   an account record is linked to the subject but its absence is not proven
  late_file_link                        the only account evidence that names the subject is in a file captured after audit_at
  same_file_empty_collection            a file that also holds the subject's own records reports an account COLLECTION (a table or
                                        record set) with zero rows
  same_file_collection_without_subject  such a file reports a non-empty account collection with no row for the subject
  same_file_lookup_only                 such a file reports only per-resource LOOKUPS (``profile_lookups``: one answer per resource
                                        id asked about). A lookup says nothing about resources it was not asked for, so the
                                        subject's absence from it proves nothing
  no_account_evidence                   no in-time file that holds the subject's records reports account evidence at all
"""

from __future__ import annotations

ACCOUNT_OWNED = "account_owned_but_not_proven_absent"
LATE_FILE = "late_file_link"
EMPTY_STORE = "same_file_empty_collection"
STORE_WITHOUT_SUBJECT = "same_file_collection_without_subject"
LOOKUP_ONLY = "same_file_lookup_only"
NO_EVIDENCE = "no_account_evidence"

CAUSES = (ACCOUNT_OWNED, LATE_FILE, EMPTY_STORE, STORE_WITHOUT_SUBJECT, LOOKUP_ONLY, NO_EVIDENCE)


def is_lookup_store(store: str) -> bool:
    """A per-resource lookup export rather than a collection snapshot (the JSON key is ``profile_lookups``)."""
    return "lookup" in store.lower()


def g1_cause(assessment, footprint, store) -> str | None:
    """None when G1 passes; otherwise the evidence situation behind the failure."""
    if all(g.passed for g in assessment.gates if g.gate == "G1"):
        return None
    if any(v.record_type == "USER_PROFILE" for v in assessment.verdicts):
        return ACCOUNT_OWNED
    if any(u.kind == "late_file_link" for u in footprint.unresolved):
        return LATE_FILE
    subject_files = {o.evidence_id for owned in footprint.resources.values() for o in owned.state.observations}
    account_stores = [cv for result in store.results.values() for cv in result.coverage
                      if cv.record_type == "USER_PROFILE" and cv.evidence_id in subject_files
                      and cv.captured_at <= assessment.audit_at and cv.observed_at <= assessment.audit_at]
    collections = [cv for cv in account_stores if not is_lookup_store(cv.store)]
    if any(cv.row_count == 0 for cv in collections):
        return EMPTY_STORE
    if collections:
        return STORE_WITHOUT_SUBJECT
    return LOOKUP_ONLY if account_stores else NO_EVIDENCE
