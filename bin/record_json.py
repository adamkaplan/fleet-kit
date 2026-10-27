"""The shared bounded-depth parse of record JSON the daemon did not write (#1314).

One named limit, RECORD_MAX_DEPTH, and one refusal (TooDeep, a ValueError) on every Python: the depth is counted by an iterative scan before
json.loads, so no reader depends on the interpreter's recursion limit. Same rules as cloud_worker.nesting_too_deep (brackets in strings and
escapes are not counted); the limit equals cloud_worker.WATCH_MAX_DEPTH.
"""
import json

RECORD_MAX_DEPTH = 64   # the deepest [ / { nesting a record may have (a real record is about 5 deep)


class TooDeep(ValueError):
    """The text nests deeper than RECORD_MAX_DEPTH."""


def nesting_too_deep(text, limit=None):
    limit = RECORD_MAX_DEPTH if limit is None else limit
    depth, in_string, escaped = 0, False, False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "[" or ch == "{":
            depth += 1
            if depth > limit:
                return True
        elif ch == "]" or ch == "}":
            depth -= 1
    return False


def parse_bounded(text):
    """json.loads of untrusted record text with the same depth limit on every Python. Raises ValueError (TooDeep for depth) and nothing else."""
    if nesting_too_deep(text):
        raise TooDeep("nested deeper than %d" % RECORD_MAX_DEPTH)
    try:
        return json.loads(text)
    except RecursionError:   # defence in depth
        raise TooDeep("recursion limit") from None
