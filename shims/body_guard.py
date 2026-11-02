"""The one rule for a pull request body written over an old one (pure: it reads no file and runs no process).

check(old, new, dropped=()) returns None, or ONE refusal line. It refuses a new body that is empty or only whitespace, a new body that is a proper prefix of
the old one (the write was cut off), and a new body that lacks a keyed line of the old body: a line that begins with an all-capitals word and a colon, held
when some line of the new body begins with the same key (the words after the colon may change). A key the write names in `dropped` may be lacking.
"""
import re

KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*(-[A-Z0-9]+)*:", re.M)


def keys_of(text):
    """The keys (word and colon) that begin a line of text, in order of first appearance."""
    out = []
    for found in KEY_RE.finditer(text or ""):
        if found.group(0) not in out:
            out.append(found.group(0))
    return out


def check(old, new, dropped=()):
    old = old if isinstance(old, str) else ""
    new = new if isinstance(new, str) else ""
    if not new.strip():
        return "the new body is empty, so it would erase the old one"
    if new != old and old.startswith(new):
        return "the new body is only the first part of the old one (it was cut off)"
    kept = set(keys_of(new))
    lacking = [k for k in keys_of(old) if k not in kept and k not in set(dropped)]
    if lacking:
        return ("the new body lacks the line %s that the old body held" % ", ".join(lacking))
    return None
