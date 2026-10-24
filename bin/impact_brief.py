"""The impact brief (#1215, slice A): what a held change can do, built from the DIFF and the records only. Pure and deterministic: the same
facts give byte-identical text, no clock, no network. A piece that cannot be read is "not shown": it is never filled, never counts as fine, and
recommends the owner. The PR's own text (`body_claim`) is never used as evidence. The brief is advice for the Chief of Staff; it lifts no backstop."""

import hashlib

NOT_SHOWN = "not shown"
MAX_LINES, MAX_WIDTH = 24, 100

# every table below is checked by a completeness test against its use in build_brief
MATERIAL = ("credentials_widened", "issuance_widened", "spend", "irreversible", "speaks_for_owner", "authority_widened")
MATERIAL_WORDS = {"credentials_widened": "credentials or issuance widened" , "issuance_widened": "credentials or issuance widened",
                  "spend": "spend", "irreversible": "irreversible", "speaks_for_owner": "speaks for the owner",
                  "authority_widened": "authority widened"}
# path class -> material risk it carries when the diff ADDS to it
CLASS_RISK = {"credentials": "credentials_widened", "identity": "credentials_widened", "issuance": "issuance_widened",
              "authority": "authority_widened", "spend": "spend", "release": "irreversible", "owner_voice": "speaks_for_owner"}
# path class -> stated ground when every file is of such classes
CLASS_GROUND = {"tests": "tests-only", "docs": "docs-only"}
# classes with no material risk of their own and no stated ground (ordinary code, the review fleet's definitions)
PLAIN_CLASSES = ("code", "review_definitions")
# classes of the clear-and-hold machinery's own decision of WHO may clear: owner whatever the diff says
OWNER_ONLY_CLASSES = ("merge_shim", "clear_machinery")
KNOWN_CLASSES = tuple(sorted(set(CLASS_RISK) | set(CLASS_GROUND) | set(PLAIN_CLASSES) | set(OWNER_ONLY_CLASSES)))
PIECE_NAMES = ("files", "classes", "ci", "reads", "rating", "mutants", "blast")
DECIDERS = ("chief_of_staff", "owner")


def _s(value):
    return "".join(c if c.isprintable() else "?" for c in str(value))


def _cut(text, width=MAX_WIDTH):
    text = _s(text)
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def _files(facts):
    files = facts.get("files")
    if not isinstance(files, list) or not files:
        return None
    out = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            return None   # one unreadable entry makes the whole file list unreadable
        add, dele = f.get("additions"), f.get("deletions")
        out.append({"path": f["path"], "additions": add if isinstance(add, int) and not isinstance(add, bool) else None,
                    "deletions": dele if isinstance(dele, int) and not isinstance(dele, bool) else None, "status": f.get("status")})
    return sorted(out, key=lambda f: f["path"])


def _classes(facts, files):
    classes = facts.get("classes")
    if files is None or not isinstance(classes, dict):
        return None
    out = {}
    for f in files:
        cls = classes.get(f["path"])
        out[f["path"]] = cls if cls in KNOWN_CLASSES else None   # an unnamed class is unknown, never "no loosening"
    return out


def _ci(facts):
    ci = facts.get("ci")
    if not isinstance(ci, dict) or ci.get("state") not in ("green", "red", "pending", "unknown") or ci.get("head") != facts.get("head"):
        return NOT_SHOWN   # a CI result of another head says nothing about this one
    return "%s at %s" % (ci["state"], _s(ci["head"])[:8]) if ci["state"] != "unknown" else NOT_SHOWN


def _reads(facts):
    reads = facts.get("reads")
    head = facts.get("head")
    if not isinstance(reads, list):
        return NOT_SHOWN
    at = sorted({_s(r.get("by")) for r in reads if isinstance(r, dict) and r.get("head") == head and r.get("verdict") in ("pass", "approve", "ok", "clear")})
    return "%d independent read%s at head (%s)" % (len(at), "" if len(at) == 1 else "s", ", ".join(at) or "none")


def _simple(value):
    if value is None or value == "" or value == []:
        return NOT_SHOWN
    if isinstance(value, dict):
        return ", ".join("%s=%s" % (k, _s(value[k])) for k in sorted(value)) or NOT_SHOWN
    return _s(value)


