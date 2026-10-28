"""The check of an approval sent as a message (#951). Pure: it reads nothing and writes nothing.

`Approved at sha256:X` is recorded only when X is the current hash of the issue's one plan comment AND the plan's declared
clauses are the issue's current Done-when. `evaluate` returns the verdict; the caller reads GitHub (GET only) and audits.
"""
import hashlib
import re

APPROVAL_RE = re.compile(r"^\s*Approved at sha256:(\S*)", re.IGNORECASE)
HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def approval_value(text):
    """The text after `Approved at sha256:` (may be empty) when the message starts with it, else None (no approval)."""
    found = APPROVAL_RE.match(text or "")
    return None if found is None else found.group(1).rstrip(".,;:")


ANYWHERE_RE = re.compile(r"Approved at sha256:([0-9a-fA-F]{64})(?![0-9a-fA-F])", re.IGNORECASE)
LIST_RE = re.compile(r"(?<![\w/])(?:([\w.-]+/[\w.-]+))?#(\d+) at sha256:([0-9a-fA-F]{64})(?![0-9a-fA-F])")
MENTION_RE = re.compile(r"(?<![\w/&])(?:([\w.-]+/[\w.-]+))?#(\d+)\b")
APPROVED_COLON_RE = re.compile(r"Approved:", re.IGNORECASE)
MAX_CANDIDATES = 10


def find_approvals(text):
    """(#1463) Every approval in a message, each hash once, in order: the value of a message that STARTS with `Approved at sha256:` (as
    before, even when it is not a full hash), every `Approved at sha256:<64 hex>` anywhere, and every `#N at sha256:<64 hex>` (or
    `owner/repo#N ...`) after `Approved:`. Returns [{"value": hash, "ref": (repo or None, N) or None}]."""
    text = text or ""
    found, seen = [], set()

    def add(value, ref):
        key = value.lower()
        if key not in seen:
            seen.add(key)
            found.append({"value": key if HEX64_RE.match(value) else value, "ref": ref})
    start = approval_value(text)
    if start is not None:
        add(start, None)
    for _, value, ref in _sightings(text)[0]:
        add(value, ref)
    return found


def _sightings(text):
    """(clean, refused): every form found, as (position, value, ref), split by `refusal_of`; the start-anchored form is a form too."""
    text = text or ""
    spans = []
    lead = APPROVAL_RE.match(text)
    if lead is not None and approval_value(text) is not None:
        spans.append((lead.start(1) - len("Approved at sha256:"), approval_value(text), None))
    spans += [(m.start(), m.group(1), None) for m in ANYWHERE_RE.finditer(text)]
    colon = APPROVED_COLON_RE.search(text)
    if colon:
        for m in LIST_RE.finditer(text, colon.end()):
            spans.append((m.start(), m.group(3), (m.group(1), int(m.group(2)))))
    clean, refused = [], []
    for span in sorted(spans, key=lambda s: s[0]):
        (refused if refusal_of(text, span[0]) else clean).append(span)
    return clean, refused


def refused_forms(text):
    """The values of the forms refused as negated or quoted (#1463); [] when none."""
    return [value for _, value, _ in _sightings(text)[1]]


# ---- the ONE check that refuses a found form: REFUSALS is the family (name, test(text, position)); `refusal_of` runs a form through it.
# The negation window is the 5 words before the form on the SAME LINE and does NOT stop at a sentence boundary: 'No issues found.
# Approved at sha256:<current>' is refused too (a documented false refusal: a resend is cheaper than an accidental approval).
NEGATION_WORDS = ("not", "no", "never", "n't", "do not", "did not", "have not", "has not", "cannot", "without", "not yet")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
WINDOW_WORDS = 5


def _negation(word):
    body = r"n['\u2019]t" if word == "n't" else r"\s+".join(re.escape(w) for w in word.split())
    pattern = re.compile((r"(?<![\w'\u2019])" if word != "n't" else r"(?<=\w)") + body + r"(?![\w'\u2019])", re.IGNORECASE)

    def test(text, pos):
        before = text[text.rfind("\n", 0, pos) + 1:pos]
        return pattern.search(" ".join(before.split()[-WINDOW_WORDS:])) is not None
    return test


