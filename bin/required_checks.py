"""The checks a reader's brief requires, and whether a posted review comment answers each one (#2203).

A brief may hold ONE marked block: a line that is exactly `REQUIRED CHECKS:` followed by consecutive `- <check>` lines (the block ends at the
first line that is not one). At most MAX_CHECKS lines are read and each is cut to MAX_CHECK_CHARS; the brief is only read as text, never run.
A brief with no block gives the empty list, and the empty list changes nothing anywhere.

`compare` reads a review comment against the list. A check is ANSWERED when a line of the comment names it (case folded) and what follows
the name on that line is `:` or a dash and then a result, or `not done` with a reason. A bare mention, or `not done` with no reason, is MISSING. It never judges
whether an answer is right: that is for the lane. Pure: no I/O."""

import re

MARK = "REQUIRED CHECKS:"
MAX_CHECKS = 12
MAX_CHECK_CHARS = 120
MAX_BRIEF_CHARS = 200000
MAX_COMMENT_CHARS = 20000
_ITEM = re.compile(r"^[ \t]*[-*][ \t]+(\S.*?)[ \t]*$")
_NOT_DONE = re.compile(r"^not[ \t]+done\b", re.I)
_SEP = ":-\u2013\u2014"   # what must follow the name: `<check>: <result>` or `<check> - <result>`
_WRAP = " \t*_`)]"   # markup that may sit between the name and the separator
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_PAD = " \t:;,.=()[]*_`-\u2013\u2014>"


def parse(brief):
    """The check names of the brief's marked block, in order, without repeats; [] when there is none."""
    if not isinstance(brief, str):
        return []
    lines = brief[:MAX_BRIEF_CHARS].splitlines()
    for at, line in enumerate(lines):
        if line.strip() == MARK:
            found = []
            for text in lines[at + 1:]:
                item = _ITEM.match(text)
                if not item:
                    break
                name = " ".join(_CONTROL.sub("", item.group(1)).split())[:MAX_CHECK_CHARS]   # control characters out, so every reader gets a clean name
                if name and name not in found:
                    found.append(name)
                if len(found) >= MAX_CHECKS:
                    break
            return found
    return []


def _state(line, name):
    """'result', 'not_done', 'no_reason' or None for one comment line and one check name."""
    at = line.casefold().find(name.casefold())
    if at < 0:
        return None
    rest = line[at + len(name):].lstrip(_WRAP)
    if not rest or rest[0] not in _SEP:
        return None   # a bare mention, or a name followed by prose, gives no result
    rest = rest[1:].strip(_PAD)
    if _NOT_DONE.match(rest):
        return "not_done" if re.search(r"[A-Za-z0-9]", rest[len("not done"):]) else "no_reason"
    return "result" if re.search(r"[A-Za-z0-9]", rest) else None


def compare(body, checks):
    """{"answered": [...], "missing": [...], "not_done": [...]} for a comment body and the required list; `not_done` is the part of `answered`
    that says `not done` with a reason. An empty list gives all three empty."""
    lines = (body[:MAX_COMMENT_CHARS] if isinstance(body, str) else "").splitlines()
    out = {"answered": [], "missing": [], "not_done": []}
    for name in checks or []:
        states = {s for s in (_state(line, name) for line in lines) if s}
        if "result" in states:
            out["answered"].append(name)
        elif "not_done" in states:
            out["answered"].append(name)
            out["not_done"].append(name)
        else:
            out["missing"].append(name)
    return out


def notice(body, checks):
    """The one sentence added to a review comment's notice, or "" when the reader's launch record holds no list."""
    if not checks:
        return ""
    found = compare(body, checks)
    text = "required checks: %d answered" % len(found["answered"])
    if found["not_done"]:
        text += " (not done, with a reason: %s)" % ", ".join(found["not_done"])
    text += "; MISSING: %s" % (", ".join(found["missing"]) if found["missing"] else "none")
    return text
