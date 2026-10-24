#!/usr/bin/env python3
"""Which macOS legs a test-workflow run has, and how the gates read them (ask #1235). Pure: no network, no git, no clock.

`plan_matrix` is the decision the workflow's `plan` step makes for a pull request into the default branch (the step calls this file's CLI:
`python3 bin/macos_plan.py --event pull_request --action synchronize --base main --files-nul-file diff.z --patterns-file list.txt`).
`legs_reading` is what gate G4 and the gate pass read: the ubuntu legs green at the head, and the macOS legs green at the head or on the gate pass's
merge-ref run of that head. A leg that is absent, skipped, cancelled or neutral is never green: only the conclusion `success` is.
"""
import fnmatch
import json
import sys

DEFAULT_BRANCH = "main"
LABEL = "needs-mac"
UBUNTU = "ubuntu-latest"
MACOS = "macos-latest"
LEGS = ("test", "test-cloud")
# Hard-coded beside the list, as the integration rule does: an edit of the list or of the workflows always runs macOS.
ALWAYS = (".github/macos-platform-paths.txt", ".github/" + "workflows/" + "**")   # built, not one literal: the owner-hold matrix names that path, and no code names it as a string
REQUIRED_GROUPS = {"shims": ("shims/",), "plugins": ("plugins/",), "install-and-service": ("INSTALL.md", "bin/fleet-doctor", "bin/fleet-heartbeat", "templates/")}


def parse_patterns(text):
    """The glob patterns of the platform-path list file: no blank lines, no comments (`# group:` lines included)."""
    return [l.strip() for l in (text or "").splitlines() if l.strip() and not l.lstrip().startswith("#")]


def groups_missing(patterns):
    """The names of the required groups (shims, plugins, install and service files) that no pattern of the list covers."""
    missing = []
    for name, needles in REQUIRED_GROUPS.items():
        if not all(any(p == n or p.startswith(n) for p in patterns) for n in needles):
            missing.append(name)
    return missing


def touches(files, patterns):
    return any(fnmatch.fnmatchcase(f, p) for f in files for p in list(patterns) + list(ALWAYS))


def plan_matrix(event, action="", base="", draft=False, labels=(), files=(), patterns=(), default=DEFAULT_BRANCH):
    """{"macos": bool, "reason": str} for a run, or {"macos": None, ...} for a pull request into another branch (the workflow's own rule decides it).
    Push, dispatch and the nightly: both macOS legs. A pull request into the default branch: `reopened` (the gate pass) always; a draft never;
    else only when the label `needs-mac` is set or the diff touches a platform path; an empty list fails safe (macOS runs)."""
    if event != "pull_request":
        return {"macos": True, "reason": "%s: both legs" % (event or "run")}
    if base != default:
        return {"macos": None, "reason": "a pull request into %s: unchanged, the integration rule decides" % (base or "another branch")}
    if LABEL in list(labels or []):   # a label always runs macOS, a draft included
        return {"macos": True, "reason": "the label %s is set" % LABEL}
    if action == "reopened":
        return {"macos": True, "reason": "reopened (the gate pass's run on the merge ref): every leg"}
    if draft:
        return {"macos": False, "reason": "a draft skips macOS until ready_for_review"}
    if not parse_patterns("\n".join(patterns)):
        return {"macos": True, "reason": "the platform-path list holds no pattern: running macOS (fail safe)"}
    if not [f for f in files if f]:
        return {"macos": True, "reason": "the diff lists no file: running macOS (fail safe)"}
    hit = [f for f in files if touches([f], patterns)]
    if hit:
        return {"macos": True, "reason": "the diff touches a platform path (%s)" % hit[0]}
    return {"macos": False, "reason": "no platform path in the diff and no %s label: ubuntu legs only; the gate pass's run on the merge ref runs macOS" % LABEL}


def rows(event, macos, refs=("main",)):
    """The matrix rows the workflow uses (same shape as the inline script)."""
    oses = [UBUNTU] + ([MACOS] if macos else [])
    if event == "schedule":
        return [dict(os=o, ref=r, name="nightly %s (%s)" % (r, o), cloud="nightly-cloud %s (%s)" % (r, o)) for r in refs for o in (UBUNTU, MACOS)]
    return [dict(os=o, ref="", name="test (%s)" % o, cloud="test-cloud (%s)" % o) for o in oses]