def _line_of(text, pos):
    return text[text.rfind("\n", 0, pos) + 1:(text.find("\n", pos) if text.find("\n", pos) >= 0 else len(text))]


def _blockquote(text, pos):
    return _line_of(text, pos).lstrip().startswith(">")


def _code_span(text, pos):
    return any(m.start() <= pos < m.end() for m in re.finditer(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", text, re.DOTALL))


def _fenced(text, pos):
    fence, offset = None, 0
    for line in text.splitlines(True):
        mark = FENCE_RE.match(line)
        inside = fence is not None
        if mark:
            fence = None if fence == mark.group(1) else (fence or mark.group(1))
        if offset <= pos < offset + len(line):
            return inside and not mark
        offset += len(line)
    return False


def _between(opening, closing):
    def test(text, pos):
        line = _line_of(text, pos)
        col = pos - (text.rfind("\n", 0, pos) + 1)
        before, after = line[:col], line[col:]
        if opening == closing:
            return before.count(opening) % 2 == 1 and closing in after
        return before.rfind(opening) > before.rfind(closing) and opening in before and closing in after
    return test


REFUSALS = [("negation: " + w, _negation(w)) for w in NEGATION_WORDS] + [
    ("quote: > line", _blockquote), ("quote: code span", _code_span), ("quote: fenced block", _fenced),
    ("quote: straight double quotes", _between('"', '"')), ("quote: curly double quotes", _between("\u201c", "\u201d"))]


def refusal_of(text, pos):
    """The name of the first member of REFUSALS that refuses the form at `pos` of `text`, else None."""
    for name, test in REFUSALS:
        if test(text, pos):
            return name
    return None


def candidates(text, repo, number, limit=MAX_CANDIDATES):
    """The bounded set of issues to read: the one the message was sent on, then each `#N at sha256` of an `Approved:` list, then each
    issue the text names as `#N` or `owner/repo#N`; (repo, number) pairs, at most `limit`, never more however many the text names."""
    out = []

    def add(r, n):
        if r is not None and n is not None and (r, n) not in out and len(out) < limit:
            out.append((r, n))
    add(repo, number)
    for item in find_approvals(text):
        if item["ref"]:
            add(item["ref"][0] or repo, item["ref"][1])
    for m in MENTION_RE.finditer(text or ""):
        add(m.group(1) or repo, int(m.group(2)))
    return out


def done_when_hash(done_when):
    return hashlib.sha256(" ".join((done_when or "").split()).encode("utf-8")).hexdigest()


def evaluate(value, comments, done_when, plan_check):
    """{"ok": bool, "line": the one stderr line (failure only), "plan_hash", "done_when_hash"}.
    `comments`: the issue's comments; `done_when`: the issue's own, else its ask's (None when neither has one)."""
    def refuse(line, plan_hash=None):
        return {"ok": False, "line": "approval not recorded: %s; the message was delivered" % line, "plan_hash": plan_hash}
    if not HEX64_RE.match(value or ""):
        return refuse("the full 64-character hash is needed (a prefix or an empty value never approves)")
    plans = [c for c in comments if isinstance(c, dict) and isinstance(c.get("body"), str)
             and plan_check.HEADING_RE.match(next((l for l in c["body"].splitlines() if l.strip()), ""))]
    if len(plans) != 1:
        return refuse("the issue has %d plan comments, and an approval needs exactly 1" % len(plans))
    text = plans[0]["body"]
    current = plan_check.plan_hash(text)
    if "sha256:" + value.lower() != current:
        return refuse("the plan has changed (current %s)" % current, current)
    if not (done_when or "").strip():
        return refuse("there is no Done-when on the issue or its ask to check the plan against (current %s)" % current, current)
    try:
        declared = plan_check.declared_clauses(text)
    except Exception:
        declared = None
    if declared is None:
        return refuse("the plan declares no clauses block to compare with the Done-when (current %s)" % current, current)
    if not plan_check.verify_declared(declared, done_when):
        return refuse("the plan's clauses no longer equal the current Done-when (current %s)" % current, current)
    return {"ok": True, "line": "", "plan_hash": current, "done_when_hash": "sha256:" + done_when_hash(done_when)}
