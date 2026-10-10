"""plan_check: the mechanical check of a plan's scenarios (gate G0a of plan-driven development, #135).

A plan states WHAT: its core is a fenced yaml block starting `scenarios:`. This module reads that block with a small
STRICT parser of exactly the documented subset (there is no yaml dependency, and nothing is ever evaluated), checks
that every scenario is well-formed and discriminating, that every Done-when clause of the ask is covered, and
computes the plan's content hash so a later approval can be bound to it.

Pure: text in, data out. It reads no file, no GitHub, no clock, no environment. The caller (`fleet-switchboard plan
check`) fetches the text and passes it. See docs/plan-template.md for the schema and examples.
"""
import hashlib
import json
import re

MAX_BYTES = 100000          # a plan block larger than this is refused
MAX_LINE = 2000
MAX_SCENARIOS = 200         # a hard limit; the guidance is about 30
GUIDANCE_SCENARIOS = 30

PROOFS = ("test", "live", "measure", "screenshot")
EVALUATORS = ("ci", "tester", "reviewer", "adam")
CLAIMS = ("pr", "epic")
REQUIRED = ("id", "covers", "text", "proof", "pass", "discriminates", "evaluator")
OPTIONAL = ("claim", "slice", "phase")
KNOWN = REQUIRED + OPTIONAL
# proof -> the evaluators that can record it
PROOF_EVALUATORS = {"test": ("ci",), "screenshot": ("reviewer", "adam"), "live": ("tester", "adam"),
                    "measure": ("tester", "adam")}

ID_RE = re.compile(r"^[SU][0-9]+$")
COVERS_RE = re.compile(r"^(DW|PC)-([0-9]+)$")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.*plan\s*\(scenarios\)", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
UNCHANGED_NONE_RE = re.compile(r"^[\s#>*-]*unchanged:\s*none\s+because\s+(\S.*)$", re.IGNORECASE | re.MULTILINE)

# A pass rule that says nothing. Compared after lower-casing and dropping punctuation.
VAGUE_PASS = {"works", "it works", "work", "ok", "okay", "pass", "passes", "it passes", "non-empty", "nonempty",
              "non empty", "not empty", "good", "fine", "works fine", "works correctly", "works as expected",
              "as expected", "correct", "correctly", "succeeds", "it succeeds", "success", "is ok", "is fine",
              "no errors", "no error", "looks good", "looks right", "behaves correctly", "done"}
VACUOUS_DISCRIMINATES = {"n/a", "na", "none", "nothing", "tbd", "todo", "-", "no", "not applicable", "same as pass"}
MIN_PASS_CHARS = 15
MIN_TEXT_CHARS = 10
MIN_DISCRIMINATES_CHARS = 10
COMPARISON_WORDS = ("at least", "at most", "exactly", "equals", "equal to", "fewer than", "less than", "more than",
                    "greater than", "no more than", "no fewer than", "contains", "does not contain", "matches",
                    "status", "exit code", "within", "before", "after", "under", "over", "identical", "differs",
                    "unchanged", "empty list", "is empty", "is absent", "is present", "refused", "rejected", "denied")


COMPARISON_RE = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(w) for w in COMPARISON_WORDS))


class PlanFormatError(Exception):
    """The plan cannot be read at all (no block, a malformed block, too large): the check cannot say pass or fail."""


# ----------------------------------------------------------------------------------- finding the plan

def select_plan_text(body, comments):
    """(text, source): the LATEST comment whose first non-empty line is a heading containing `Plan (scenarios)`, else
    the issue body. `comments` is a list of {"id", "created_at", "body"}; source is "comment <id>" or "issue body"."""
    chosen = None
    ordered = sorted((c for c in comments or [] if isinstance(c, dict) and isinstance(c.get("body"), str)),
                     key=lambda c: (str(c.get("created_at") or ""), c.get("id") if isinstance(c.get("id"), int) else 0))
    for comment in ordered:
        first = next((line for line in comment["body"].splitlines() if line.strip()), "")
        if HEADING_RE.match(first):
            chosen = comment
    if chosen is not None:
        return chosen["body"], "comment %s" % chosen.get("id")
    return body or "", "issue body"


def fenced_blocks(text):
    """[(start line number, [lines])] of every fenced block in `text` (``` or ~~~, any info string)."""
    blocks, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        opened = FENCE_RE.match(lines[i])
        if not opened:
            i += 1
            continue
        mark = opened.group(1)
        start, inner, i = i + 1, [], i + 1
        while i < len(lines):
            closing = FENCE_RE.match(lines[i])
            if closing and closing.group(1)[0] == mark[0] and len(closing.group(1)) >= len(mark) \
                    and not closing.group(2).strip():
                break
            inner.append(lines[i])
            i += 1
        blocks.append((start, inner))
        i += 1
    return blocks


