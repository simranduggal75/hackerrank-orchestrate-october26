"""Does a request authorize deletion?

Grounded in the challenge text: the request must authorize deletion. A request for access, correction,
export, marketing opt-out, or another non-deletion action is NOT_APPLICABLE, even if other fields look
like a deletion case (AGENTS.md, section 6.5).

Rules, applied to the request wording only (no ids, no case data):

A. **Affirmative.** A deletion/erasure/closure expression (a general multilingual lexicon), or a
   first-person assertion that the person's own account was deleted/closed/removed together with a
   request to check, finish or confirm it. Both count as authorization.
B. **Retention and limiters take precedence over the lexicon.** A deletion word that is negated
   ("not asking for deletion", "don't delete"), that keeps the account/profile/history in place
   ("leave the account active", "continue using the service"), or that is scoped to marketing only
   ("remove me from marketing lists only") does not authorize deletion.
C. **Permission to retain specific categories** ("you may keep anonymous statistics", "security records
   can stay if policy requires") is not a limiter: it does not negate the request.
D. Without an affirmative expression the request is not authorized. Conflicting signals are reported
   (``uncertain=True``) so a human can review; the decision then follows rule B.

Activity events never decide a request. ``corroborate`` compares the *number* of wording-classified
non-deletion requests with the number of intake ``CLASSIFY_REQUEST`` events as a global consistency check
(those events name accounts, not cases).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# --- affirmative lexicon (stems; matched on word starts, case-insensitive) --------------------
_LATIN_STEMS = (
    # English
    r"delet", r"eras(e|ed|ing|ure)\b", r"remov", r"wipe", r"close[ds]?\b", r"closing", r"closure", r"forgotten",
    r"is gone\b", r"are gone\b", r"was gone\b", r"no longer exist",
    # Spanish
    r"elimin", r"borr(ar|ad|e\b)", r"suprim", r"suprimir", r"cerrar",
    # French
    r"suppress", r"supprim", r"effac", r"cl[oô]tur", r"fermer",
    # Portuguese
    r"exclu(ir|s[aã]o|a\b)", r"apag", r"remo[cç]", r"remover", r"encerr",
    # German
    r"l[öo]sch", r"entfern",
    # Italian
    r"cancell", r"eliminazione", r"rimuov", r"rimozion",
)
_CJK_TERMS = ("削除", "消去", "退会", "解約", "삭제", "탈퇴", "删除", "刪除", "注销", "註銷")
AFFIRMATIVE = re.compile("|".join([rf"\b(?:{s})" if s[0].isalpha() else s for s in _LATIN_STEMS]
                                   + [re.escape(t) for t in _CJK_TERMS]), re.I)

# --- precedence: retention of the account/profile, negations, scope limiters -------------------
_NEG_VERB = r"(?:delet\w*|eras\w*|remov\w*|clos\w*|wip\w*|chang\w*)"
BLOCKERS = (
    ("negated_deletion", re.compile(
        rf"\b(?:not|n't|never|no)\b[^.!?]{{0,40}}\b{_NEG_VERB}|\bwithout\s+(?:\w+\s+){{0,2}}{_NEG_VERB}"
        rf"|\bnot\s+(?:an?\s+)?(?:request|asking|requesting)\b[^.!?]{{0,40}}\b(?:delet\w*|erase\w*|remov\w*|clos\w*|erasure|deletion)",
        re.I)),
    ("keeps_account", re.compile(
        r"\b(?:keep|leave|retain|preserve|maintain)\b[^.!?]{0,40}\b(?:account|profile|purchase history|order history|history)\b"
        r"[^.!?]{0,25}\b(?:as it is|in place|active|open|intact|untouched|unchanged)\b"
        r"|\b(?:leave|keep)\s+(?:the|my)\s+account\b"
        r"|\bcontinue\s+(?:to\s+)?us(?:e|ing)\b|\bstill\s+(?:need|want|use|using)\b[^.!?]{0,30}\b(?:account|service|profile|history|receipts|it)\b", re.I)),
    ("marketing_only", re.compile(
        r"\b(?:marketing|promotional|promotions?|newsletters?|ads?)\b[^.!?]{0,25}\b(?:only|lists?|emails?|messages?)\b[^.!?]{0,15}\bonly\b"
        r"|\bonly\b[^.!?]{0,25}\b(?:marketing|promotional|newsletters?)\b"
        r"|\b(?:marketing|promotional)\s+lists?\s+only\b|\bstop\s+(?:the\s+)?(?:promotional|marketing)\b|\bunsubscribe\b|\bopt[- ]?out\b", re.I)),
    # other languages: a negation next to a deletion verb
    ("negated_deletion_es_fr_pt", re.compile(
        r"\b(?:no\s+(?:quiero|deseo)|sin|ne\s+(?:veux|souhaite)\s+pas|sans|n[aã]o\s+(?:quero|desejo)|sem)\b[^.!?]{0,30}"
        r"\b(?:elimin\w*|borr\w*|supprim\w*|effac\w*|exclu\w*|apag\w*)", re.I)),
)

# non-deletion topics, used only to explain a negative result
NON_DELETION_TOPICS = (
    ("access_or_copy", re.compile(r"\b(?:copy|download|portable format|what (?:personal )?information|information (?:held|you hold)|send me)\b", re.I)),
    ("correction", re.compile(r"\b(?:correct|misspell|out of date|replace it|change the email|update)\b", re.I)),
    ("login_help", re.compile(r"\b(?:reset|can.?t access|login)\b", re.I)),
    ("invoice", re.compile(r"\binvoices?\b", re.I)),
    ("marketing_opt_out", re.compile(r"\b(?:promotional|marketing|unsubscribe|opt[- ]?out)\b", re.I)),
)


@dataclass(frozen=True, slots=True)
class IntentResult:
    authorized: bool
    basis: str  # "affirmative" | "blocked" | "none"
    affirmative: tuple[str, ...]  # matched deletion expressions
    blockers: tuple[str, ...]  # names of limiter rules that fired
    topics: tuple[str, ...]  # non-deletion topics noticed
    uncertain: bool  # an affirmative expression and a blocker both appear


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?。！？])\s*", text) if s.strip()]


def classify_intent(request_text: str) -> IntentResult:
    """Classify one request from its wording alone."""
    text = _normalise(request_text)
    affirmative: list[str] = []
    blockers: list[str] = []
    for sentence in _sentences(text):
        hits = [m.group(0) for m in AFFIRMATIVE.finditer(sentence)]
        fired = [name for name, rx in BLOCKERS if rx.search(sentence)]
        # A blocker only cancels the affirmative words of ITS OWN sentence, so a later sentence that
        # asks for deletion still counts ("... leave my profile ... Please delete my account.").
        if hits and not fired:
            affirmative += hits
        blockers += fired
    # blockers in sentences that also contain deletion words are the interesting ones
    if affirmative:
        return IntentResult(True, "affirmative", tuple(dict.fromkeys(affirmative)), tuple(dict.fromkeys(blockers)),
                            (), uncertain=bool(blockers))
    topics = tuple(name for name, rx in NON_DELETION_TOPICS if rx.search(text))
    basis = "blocked" if blockers or AFFIRMATIVE.search(text) else "none"
    return IntentResult(False, basis, (), tuple(dict.fromkeys(blockers)), topics, uncertain=False)


def corroborate(results: dict[str, IntentResult], classify_events: int) -> dict:
    """Global consistency check only: wording says N non-deletion requests; intake logged M events."""
    unauthorized = sum(1 for r in results.values() if not r.authorized)
    return {"wording_non_deletion": unauthorized, "intake_classify_events": classify_events,
            "consistent": unauthorized == classify_events}
