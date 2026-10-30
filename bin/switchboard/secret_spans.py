# ---- #1544: where the first secret-shaped match is, named by a fixed rule label and numbers only (never a matched character).
# A separate finder: redact_command_text and _SECRET_PATTERNS stay as they are. The names are a parallel table, one per pattern, in the same order.

SECRET_RULE_NAMES = ("bearer-or-basic-token", "credential-word-value", "known-key-prefix", "url-password", "jwt-shape")
SECRET_RULE_SEQUENCE = "rules-in-sequence"


def _secret_position(text, start, end):
    """(line, first column, last column), all counted from 1 in characters; a carriage return before a line feed is no column; a match
    that runs over the end of its first line is cut at that line's end."""
    line = text.count("\n", 0, start) + 1
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    line_end = len(text) if line_end < 0 else line_end
    if line_end > line_start and text[line_end - 1] == "\r":
        line_end -= 1
    last = max(min(end, line_end), start + 1)
    return line, start - line_start + 1, last - line_start


def find_secret_span(text):
    """(rule label, line, first column, last column) of the FIRST place `redact_command_text` would change `text`, or None when it changes nothing.
    A rule is tried on the original text; a body changed only because a later rule sees an earlier rule's output is `rules-in-sequence`,
    placed at the first character where the redacted text differs."""
    best = None
    for name, (pattern, replacement) in zip(SECRET_RULE_NAMES, _SECRET_PATTERNS):
        for match in pattern.finditer(text):
            if match.expand(replacement) != match.group(0):
                if best is None or match.start() < best[0]:
                    best = (match.start(), match.end(), name)
                break
    if best is not None:
        return (best[2],) + _secret_position(text, best[0], best[1])
    redacted = redact_command_text(text)
    if redacted == text:
        return None
    at = next((i for i, (a, b) in enumerate(zip(text, redacted)) if a != b), min(len(text), len(redacted)))
    return (SECRET_RULE_SEQUENCE,) + _secret_position(text, at, at + 1)


def secret_where(text):
    """The fixed words a refusal adds: the rule label, the line and the columns. No character of the text."""
    found = find_secret_span(text)
    if found is None:
        return "no position found"
    return "rule %s, line %d, columns %d to %d" % found