def scenario_block(text):
    """The lines of the ONE fenced block whose first non-blank, non-comment line is `scenarios:`. Raises
    PlanFormatError for none, or for more than one."""
    found = []
    for start, inner in fenced_blocks(text):
        first = next((l for l in inner if l.strip() and not l.lstrip().startswith("#")), None)
        if first is not None and first.strip() == "scenarios:":
            found.append(inner)
    if not found:
        raise PlanFormatError("no fenced yaml block starting `scenarios:` was found in the plan")
    if len(found) > 1:
        raise PlanFormatError("%d blocks start `scenarios:`: a plan has exactly one" % len(found))
    return found[0]


# ----------------------------------------------------------------------------------- the strict parser

def _strip_comment(value):
    """`value` without a trailing ` # comment` (a `#` after whitespace, outside quotes)."""
    quote, skip = None, False
    for index, char in enumerate(value):
        if skip:
            skip = False
            continue
        if quote:
            if char == "\\" and quote == '"':
                skip = True
            elif char == quote:
                quote = None
        elif char in "'\"":
            if index == 0 or value[index - 1] in " \t":
                quote = char
        elif char == "#" and (index == 0 or value[index - 1] in " \t"):
            return value[:index].rstrip()
    return value.rstrip()


def _scalar(raw, where):
    """The string a one-line scalar means: quoted (single or double) or plain. Anything else is refused."""
    raw = raw.strip()
    if raw == "":
        return ""
    if raw[0] == '"':
        if len(raw) < 2 or raw[-1] != '"':
            raise PlanFormatError("%s: an unterminated double-quoted value" % where)
        out, i, body = [], 0, raw[1:-1]
        while i < len(body):
            char = body[i]
            if char == "\\":
                i += 1
                if i >= len(body) or body[i] not in 'nt"\\/':
                    raise PlanFormatError("%s: only \\n \\t \\\" \\\\ escapes are understood in a double-quoted value" % where)
                out.append({"n": "\n", "t": "\t"}.get(body[i], body[i]))
            elif char == '"':
                raise PlanFormatError("%s: a quote inside a double-quoted value must be written \\\"" % where)
            else:
                out.append(char)
            i += 1
        return "".join(out)
    if raw[0] == "'":
        if len(raw) < 2 or raw[-1] != "'":
            raise PlanFormatError("%s: an unterminated single-quoted value" % where)
        body = raw[1:-1]
        if re.search(r"(?<!')'(?!')", body.replace("''", "")):
            raise PlanFormatError("%s: a quote inside a single-quoted value must be doubled ('')" % where)
        return body.replace("''", "'")
    if raw[0] in "[{&*!%@`" or raw[0] in "|>" and raw not in ("|", ">", "|-", ">-"):
        raise PlanFormatError("%s: a value may not start with %r (flow lists and maps, anchors, tags and block "
                              "indicators are not part of the subset): quote it" % (where, raw[0]))
    return raw


def _indent(line):
    stripped = line.lstrip(" ")
    return len(line) - len(stripped)


