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
GUIDANCE_SCENARIOS = 30       # per slice, or in total when no scenario has a slice
GUIDANCE_TOTAL_WITH_SLICES = 80

PROOFS = ("test", "live", "measure", "screenshot")
EVALUATORS = ("ci", "tester", "reviewer", "owner")
# An older plan wrote the owner's evaluator under a personal name; it is still accepted as `owner` (a plan approved with it
# keeps passing, and its hash, which covers the text as written, does not change), with a warning to rename it.
EVALUATOR_ALIASES = {"adam": "owner"}
CLAIMS = ("pr", "epic")
REQUIRED = ("id", "covers", "text", "proof", "pass", "discriminates", "evaluator")
OPTIONAL = ("claim", "slice", "phase")
KNOWN = REQUIRED + OPTIONAL
# proof -> the evaluators that can record it
PROOF_EVALUATORS = {"test": ("ci",), "screenshot": ("reviewer", "owner"), "live": ("tester", "owner"),
                    "measure": ("tester", "owner")}

ID_RE = re.compile(r"^[SU][0-9]+[a-z]?$")
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


class PlanFormatError(ValueError):
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
        return chosen["body"], "comment %s%s" % (chosen.get("id"), comment_author(chosen))
    return body or "", "issue body"


def comment_author(comment):
    """` by <login>` for a GitHub comment ({"user": {"login": ...}}), else empty. Reported so a reader sees whose plan
    the check read: the latest heading comment counts whoever wrote it."""
    user = comment.get("user") if isinstance(comment, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return " by %s" % login if isinstance(login, str) and re.match(r"^[A-Za-z0-9_.\[\]-]{1,60}$", login) else ""


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

PROSE_REF_RE = re.compile(r"\b(?:see|per|in|of|clause|item|section|and|or|with|to)\s*\(?\s*$", re.IGNORECASE)


def clauses_of(done_when):
    """[clause text] from the ask's Done-when line: its numbered parts `(1) ... (2) ...`, in order, counting up from
    1 and stopping at the first gap. A line with no `(1)` is one clause. None or empty gives []."""
    listed = [re.match(r"^\s*([0-9]+)[.)]\s+(\S.*)$", line) for line in (done_when or "").splitlines() if line.strip()]
    if len(listed) >= 2 and all(listed) and [int(m.group(1)) for m in listed] == list(range(1, len(listed) + 1)):
        return [" ".join(m.group(2).split()) for m in listed]   # a numbered list, one clause per line
    text = " ".join((done_when or "").split())
    if not text:
        return []
    marks, position, n = [], 0, 1
    while True:
        found = None
        for candidate in re.finditer(r"(?:^|(?<=[\s;,.]))\(%d\)" % n, text[position:]):
            before = text[:position + candidate.start()]
            # a `(3)` that is a reference in prose ("see (3)") is not a marker; an unbalanced "(" elsewhere is irrelevant
            if not PROSE_REF_RE.search(before):
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


# ----------------------------------------------------------------------------------- declared clauses

def declared_clauses(text):
    """[clause text] from the plan's own `clauses:` block (a fenced yaml block, `DW-<n>: text` lines numbered from 1), or
    None when the plan has none. A Done-when written as one sentence with several clauses cannot be split reliably, so the
    plan declares them and `verify_declared` checks they say what the ask says. Raises PlanFormatError."""
    found = []
    for _, inner in fenced_blocks(text):
        first = next((l for l in inner if l.strip() and not l.lstrip().startswith("#")), None)
        if first is not None and first.strip() == "clauses:":
            found.append(inner)
    if not found:
        return None
    if len(found) > 1:
        raise PlanFormatError("%d blocks start `clauses:`: a plan declares its clauses once" % len(found))
    out, key_indent = [], None
    started = False
    for number, line in enumerate(found[0], 1):
        body = line.strip()
        if not body or body.startswith("#"):
            continue
        if re.match(r"^ *\t", line):
            raise PlanFormatError("clauses line %d: a tab in the indentation: use spaces" % number)
        if not started:
            started = True
            continue   # the `clauses:` line
        indent = len(line) - len(line.lstrip(" "))
        found_key = re.match(r"^DW-([0-9]+):(?:[ ]+(.*))?$", body)
        if found_key and (key_indent is None or indent == key_indent):
            key_indent = indent if key_indent is None else key_indent
            if int(found_key.group(1)) != len(out) + 1:
                raise PlanFormatError("clauses line %d: DW-%s is out of order: number the clauses 1, 2, 3 ..." % (number, found_key.group(1)))
            out.append(_scalar(_strip_comment(found_key.group(2) or ""), "clauses line %d" % number))
        elif key_indent is not None and indent > key_indent and out:
            out[-1] = (out[-1] + " " + _strip_comment(body)).strip()   # a continuation line
        else:
            raise PlanFormatError("clauses line %d: expected `DW-<n>: text`, got %r" % (number, body[:60]))
    if not out or not all(c.strip() for c in out):
        raise PlanFormatError("the `clauses:` block is empty or has an empty clause")
    return [" ".join(c.split()) for c in out]


def _words(text):
    return re.findall(r"[a-z0-9]+", re.sub(r"\([0-9]+\)", " ", str(text).lower()))


def verify_declared(declared, done_when):
    """True when the declared clauses, joined in order, reproduce the Done-when text (compared as words: whitespace, case,
    punctuation and the `(n)` numbering are ignored, so the list may split on the Done-when's own separators)."""
    return _words(" ".join(declared)) == _words(done_when)


CONSTRAINT_RE = re.compile(r"(?m)^\s*(?:[-*]\s*)?\**PC-([0-9]+)\**\s*[:.)-]")


def declared_constraints(text):
    """The PC numbers the plan declares (a line `PC-<n>: ...` outside code fences), as a set."""
    kept, inside = [], None
    for line in (text or "").splitlines():
        opened = FENCE_RE.match(line)
        if opened:
            inside = None if inside else opened.group(1)[0]
            continue
        if not inside:
            kept.append(line)
    return {int(n) for n in CONSTRAINT_RE.findall("\n".join(kept))}


# ----------------------------------------------------------------------------------- validation

def _normal(text):
    return " ".join(str(text).split())


def _plain_words(text):
    return " ".join(re.sub(r"[^a-z0-9 /'-]", " ", str(text).lower()).split())


# Words that say nothing a wrong build could fail. A quoted literal made only of these is not an expected value.
VAGUE_WORDS = {"works", "work", "working", "ok", "okay", "fine", "good", "correct", "correctly", "right", "pass", "passes",
               "passed", "success", "succeeds", "succeeded", "done", "nice", "nicely", "well", "properly", "expected",
               "everything", "something", "nothing", "true", "yes", "non-empty", "nonempty", "valid", "as", "it", "is"}
UNITS = (r"ms|s|sec|secs|second|seconds|minute|minutes|min|mins|hour|hours|h|day|days|bytes?|kb|mb|gb|characters?|chars?|"
         r"lines?|rows?|items?|agents?|files?|requests?|calls?|messages?|errors?|retries|times|entries|records|tests?|"
         r"scenarios?|comments?|issues?|prs?|commits?|branches|fields?|results?|replies|reply|events?|attempts?|"
         r"occurrences?|matches|hits|failures?|warnings?|defects?|violations?|dirs?|directories|modules?|functions?")
THRESHOLD = (r"(?:at least|at most|exactly|more than|fewer than|less than|greater than|no more than|no fewer than|"
             r"under|over|within|above|below|up to)")
STATUS_RE = re.compile(r"\b(?:status(?: code)?|http|returns?|responds? with|responds?|code)\s*(?:is|=|==|of|:)?\s*(?:[1-5][0-9][0-9]|[1-5]xx)\b",
                       re.IGNORECASE)
STATUS_CLASS_RE = re.compile(r"\b[1-5]xx\b|\b(?:a|an)\s+[1-5][0-9][0-9]\b", re.IGNORECASE)   # a status CLASS: `a 4xx`, `5xx`
# zero, or an exact number, of anything: `sends 0 accepts`, `received 0 responses`, `exactly 3 retries`
COUNT_OF_ANYTHING_RE = re.compile(r"\b(?:0|exactly\s+[0-9]+)\s+(?:[A-Za-z-]+\s+){0,2}?[A-Za-z]{3,}", re.IGNORECASE)
EXIT_RE = re.compile(r"\bexit (?:code|status)\s*(?:is|=|==|of|:)?\s*[0-9]+\b", re.IGNORECASE)
UNIT_RE = re.compile(r"\b[0-9]+(?:\.[0-9]+)?\s*(?:%%|percent|%s)(?![A-Za-z])" % UNITS, re.IGNORECASE)
PERCENT_RE = re.compile(r"[0-9]\s*%")
N_OF_N_RE = re.compile(r"\b([0-9]+)\s+of\s+(?:the\s+)?([0-9]+)\b", re.IGNORECASE)
HEDGE_RE = re.compile(r"\b(?:maybe|somehow|or so|probably|perhaps|presumably|kind of|sort of|more or less|i guess|"
                      r"yes fine|fine ok|ok fine|hopefully|roughly|about right|seems?|looks? (?:right|good|fine))\b", re.IGNORECASE)
IDENTICAL_RE = re.compile(r"\b(?:identical|equal|same)\b(?:\s+\w+){0,3}?\s+(?:to|as)\s+(?:the\s+|a\s+)?([A-Za-z][\w-]*)", re.IGNORECASE)
EQUALS_NUMBER_RE = re.compile(r"\b(?:equals?|==|is exactly|are exactly|is|are)\s+[0-9]+(?![0-9]*\s*(?:user|person|people)\b)", re.IGNORECASE)
VAGUE_TARGETS = {"same", "expectations", "expectation", "expected", "it", "that", "this", "before", "good", "fine", "right", "ok"}
THRESHOLD_RE = re.compile(r"\b%s\s+[0-9]" % THRESHOLD, re.IGNORECASE)
# an identifier (snake_case, camelCase, dotted, a path, or backticked) compared to a number or a non-vague quoted value
IDENT = r"(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_]*(?:[._/-][A-Za-z0-9_]+)+|[a-z]+[A-Z][A-Za-z0-9]*|[A-Za-z_][A-Za-z0-9_]*)"
COMPARED_RE = re.compile(r"(%s)\s*(?:==|!=|>=|<=|=|<|>|\bis\b|\bequals\b|\bcontains\b|\bmatches\b)\s*(\"[^\"]+\"|'[^']+'|`[^`]+`|[0-9][0-9.]*)"
                         % IDENT, re.IGNORECASE)
STRONG_PHRASES = ("byte-identical", "byte equal", "byte-equal", "byte for byte", "byte-for-byte", "identical to the stored", "identical to the fixture", "no diff", "diff is empty",
                  "zero diff", "checksum", "sha256", "empty diff")
QUOTED_RE = re.compile(r"[\"'`]([^\"'`]+)[\"'`]")


def _vague_literal(text):
    words = re.findall(r"[a-z0-9-]+", text.lower())
    return not words or all(w in VAGUE_WORDS for w in words)


def _equal_n_of_n(rule):
    """`10 of 10 files`: a count out of the same total. `3 of 4 people like it` observes nothing in particular (and a
    stated threshold, `at least 3 of 4`, is caught by THRESHOLD_RE)."""
    return any(m.group(1) == m.group(2) for m in N_OF_N_RE.finditer(rule))


def _has_observable(rule):
    """True when the rule names something a wrong build could fail: a status or exit code, a number with a unit or a
    threshold, an identifier compared to a number or a quoted literal, a quoted EXPECTED literal that is not itself a
    vague word, or a strong comparison phrase (byte-identical, no diff). A bare digit, `=`, `after` or `before` is not."""
    lowered = rule.lower()
    if STATUS_RE.search(rule) or EXIT_RE.search(rule) or UNIT_RE.search(rule) or PERCENT_RE.search(rule) \
            or THRESHOLD_RE.search(rule) or STATUS_CLASS_RE.search(rule) or COUNT_OF_ANYTHING_RE.search(rule) or _equal_n_of_n(rule) or any(phrase in lowered for phrase in STRONG_PHRASES):
        return True
    named = IDENTICAL_RE.search(rule)
    if named and named.group(1).lower() not in VAGUE_TARGETS and named.group(1).lower() not in VAGUE_WORDS:
        return True  # identical to a NAMED thing (the baseline, the stored fixture)
    if EQUALS_NUMBER_RE.search(rule) and re.search(r"[A-Za-z]{3,}", rule.split(" is ")[0] if " is " in rule else rule):
        return True  # a count or value compared to a number: `count of Z equals 3`, `the total is 0`
    for quoted in QUOTED_RE.findall(rule):
        if not _vague_literal(quoted):
            return True
    for ident, value in COMPARED_RE.findall(rule):
        if ident.lower() in VAGUE_WORDS or ident.lower() in ("it", "result", "output") and _vague_literal(value.strip("\"'`")):
            continue
        if not _vague_literal(value.strip("\"'`")):
            return True
    return False


def pass_rule_problem(rule):
    """Why a pass rule is vague, or None. It needs a concrete OBSERVABLE (see _has_observable); filler such as `works`,
    `correctly`, `matches the expectations` earns nothing."""
    rule = _normal(rule)
    if not rule:
        return "the pass rule is empty"
    if _plain_words(rule) in VAGUE_PASS:
        return ("the pass rule %r says nothing a wrong build could fail: it names no observable, threshold, expected "
                "literal or named comparison (for example `exit status is 0`)" % rule)
    hedge = HEDGE_RE.search(rule)
    if hedge:
        return ("the pass rule %r hedges (%r): state what is observed, not what seems or is roughly so, for example "
                "`exit status is 0` or `0 occurrences of X`" % (rule[:80], hedge.group(0)))
    if len(rule) < MIN_PASS_CHARS:
        return ("the pass rule %r is too short to be checkable (%d characters, at least %d): say what is observed, for "
                "example `exit status is 0` or `exactly 1 agent named \"alpha\"`" % (rule, len(rule), MIN_PASS_CHARS))
    if not _has_observable(re.sub(r"\b[0-9]+\s+(?:users?|persons?|people)\b", " ", rule, flags=re.IGNORECASE)):
        return ("the pass rule %r has no observable: it needs ONE of an exit code or status (`exit status is 0`), a count or a "
                "threshold (`0 occurrences of X`, `10 of 10 files`, `within 5 s`), an expected literal in quotes "
                "(`shows \"Ready\"`), or a named comparison (`identical to the baseline`, `count equals 3`)" % rule[:80])
    return None


# Phrases that say "a wrong build fails" without saying what the wrong build does.
META_WORDS = {"a", "an", "the", "this", "that", "it", "any", "wrong", "bad", "incorrect", "build", "builds", "version",
              "implementation", "would", "will", "should", "could", "fail", "fails", "failing", "failed", "break",
              "breaks", "and", "or", "of", "to", "be", "is", "are", "if", "when", "scenario", "test", "check", "so"}
NEGATIVE_MARKERS = ("stub", "canned", "empty", "nothing", "missing", "without", "ignores", "ignore", "hard-coded",
                    "hardcoded", "returns", "omits", "skips", "instead", "stale", "unchanged", "duplicate", "drops",
                    "never", "not", "no", "zero", "diff", "differs", "mismatch", "extra", "reordered", "ordering",
                    "changed", "old", "leaks", "crash", "crashes", "hangs", "times", "timeout", "denied", "refused",
                    "rejects", "accepts", "wrong", "creates", "deletes", "overwrites", "different")
OVERLAP_LIMIT = 0.6   # at or above: a warning
OVERLAP_FAIL = 0.8    # at or above: a failure (the pass rule reworded)


# `a build that <does X> ... <an observable outcome>`: structure, not a list of verbs the plan must happen to use
BUILD_NOUNS = r"(?:build|builds|implementation|version|stub|variant|fake|mock|regression|bug|handler|client|agent|service|change|patch|fix|shortcut|cache)"
STRUCT_RE = re.compile(r"^(?:a|an|the|any)\s+(?:[a-z-]+\s+){0,3}?%s\s+(?:that|which|where|with|can|still|only|just|always|"
                       r"does|did|has|had|would|will|never)\s+\S+(?:\s+\S+){2,}" % BUILD_NOUNS, re.IGNORECASE)
OUTCOME_RE = re.compile(r"\b(?:shows?|returns?|sends?|finds?|changes?|queues?|prints?|reports?|emits?|writes?|leaves?|"
                        r"yields?|produces?|gives?|answers?|replies|logs?|creates?|lists?|accepts?|rejects?|posts?|opens?|"
                        r"displays?|reads?|drops?|skips?|loses?|keeps?|sets?|updates?|marks?|counts?|resolves?|"
                        r"hides?|exposes?|leaks?|retries|retry|waits?|times? out|crash(?:es)?|hangs?)\b", re.IGNORECASE)


def _structural_wrong_build(value):
    return bool(STRUCT_RE.match(value) and OUTCOME_RE.search(value))


def _content_words(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if len(w) > 2}


def discriminates_problem(value, pass_rule):
    value = _normal(value)
    if not value:
        return ("`discriminates` is empty: name the wrong build and what it does, e.g. a stub that returns an empty list "
                "(not that a wrong build 'fails')")
    if _plain_words(value) in VACUOUS_DISCRIMINATES:
        return "`discriminates` is %r: name the wrong build and what it does, e.g. a stub that returns an empty list" % value
    if len(value) < MIN_DISCRIMINATES_CHARS:
        return ("`discriminates` %r is too short to name a wrong build: say what it does, e.g. a stub that returns an "
                "empty list" % value)
    words = re.findall(r"[a-z0-9-]+", value.lower())
    rest = [w for w in words if w not in META_WORDS]
    if len(rest) < 3:
        return ("`discriminates` only says that a wrong build fails (%r): say what the wrong build does, for example "
                "'a stub that returns an empty list'" % value[:60])
    if not (any(w in NEGATIVE_MARKERS for w in words) or re.search(r"[0-9]", value) or QUOTED_RE.search(value)
            or _structural_wrong_build(value)):
        return ("`discriminates` %r names no concrete wrong build or negative case: say what the wrong build does, e.g. a "
                "stub that returns an empty list" % value[:60])
    if _overlap(value, pass_rule) >= OVERLAP_FAIL:
        return "`discriminates` is nearly the pass rule reworded: say what a WRONG build would show instead"
    return None


def _overlap(value, pass_rule):
    ours, theirs = _content_words(value), _content_words(pass_rule)
    return len(ours & theirs) / len(ours) if ours else 0.0


def discriminates_overlap_warning(value, pass_rule):
    """A warning (not a failure) when a wrong build shares most of its words with the pass rule: a wrong build is often
    the right one with one thing changed, so only a near copy (OVERLAP_FAIL) is refused."""
    if OVERLAP_LIMIT <= _overlap(_normal(value), pass_rule) < OVERLAP_FAIL:
        return ("`discriminates` repeats much of the pass rule: check it names what a WRONG build shows, not the right "
                "build's result again")
    return None


FILLER_REASON_WORDS = {"n/a", "na", "ok", "okay", "none", "tbd", "todo", "nothing", "no", "because", "reason", "same",
                       "n", "a", "fine", "x", "yes"}
MIN_UNCHANGED_REASON = 20


def unchanged_reason_ok(reason):
    reason = _normal(reason)
    real = [w for w in re.findall(r"[a-z0-9/]+", reason.lower()) if w not in FILLER_REASON_WORDS]
    return len(reason) >= MIN_UNCHANGED_REASON and len(real) >= 3


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
                bad(ident, "bad-id", "id %r is not valid: the grammar is S<number> (behaviour) or U<number> (unchanged), with an "
                    "optional lowercase letter to add one later, e.g. S1, U3, S11b" % sid)
            if sid in seen:
                bad(ident, "duplicate-id", "id %s is used twice" % sid)
            seen[sid] = scenario
        covers = _normal(scenario.get("covers", ""))
        if covers and not COVERS_RE.match(covers):
            bad(ident, "bad-covers", "covers %r is not valid: write DW-<n> (a Done-when clause, 1 to %d) or PC-<n> (a plan "
                "constraint), exactly one, e.g. DW-1" % (covers, max(len(clauses), 1)))
        elif covers.startswith("DW-") and clauses and not 1 <= int(covers[3:]) <= len(clauses):
            bad(ident, "covers-unknown-clause",
                "covers %s but the Done-when has %d clause(s)%s" % (covers, len(clauses), " (a Done-when written as one "
                "sentence counts as 1 clause: declare the clauses in a `clauses:` block, see docs/plan-template.md)"
                if len(clauses) == 1 else ""))
        if _normal(scenario.get("text", "")) and len(_normal(scenario["text"])) < MIN_TEXT_CHARS:
            bad(ident, "short-text", "`text` is too short to say what the scenario is")
        proof, evaluator = _normal(scenario.get("proof", "")), _normal(scenario.get("evaluator", ""))
        if proof and proof not in PROOFS:
            bad(ident, "bad-proof", "proof %r must be one of %s" % (proof, "|".join(PROOFS)))
        if evaluator in EVALUATOR_ALIASES:
            warnings.append({"id": ident, "code": "evaluator-alias", "message": "%s: evaluator %r is a deprecated alias of "
                             "%r: rename it" % (ident, evaluator, EVALUATOR_ALIASES[evaluator])})
            evaluator = EVALUATOR_ALIASES[evaluator]
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
            else:
                note = discriminates_overlap_warning(scenario["discriminates"], scenario.get("pass", ""))
                if note:
                    warnings.append({"id": ident, "code": "discriminates-overlap", "message": "%s: %s" % (ident, note)})

    for number in range(1, len(clauses) + 1):
        tag = "DW-%d" % number
        covering = [s for s in scenarios if _normal(s.get("covers", "")) == tag and _normal(s.get("id", "")).startswith("S")]
        if not covering:
            bad("-", "uncovered-clause", "%s is not covered by any S scenario: %s" % (tag, clauses[number - 1][:80]))
    if not clauses:
        bad("-", "no-done-when", "the ask has no Done-when to cover: coverage cannot be checked")
    if not any(_normal(s.get("id", "")).startswith("U") for s in scenarios):
        reason = UNCHANGED_NONE_RE.search(text or "")
        if not (reason and unchanged_reason_ok(reason.group(1))):
            bad("-", "no-unchanged-scenario",
                "no U (unchanged) scenario, and the plan does not say `unchanged: none because <reason>`")
    constraints = declared_constraints(text)
    covered_pc = {int(_normal(s_.get("covers", ""))[3:]) for s_ in scenarios if re.match(r"^PC-[0-9]+$", _normal(s_.get("covers", "")))}
    if constraints:
        for number in sorted(covered_pc - constraints):
            bad("-", "covers-unknown-constraint", "a scenario covers PC-%d but the plan declares no such constraint "
                "(PC-n is a plan constraint, a `PC-<n>: ...` line in the plan's constraints section)" % number)
        for number in sorted(constraints - covered_pc):
            bad("-", "uncovered-constraint", "PC-%d is declared in the plan but no scenario covers it (`covers: PC-%d`)" % (number, number))
    elif covered_pc:
        warnings.append({"id": "-", "code": "no-constraint-list",
                         "message": "scenarios cover PC-%s but the plan declares no constraint list (`PC-<n>: ...` lines): "
                                    "PC-n is a plan constraint, so what it covers cannot be checked" % ", PC-".join(str(n) for n in sorted(covered_pc))})
    sliced = {}
    for scenario in scenarios:
        if _normal(scenario.get("slice", "")):
            sliced[_normal(scenario["slice"])] = sliced.get(_normal(scenario["slice"]), 0) + 1
    if sliced:
        for name, count in sorted(sliced.items()):
            if count > GUIDANCE_SCENARIOS:
                warnings.append({"id": "-", "code": "many-scenarios", "message": "slice %r has %d scenarios (guidance: at "
                                 "most %d per slice): split it" % (name, count, GUIDANCE_SCENARIOS)})
        if len(scenarios) > GUIDANCE_TOTAL_WITH_SLICES:
            warnings.append({"id": "-", "code": "many-scenarios", "message": "%d scenarios in total (guidance with slices: "
                             "at most %d): split the plan" % (len(scenarios), GUIDANCE_TOTAL_WITH_SLICES)})
    elif len(scenarios) > GUIDANCE_SCENARIOS:
        warnings.append({"id": "-", "code": "many-scenarios",
                         "message": "%d scenarios (guidance: about %d with no slices): give each scenario a `slice` and the "
                                    "limit applies per slice" % (len(scenarios), GUIDANCE_SCENARIOS)})
    return defects, warnings


# ----------------------------------------------------------------------------------- the content hash

def _id_key(ident):
    """Display and hash order: letter, number, then the suffix (S11 < S11b < S12)."""
    found = re.match(r"^([A-Za-z]*)([0-9]*)([a-z]?)$", ident)
    return (found.group(1), int(found.group(2) or 0), found.group(3), ident) if found else (ident, 0, "", ident)


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


def plan_hash(text):
    """sha256 of the WHOLE plan text, whitespace collapsed. Approval (G0c) and consensus (G0b) bind to this one: an edit
    to constraints, scope or risks changes it as well as an edit to a scenario."""
    return "sha256:" + hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------------- the whole check

def _clause_key(clause):
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", clause.lower()).split())


def check(text, done_when, plan_done_when=None):
    """The result of checking `text` (the plan) against the ask's `done_when` line (authoritative):
    {"result": "pass"|"fail", "scenario_hash", "plan_hash", "scenarios", "clauses", "defects", "warnings"}.
    `plan_done_when` is the Done-when the plan states for itself, when it does: it must be the same clauses (a plan never
    weakens coverage by copying fewer). Raises PlanFormatError when the plan cannot be read."""
    scenarios = parse_scenarios(scenario_block(text))
    clauses = clauses_of(done_when)
    declared = declared_clauses(text)
    extra = []
    if declared is not None:
        if len(clauses) > 1:
            # the ask already has numbered parts: they are the authority, a declared list can only repeat them
            if len(declared) != len(clauses) or not verify_declared(declared, done_when):
                extra.append({"id": "-", "code": "declared-clauses-mismatch", "message": "declared clauses do not match the "
                              "ask: the ask has %d numbered clauses and the plan declares %d (a plan can never merge or drop "
                              "clauses). The ask has: %r" % (len(clauses), len(declared), _normal(done_when)[:160])})
        elif not _words(done_when):
            extra.append({"id": "-", "code": "no-done-when", "message": "the plan declares clauses but the ask has no "
                          "Done-when to check them against"})
        elif verify_declared(declared, done_when):
            clauses = declared   # the declared split of the ask's own text
        else:
            extra.append({"id": "-", "code": "declared-clauses-mismatch", "message": "declared clauses do not match the ask: "
                          "joined in order they must reproduce the Done-when text (case, spacing, punctuation and `(n)` "
                          "numbers are ignored). The ask has: %r" % _normal(done_when)[:160]})
    defects, warnings = validate(scenarios, clauses, text)
    defects = [d for d in defects if not (extra and d["code"] == "no-done-when")] + extra
    if declared is None and len(clauses) == 1 and len(re.findall(r"[;,]", clauses[0])) >= 3 and not re.search(r"\([0-9]+\)", done_when or ""):
        warnings.append({"id": "-", "code": "one-clause-many-parts", "message": "the Done-when counts as 1 clause but has %d "
                         "commas or semicolons: if it holds several clauses, declare them in a `clauses:` block (see "
                         "docs/plan-template.md), or the coverage check only asks for one" % len(re.findall(r"[;,]", clauses[0]))})
    if plan_done_when and clauses:
        own = clauses_of(plan_done_when)
        if [_clause_key(c) for c in own] != [_clause_key(c) for c in clauses] and _words(plan_done_when) != _words(done_when):
            defects.append({"id": "-", "code": "done-when-differs",
                            "message": "the plan states %d Done-when clause(s) but the ask has %d, or they differ: the ask's "
                                       "Done-when is the authority, copy it exactly" % (len(own), len(clauses))})
    return {"result": "fail" if defects else "pass", "scenario_hash": content_hash(scenarios, clauses, text),
            "plan_hash": plan_hash(text), "scenarios": len(scenarios), "clauses": clauses, "defects": defects,
            "warnings": warnings}


GROUP_LINES = 5   # defect lines shown per kind in the text output; --json has them all


def render(result, source):
    """The lines `plan check` prints: defects grouped by kind with counts, at most GROUP_LINES lines each."""
    lines = ["plan check: %s" % result["result"].upper(), "source: %s" % source,
             "scenarios: %d; Done-when clauses: %d" % (result["scenarios"], len(result["clauses"])),
             "scenario hash: %s" % result["scenario_hash"],
             "plan hash: %s (approval and consensus bind to this)" % result["plan_hash"]]
    groups = {}
    for defect in result["defects"]:
        groups.setdefault(defect["code"], []).append(defect)
    if groups:
        lines.append("defects: %s" % ", ".join("%s x%d" % (code, len(items)) for code, items in groups.items()))
    for code, items in groups.items():
        lines.append("%s x%d" % (code, len(items)))
        lines.extend("  DEFECT %s: %s" % (d["id"], d["message"]) for d in items[:GROUP_LINES])
        if len(items) > GROUP_LINES:
            lines.append("  ... and %d more (use --json for all)" % (len(items) - GROUP_LINES))
    lines.extend("WARN: %s" % w["message"] for w in result["warnings"])
    return lines