def concurrency_group(workflow, event, number, sha, action=""):
    """The workflow's concurrency group (a model of the expression in .github/workflows/test.yml): a pull request's runs share one group, so a later push cancels a
    superseded run; a `reopened` run (the gate pass's) has a group of its own, so a later push of the same head never cancels it. A push or dispatch groups by its sha."""
    base = "test-%s-%s" % (workflow, number or sha)
    return base + ("-reopened" if event == "pull_request" and action == "reopened" else "")


def cancelled_by(old, new):
    """True when the newer run `new` cancels the in-progress run `old`; each is (event, number, sha, action). cancel-in-progress is on for pull request events only."""
    group = lambda r: concurrency_group("test", r[0], r[1], r[2], r[3])
    return new[0] == "pull_request" and group(old) == group(new)


def _by_name(checks):
    """{name: conclusion}. The same name may be listed twice (the head run and the reopened run, in no guaranteed order): ANY non-success of a duplicate wins, so the
    order of the list never decides and a duplicate never reads green when one of them is not."""
    out = {}
    for c in checks or []:
        if isinstance(c, dict) and isinstance(c.get("name"), str):
            word = str(c.get("conclusion") or "").lower()
            if c["name"] not in out or out[c["name"]] == "success":
                out[c["name"]] = word
    return out


def has_legs(names):
    """True when any name is a leg of the test workflow (`test (<os>)` or `test-cloud (<os>)`): the readings then apply, and a missing ubuntu leg is refused, never skipped over."""
    return any(isinstance(n, str) and any(n.startswith(leg + " (") for leg in LEGS) for n in names or [])


def _bad(found, os_name):
    """[the legs of `os_name` that are not success, each as `name (state)`]; an absent leg is `absent`."""
    return ["%s (%s)" % (n, found.get(n) or "absent") for n in ("%s (%s)" % (leg, os_name) for leg in LEGS) if found.get(n) != "success"]


def legs_reading(head_checks, merge_ref_checks=None):
    """(ok, which, detail). `head_checks`: the checks [{"name","conclusion"}] at the head; `merge_ref_checks`: the same for the gate pass's run on the
    merge ref of that head (None when there is none). ok only when the ubuntu legs are green at the head and the macOS legs are green at the head
    ("head") or on the merge-ref run ("merge ref"). Only `success` is green."""
    head = _by_name(head_checks)
    ubuntu = _bad(head, UBUNTU)
    if ubuntu:
        return False, "none", "the ubuntu legs are not green at the head: " + ", ".join(ubuntu)
    at_head = _bad(head, MACOS)
    if not at_head:
        return True, "head", "ubuntu and macOS legs green at the head"
    failed = [n for n in at_head if "(failure)" in n or "(timed_out)" in n]
    if failed:
        return False, "none", "a macOS leg failed at the head: " + ", ".join(failed)
    if merge_ref_checks is None:
        return False, "none", "no macOS leg green at the head (%s) and no gate-pass run on the merge ref: needs `merge-gate --pass`, or the label %s" % (", ".join(at_head), LABEL)
    on_ref = _bad(_by_name(merge_ref_checks), MACOS)
    if on_ref:
        return False, "none", "the macOS legs are not green on the merge-ref run: " + ", ".join(on_ref)
    return True, "merge ref", "ubuntu legs green at the head; macOS legs green on the gate pass's run on the merge ref of that head"


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--event", required=True)
    ap.add_argument("--action", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--draft", default="false")
    ap.add_argument("--labels", default="", help="comma-separated")
    ap.add_argument("--files", default="", help="newline-separated changed paths (a comma belongs to a name)")
    ap.add_argument("--files-nul-file", default="", help="a file of NUL-separated changed paths (git diff -z), read as UTF-8")
    ap.add_argument("--patterns-file", default="")
    a = ap.parse_args(argv)
    patterns = parse_patterns(open(a.patterns_file).read()) if a.patterns_file else []
    files = [f for f in a.files.split("\n") if f]
    if a.files_nul_file:
        files = [f.decode("utf-8", "surrogateescape") for f in open(a.files_nul_file, "rb").read().split(b"\0") if f]
    out = plan_matrix(a.event, a.action, a.base, a.draft == "true", [l for l in a.labels.split(",") if l], files, patterns)
    print(json.dumps(out, separators=(",", ":")))
    return 0 if out["macos"] is not None else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
