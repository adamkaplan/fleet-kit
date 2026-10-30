"""Slice D of #1215: the pure rules that read an impact brief (see `bin/impact_brief.py`, slice A) for the advisory case, the Decisions row
and the held-body rationale line. No I/O. Python 3.9 standard library only."""
import re

RATIONALE_PREFIX = "Rationale:"
_OPENING = re.compile(r"^(?:PLAN|MERGE|canon|CLAIMS):", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def is_advisory(brief):
    """True only for a brief that says decider chief_of_staff AND an empty material list. Anything else (None, a missing key, a non-empty list) is not advisory: fail closed."""
    return (isinstance(brief, dict) and brief.get("decider") == "chief_of_staff"
            and isinstance(brief.get("material"), list) and brief.get("material") == [])


def owner_hold_wanted(brief):
    """Whether the daemon may write an owner-hold label for a PR the floor holds: not for an advisory brief."""
    return not is_advisory(brief)


def why_sentence(brief):
    """The brief's ONE `why` sentence, or None when it is missing or is not one sentence."""
    why = brief.get("why") if isinstance(brief, dict) else None
    if not isinstance(why, str) or not why.strip():
        return None
    return why.strip() if len(_SENTENCE_END.split(why.strip())) == 1 else None


def decision_row_fields(brief):
    """What the Decisions row adds from a brief: {"rationale": why, "advisory": bool, "note": text}; {} without a readable brief."""
    why = why_sentence(brief)
    if why is None:
        return {}
    advisory = is_advisory(brief)
    lead = "advisory for the Chief of Staff: " if advisory else "for the owner: "
    return {"rationale": why, "advisory": advisory, "note": lead + why}


def rationale_defects(lines):
    """G6b: the first non-blank line after the opening lines (PLAN:, MERGE:, canon:) of a held body is `Rationale: <ONE sentence>`.
    Returns [] or ONE defect naming "rationale"."""
    at = 0
    while at < len(lines) and (_OPENING.match(lines[at]) or not lines[at].strip()):
        at += 1
    first = lines[at].strip() if at < len(lines) else ""
    text = first[len(RATIONALE_PREFIX):].strip() if first.startswith(RATIONALE_PREFIX) else ""
    if not text or len(re.findall(r"[^\W_]", text)) < 3 or len(_SENTENCE_END.split(text)) != 1:
        return ["the held PR needs a one-sentence rationale line on top: `%s <one sentence>` right after the opening lines" % RATIONALE_PREFIX]
    return []