def parse_scenarios(lines):
    """[{key: str}] from the block's lines: a flat list of maps with scalar values.

    The subset: `scenarios:` then items `- key: value`, each further key at the first key's column. A value is a
    plain or quoted scalar on one line, continued on deeper-indented lines (folded with a space), or a block scalar
    (`|` or `>`, with optional `-`). Comments (`# ...`) and blank lines are skipped. Refused, with the line: tabs,
    nested lists or maps, flow collections, anchors, tags, duplicate keys, a second document, anything over the
    size limits. Raises PlanFormatError."""
    text = "\n".join(lines)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise PlanFormatError("the scenarios block is larger than %d bytes" % MAX_BYTES)
    rows = []   # (line number, indent, text) with comments-only and blank lines dropped (kept for block scalars)
    for number, line in enumerate(lines, 1):
        if len(line) > MAX_LINE:
            raise PlanFormatError("line %d: longer than %d characters" % (number, MAX_LINE))
        if re.match(r"^ *\t", line):
            raise PlanFormatError("line %d: a tab in the indentation: use spaces" % number)
        rows.append((number, line))
    scenarios, current, i = [], None, 0
    seen_head, dash_indent, key_indent = False, None, None
    while i < len(rows):
        number, line = rows[i]
        body = line.strip()
        if not body or body.startswith("#"):
            i += 1
            continue
        if body in ("---", "...") or body.startswith("--- "):
            raise PlanFormatError("line %d: a yaml document marker: a plan has one block" % number)
        if not seen_head:
            if body != "scenarios:" or _indent(line) != 0:
                raise PlanFormatError("line %d: the block must start with `scenarios:` at column 1" % number)
            seen_head = True
            i += 1
            continue
        indent = _indent(line)
        text_part = line[indent:]
        if text_part.startswith("- ") or text_part == "-":
            if dash_indent is None:
                dash_indent = indent
            if indent != dash_indent:
                raise PlanFormatError("line %d: a nested list: the scenarios are one flat list" % number)
            if text_part[1:].lstrip().startswith("- "):
                raise PlanFormatError("line %d: a nested list: the scenarios are one flat list" % number)
            rest = text_part[1:]
            gap = len(rest) - len(rest.lstrip(" "))
            rest = rest.lstrip(" ")
            if not rest or rest.startswith("#"):
                raise PlanFormatError("line %d: put the first key of a scenario on the `-` line" % number)
            current = {}
            scenarios.append(current)
            if len(scenarios) > MAX_SCENARIOS:
                raise PlanFormatError("more than %d scenarios: split the plan" % MAX_SCENARIOS)
            this_key_indent = indent + 1 + gap
            if key_indent is None:
                key_indent = this_key_indent
            elif this_key_indent != key_indent:
                raise PlanFormatError("line %d: keys must line up with the first scenario's keys" % number)
            entry, entry_indent = rest, this_key_indent
        else:
            if current is None:
                raise PlanFormatError("line %d: expected a `- ` list item after `scenarios:`" % number)
            if indent != key_indent:
                raise PlanFormatError("line %d: a key must be at column %d (nested maps are not part of the subset)"
                                      % (number, (key_indent or 0) + 1))
            entry, entry_indent = text_part, indent
        found = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(?:[ ]+(.*))?$", entry)
        if not found:
            raise PlanFormatError("line %d: expected `key: value`, got %r" % (number, entry[:60]))
        key, raw = found.group(1), _strip_comment(found.group(2) or "")
        where = "line %d (%s)" % (number, key)
        if key in current:
            raise PlanFormatError("line %d: the key %r appears twice in one scenario" % (number, key))
        i += 1
        if raw == "":
            following = next((l for _, l in rows[i:] if l.strip() and not l.strip().startswith("#")), None)
            if following is not None and _indent(following) > entry_indent:
                raise PlanFormatError("line %d: `%s:` has a nested value: nested lists and maps are not part of the "
                                      "subset (write one scalar, or a | or > block)" % (number, key))
        if raw in ("|", ">", "|-", ">-"):
            collected = []
            while i < len(rows):
                n2, l2 = rows[i]
                if l2.strip() and _indent(l2) <= entry_indent:
                    break
                collected.append(l2.strip() if l2.strip() else "")
                i += 1
            while collected and not collected[-1]:
                collected.pop()
            current[key] = ("\n" if raw[0] == "|" else " ").join(collected) if raw[0] == "|" else \
                " ".join(c for c in collected if c)
            continue
        value = _scalar(raw, where)
        # a plain or quoted value may continue on deeper-indented lines
        while i < len(rows):
            n2, l2 = rows[i]
            if not l2.strip():
                break
            if l2.strip().startswith("#") and _indent(l2) <= entry_indent:
                break
            if _indent(l2) <= entry_indent:
                break
            if raw[:1] in ("'", '"'):
                raise PlanFormatError("line %d: a quoted value must be closed on its own line" % n2)
            value = (value + " " + _strip_comment(l2.strip())).strip()
            i += 1
        current[key] = value
    if not seen_head:
        raise PlanFormatError("the block is empty")
    if not scenarios:
        raise PlanFormatError("`scenarios:` has no items")
    return scenarios


# ----------------------------------------------------------------------------------- the Done-when clauses

def clauses_of(done_when):
    """[clause text] from the ask's Done-when line: its numbered parts `(1) ... (2) ...`, in order, counting up from
    1 and stopping at the first gap. A line with no `(1)` is one clause. None or empty gives []."""
    text = " ".join((done_when or "").split())
    if not text:
        return []
    marks, position, n = [], 0, 1
    while True:
        found = None
        for candidate in re.finditer(r"(?:^|(?<=[\s;,.]))\(%d\)" % n, text[position:]):
            before = text[:position + candidate.start()]
            if before.count("(") - before.count(")") == 0:   # a `(3)` inside an open parenthesis is prose, not a clause
                found = candidate
                break
        if found is None:
            break
        marks.append(position + found.start())
        position += found.end()
        n += 1
    if not marks:
        return [text]
    clauses = []
    for index, start in enumerate(marks):
        end = marks[index + 1] if index + 1 < len(marks) else len(text)
        clauses.append(re.sub(r"^\(\d+\)\s*", "", text[start:end]).strip(" ;,"))
    return clauses


