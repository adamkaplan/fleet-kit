"""box_precheck: the project's rules for .spire/box.json, see docs/box-precheck.md.

    python3 tools/box_precheck.py [PATH]        exit 0 when valid, else one line per problem and exit 1"""
import json
import sys



class Invalid(Exception):
    pass


def problems(doc):
    """The list of problems found in a parsed box.json (empty when valid)."""
    if not isinstance(doc, dict):
        return ["box.json must be an object"]
    out = []
    if doc.get("box") != 1:
        out.append("box must be 1")
    steps = doc.get("setup", [])
    if not isinstance(steps, list):
        return out + ["setup must be a list"]
    flags = doc.get("rewriteFlags", [])
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        return out + ["rewriteFlags must be a list of strings"]
    seen = set()
    for i, s in enumerate(steps):
        where = "setup[%d]" % i
        if not isinstance(s, dict):
            out.append(where + " must be an object")
            continue
        name, run = s.get("name"), s.get("run")
        if not isinstance(name, str) or not name:
            out.append(where + ": name must be a non-empty string")
        elif name in seen:
            out.append("%s: duplicate name %s" % (where, name))
        else:
            seen.add(name)
        if not isinstance(run, str) or not run.strip():
            out.append(where + ": run must be a non-empty string")
        if "when" in s and (not isinstance(s["when"], str) or not s["when"].strip()):
            out.append(where + ": when must be a non-empty string")
        if "refresh" in s and not isinstance(s["refresh"], bool):
            out.append(where + ": refresh must be true or false")
        if isinstance(run, str) and any(f in run.split() for f in flags) and not (isinstance(s.get("when"), str) and s["when"].strip()):
            out.append(where + ": a step that rewrites files (--fix) needs a when guard: it must not always run")
    return out


def load(path):
    """Parse and validate a box.json; return the document or raise Invalid with every problem."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise Invalid("%s: %s" % (path, e))
    found = problems(doc)
    if found:
        raise Invalid("\n".join(found))
    return doc


if __name__ == "__main__":
    try:
        load(sys.argv[1] if len(sys.argv) > 1 else ".spire/box.json")
    except Invalid as e:
        print(e)
        sys.exit(1)
