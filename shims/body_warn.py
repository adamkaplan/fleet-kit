"""A warning, never a refusal, for GitHub text that is hard to read, for the fleet's gh shim (loaded by path, like repo_match.py).

`warning(args)` reads only the body text the command line carries inline (`--body`, `-b`; never a file, never stdin, so no stream the real
gh needs is touched) for `issue` and `pr` `create`, `edit`, `comment` and `pr review`. It starts no process and makes no network call.
Code fences and code spans are left out. It returns one line to print on stderr, or None when the text reads well or anything is in doubt
(a warning is not a guard: any doubt means no warning). Standard library only.
"""
import re

LONG_PARAGRAPH = 600
MAX_MARKERS = 3
COMMANDS = {("issue", "create"), ("issue", "edit"), ("issue", "comment"), ("pr", "create"), ("pr", "edit"), ("pr", "comment"), ("pr", "review")}
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
SPAN_RE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
ITEM_RE = re.compile(r"^\s{0,6}(?:[-*+]|\d{1,3}[.)])\s")
MARKER_RE = re.compile(r"\(\d{1,2}\)")
HINT = "gh: this text may be hard to read (%s): use short paragraphs and Markdown lists, one item per line (a warning only; the command ran)\n"
BODY_FLAGS = {"-b": "body", "--body": "body"}


def texts(args):
    """The inline texts of the body flags (`--body`, `-b`) among args[2:]. A `--body-file` or `-F` is never opened, statted or read: reading a FIFO, a
    process substitution or `-` would consume the stream the real gh needs."""
    found, i = [], 2
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            break
        name = value = None
        if arg in BODY_FLAGS:
            name, value = arg, args[i] if i < len(args) else None
            i += 1
        elif arg.startswith("--") and "=" in arg and arg.split("=", 1)[0] in BODY_FLAGS:
            name, value = arg.split("=", 1)
        elif arg[:2] == "-b" and not arg.startswith("--") and len(arg) > 2:
            name, value = arg[:2], arg[2:].lstrip("=") if arg[2] == "=" else arg[2:]
        if name is None or value is None:
            continue
        found.append(value)
    return found


def prose(text):
    """The text with fenced blocks and code spans taken out."""
    kept, fence = [], None
    for line in text.splitlines():
        match = FENCE_RE.match(line)
        if fence is None and match:
            fence = match.group(1)[0] * len(match.group(1))
        elif fence is not None:
            if match and match.group(1).startswith(fence):
                fence = None
        else:
            kept.append(line)
    return SPAN_RE.sub("", "\n".join(kept))


def segments(text):
    """The paragraphs of `text`; a list item is a paragraph of its own, so a long list is not one long paragraph."""
    out = []
    for block in re.split(r"\n\s*\n", text):
        current = []
        for line in block.split("\n"):
            if ITEM_RE.match(line) and current:
                out.append(" ".join(current))
                current = []
            current.append(line.strip())
        out.append(" ".join(current))
    return out


def problems(text):
    body = prose(text)
    found = []
    if "\\n" in body:
        found.append("a literal backslash-n")
    parts = segments(body)
    if any(len(part) > LONG_PARAGRAPH for part in parts):
        found.append("a paragraph over %d characters" % LONG_PARAGRAPH)
    if any(len(MARKER_RE.findall(part)) >= MAX_MARKERS for part in parts):
        found.append("inline (1) (2) (3) markers in one paragraph")
    return found


def warning(args):
    """One line for stderr, or None (nothing to say, or any doubt)."""
    try:
        if len(args) < 3 or (args[0], args[1]) not in COMMANDS:
            return None
        found = []
        for text in texts(args):
            for item in problems(text):
                if item not in found:
                    found.append(item)
        return HINT % ", ".join(found) if found else None
    except Exception:
        return None