# ----------------------------------------------------------------------------------- validation

def _normal(text):
    return " ".join(str(text).split())


def _plain_words(text):
    return " ".join(re.sub(r"[^a-z0-9 /'-]", " ", str(text).lower()).split())


def pass_rule_problem(rule):
    """Why a pass rule is vague, or None. It needs enough words and one concrete token: a number, a quoted value, a
    comparison, or a status."""
    rule = _normal(rule)
    if not rule:
        return "the pass rule is empty"
    if _plain_words(rule) in VAGUE_PASS:
        return "the pass rule %r says nothing a wrong build could fail" % rule
    if len(rule) < MIN_PASS_CHARS:
        return "the pass rule is too short to be checkable (%d characters, at least %d)" % (len(rule), MIN_PASS_CHARS)
    lowered = rule.lower()
    concrete = (re.search(r"[0-9]", rule) or re.search(r"[\"'`][^\"'`]+[\"'`]", rule)
                or re.search(r"[<>]=?|==|!=|=", rule) or COMPARISON_RE.search(lowered))
    if not concrete:
        return ("the pass rule has no concrete token: name a number, a status, a quoted value or a comparison "
                "(%r)" % rule[:60])
    return None


def discriminates_problem(value, pass_rule):
    value = _normal(value)
    if not value:
        return "`discriminates` is empty: name an observable a wrong build would fail"
    if _plain_words(value) in VACUOUS_DISCRIMINATES:
        return "`discriminates` is %r: name an observable a wrong build would fail" % value
    if len(value) < MIN_DISCRIMINATES_CHARS:
        return "`discriminates` is too short to name an observable (%d characters)" % len(value)
    if value.lower() == _normal(pass_rule).lower():
        return "`discriminates` repeats the pass rule: say what a WRONG build would show"
    return None


def validate(scenarios, clauses, text=""):
    """(defects, warnings): each a dict {"id", "code", "message"}; `id` is the scenario id, or "-" for the plan.

    `clauses` is the ask's Done-when clause list; `text` is the plan text (for `unchanged: none because <reason>`)."""
    defects, warnings = [], []

    def bad(ident, code, message):
        defects.append({"id": ident, "code": code, "message": message})

    seen = {}
    for position, scenario in enumerate(scenarios, 1):
        ident = scenario.get("id") or "#%d" % position
        unknown = sorted(set(scenario) - set(KNOWN))
        if unknown:
            bad(ident, "unknown-key", "unknown key(s): %s (allowed: %s)" % (", ".join(unknown), ", ".join(KNOWN)))
        for key in REQUIRED:
            if key not in scenario or not _normal(scenario[key]):
                bad(ident, "missing-%s" % key, "`%s` is required and empty or missing" % key)
        sid = _normal(scenario.get("id", ""))
        if sid:
            if not ID_RE.match(sid):
                bad(ident, "bad-id", "id %r must look like S1 (behaviour) or U1 (unchanged)" % sid)
            if sid in seen:
                bad(ident, "duplicate-id", "id %s is used twice" % sid)
            seen[sid] = scenario
        covers = _normal(scenario.get("covers", ""))
        if covers and not COVERS_RE.match(covers):
            bad(ident, "bad-covers", "covers %r must be DW-<n> (a Done-when clause) or PC-<n> (a plan constraint)" % covers)
        elif covers.startswith("DW-") and clauses and not 1 <= int(covers[3:]) <= len(clauses):
            bad(ident, "covers-unknown-clause",
                "covers %s but the Done-when has %d clause(s)" % (covers, len(clauses)))
        if _normal(scenario.get("text", "")) and len(_normal(scenario["text"])) < MIN_TEXT_CHARS:
            bad(ident, "short-text", "`text` is too short to say what the scenario is")
        proof, evaluator = _normal(scenario.get("proof", "")), _normal(scenario.get("evaluator", ""))
        if proof and proof not in PROOFS:
            bad(ident, "bad-proof", "proof %r must be one of %s" % (proof, "|".join(PROOFS)))
        if evaluator and evaluator not in EVALUATORS:
            bad(ident, "bad-evaluator", "evaluator %r must be one of %s" % (evaluator, "|".join(EVALUATORS)))
        if proof in PROOF_EVALUATORS and evaluator in EVALUATORS and evaluator not in PROOF_EVALUATORS[proof]:
            bad(ident, "evaluator-mismatch", "proof %s is recorded by %s, not %s"
                % (proof, " or ".join(PROOF_EVALUATORS[proof]), evaluator))
        claim = _normal(scenario.get("claim", "")) or "pr"
        if claim not in CLAIMS:
            bad(ident, "bad-claim", "claim %r must be pr or epic" % claim)
        if _normal(scenario.get("pass", "")):
            problem = pass_rule_problem(scenario["pass"])
            if problem:
                bad(ident, "vague-pass", problem)
        if _normal(scenario.get("discriminates", "")):
            problem = discriminates_problem(scenario["discriminates"], scenario.get("pass", ""))
            if problem:
                bad(ident, "no-discriminator", problem)

    for number in range(1, len(clauses) + 1):
        tag = "DW-%d" % number
        covering = [s for s in scenarios if _normal(s.get("covers", "")) == tag and _normal(s.get("id", "")).startswith("S")]
        if not covering:
            bad("-", "uncovered-clause", "%s is not covered by any S scenario: %s" % (tag, clauses[number - 1][:80]))
    if not clauses:
        bad("-", "no-done-when", "the ask has no Done-when to cover: coverage cannot be checked")
    if not any(_normal(s.get("id", "")).startswith("U") for s in scenarios):
        reason = UNCHANGED_NONE_RE.search(text or "")
        if not (reason and len(reason.group(1).strip()) >= 5):
            bad("-", "no-unchanged-scenario",
                "no U (unchanged) scenario, and the plan does not say `unchanged: none because <reason>`")
    if len(scenarios) > GUIDANCE_SCENARIOS:
        warnings.append({"id": "-", "code": "many-scenarios",
                         "message": "%d scenarios (guidance: about %d): split the plan into sections by slice and say "
                                    "which are claimed per PR" % (len(scenarios), GUIDANCE_SCENARIOS)})
    return defects, warnings