def build_brief(facts):
    facts = facts if isinstance(facts, dict) else {}
    head = facts.get("head") if isinstance(facts.get("head"), str) else ""
    files, base = _files(facts), _s(facts.get("base") or NOT_SHOWN)
    classes = _classes(facts, files)
    pieces = {"files": NOT_SHOWN if files is None else "%d file(s), +%d -%d" % (len(files), sum(f["additions"] or 0 for f in files), sum(f["deletions"] or 0 for f in files)),
              "classes": NOT_SHOWN, "ci": _ci(facts), "reads": _reads(facts), "rating": _simple(facts.get("rating")),
              "mutants": _simple(facts.get("mutants")), "blast": _simple(facts.get("blast"))}
    material, loosening, owner_why = set(), [], None
    if classes is not None:
        unknown = sorted(p for p, c in classes.items() if c is None)
        if not unknown:
            pieces["classes"] = ", ".join("%s=%d" % (c, n) for c, n in sorted({c: sum(1 for v in classes.values() if v == c) for c in set(classes.values())}.items()))
        for f in files:
            cls = classes[f["path"]]
            if cls in CLASS_RISK and (f["additions"] is None or f["additions"] > 0 or f["status"] in ("added", "renamed", "copied", "modified-unknown")):
                material.add(CLASS_RISK[cls])
                loosening.append(f["path"])
            elif cls in CLASS_RISK and f["additions"] is None:
                pass
            if cls in OWNER_ONLY_CLASSES:
                owner_why = "it changes who may clear or merge (%s)" % _s(f["path"])
    material = [m for m in MATERIAL if m in material]
    loosening = sorted(set(loosening))
    gaps = sorted(n for n in PIECE_NAMES if pieces[n] == NOT_SHOWN)
    if material:
        decider, why = "owner", "%s; only the owner may accept that" % " and ".join(sorted({MATERIAL_WORDS[m] for m in material}))
        why = "the diff shows %s: it is the owner's" % why.split(";")[0]
    elif gaps:
        decider, why = "owner", "not shown: %s, and a missing piece never counts as fine" % ", ".join(gaps)
    elif owner_why:
        decider, why = "owner", owner_why
    else:
        kinds = {classes[f["path"]] for f in files}
        grounds = sorted({CLASS_GROUND[c] for c in kinds if c in CLASS_GROUND})
        if kinds <= set(CLASS_GROUND):
            ground = grounds[0] if len(grounds) == 1 else "tests and docs only"
        elif kinds <= set(CLASS_GROUND) | set(PLAIN_CLASSES):
            ground = "no material risk read from the diff"
        else:
            ground = "tightening only: the diff adds nothing to a sensitive path"
        decider, why = "chief_of_staff", "%s, with every piece shown" % ground
    can_do = NOT_SHOWN if files is None else "; ".join(_s(f["path"]) for f in (sorted(files, key=lambda f: (f["path"] not in loosening, f["path"]))[:3])) + (" and %d more" % (len(files) - 3) if len(files) > 3 else "")
    widens = NOT_SHOWN if files is None or classes is None or any(c is None for c in classes.values()) else ("; ".join(loosening[:3]) or "nothing the diff adds to a sensitive path")
    undo = NOT_SHOWN if files is None or classes is None else ("no: irreversible" if "irreversible" in material else "yes: revert the pull request")
    risks = NOT_SHOWN if classes is None or any(c is None for c in classes.values()) else (", ".join(sorted({MATERIAL_WORDS[m] for m in material})) or "none read from the diff")
    lines = ["IMPACT BRIEF %s against %s" % (head[:8] or NOT_SHOWN, base),
             "CAN DO: %s" % can_do, "WIDENS OR REMOVES: %s" % widens, "UNDO: %s" % undo, "RISKS (credentials, spend, speaks for owner): %s" % risks,
             "CHANGE: %s" % pieces["files"], "CLASSES: %s" % pieces["classes"], "READS: %s" % pieces["reads"], "CI: %s" % pieces["ci"],
             "RATING: %s" % pieces["rating"], "MUTANTS RUN: %s" % pieces["mutants"], "BLAST RADIUS: %s" % pieces["blast"]]
    claim = facts.get("body_claim")
    if isinstance(claim, str) and claim.strip():
        lines.append("PR TEXT: a claim was made; it is not evidence and is not used")
    lines += ["RECOMMENDED DECIDER: %s" % ("Chief of Staff" if decider == "chief_of_staff" else "owner"), "WHY: %s." % why.rstrip(".")]
    text = "\n".join(_cut(l) for l in lines[:MAX_LINES]) + "\n"
    return {"head": head, "pieces": pieces, "material": material, "loosening": loosening, "decider": decider, "why": why.rstrip(".") + ".",
            "text": text, "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def render_brief(brief):
    return brief["text"]


def classify(path, top_tier_hits):
    """The class of one changed path: a matrix top tier class it hits, else tests, docs or code by place. A class the matrix names that this module does not know stays unknown (owner)."""
    for cls in sorted(top_tier_hits or {}):
        if path in (top_tier_hits[cls] or []):
            return cls
    if path.startswith("tests/") or "/tests/" in path or "/test-" in path or path.startswith("bin/test-"):
        return "tests"
    if path.startswith("docs/") or path.endswith(".md"):
        return "docs"
    return "code"


def facts_from_report(raw_files, report):
    """Facts for build_brief from `pr_floor_report`'s own reads. Reads and rating come from the report (the ledger's records at this head); CI, mutants and blast radius are not read here yet: they stay absent, so they print `not shown` and recommend the owner."""
    hits = report.get("top_tier_hits") or {}
    files = [{"path": f.get("path"), "additions": f.get("additions"), "deletions": f.get("deletions"), "status": str(f.get("changeType") or "modified").lower()}
             for f in raw_files if isinstance(f, dict)]
    complete = report.get("complete") is True
    return {"files": files if complete else None, "ci": None, "reads": report.get("reads"), "rating": report.get("rating"), "body_claim": None, "head": report.get("head") or "", "base": report.get("base") or "",
            "classes": {f["path"]: classify(f["path"], hits) for f in files if isinstance(f["path"], str)}}