# ----------------------------------------------------------------------------------- the content hash

def _id_key(ident):
    found = re.match(r"^([A-Za-z]*)([0-9]*)$", ident)
    return (found.group(1), int(found.group(2) or 0), ident) if found else (ident, 0, ident)


def canonical(scenarios, clauses, text=""):
    """The canonical form the hash covers: the clause list and the scenarios (sorted by id, keys sorted, whitespace
    collapsed, `claim` defaulted), plus the stated reason for having no unchanged scenario."""
    items = []
    for scenario in scenarios:
        item = {key: _normal(scenario[key]) for key in sorted(scenario)}
        item.setdefault("claim", "pr")
        if not item["claim"]:
            item["claim"] = "pr"
        items.append(item)
    items.sort(key=lambda item: _id_key(item.get("id", "")))
    reason = UNCHANGED_NONE_RE.search(text or "")
    out = {"clauses": [_normal(c) for c in clauses], "scenarios": items}
    if reason and not any(i.get("id", "").startswith("U") for i in items):
        out["unchanged_none_because"] = _normal(reason.group(1))
    return out


def content_hash(scenarios, clauses, text=""):
    blob = json.dumps(canonical(scenarios, clauses, text), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------- the whole check

def check(text, done_when):
    """The result of checking `text` (the plan) against the ask's `done_when` line:
    {"result": "pass"|"fail", "hash", "scenarios", "clauses", "defects", "warnings"}. Raises PlanFormatError when the
    plan cannot be read."""
    scenarios = parse_scenarios(scenario_block(text))
    clauses = clauses_of(done_when)
    defects, warnings = validate(scenarios, clauses, text)
    return {"result": "fail" if defects else "pass", "hash": content_hash(scenarios, clauses, text),
            "scenarios": len(scenarios), "clauses": clauses, "defects": defects, "warnings": warnings}


def render(result, source):
    """The lines `plan check` prints."""
    lines = ["plan check: %s" % result["result"].upper(), "source: %s" % source,
             "scenarios: %d; Done-when clauses: %d" % (result["scenarios"], len(result["clauses"])),
             "plan hash: %s" % result["hash"]]
    lines.extend("DEFECT %s: %s" % (d["id"], d["message"]) for d in result["defects"])
    lines.extend("WARN: %s" % w["message"] for w in result["warnings"])
    return lines
