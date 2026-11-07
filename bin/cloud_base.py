"""The `--base BRANCH` option of the cloud launch (ask #2305): a part of bin/cloud_worker.py (it keeps that file within its size row).

A launch may name the branch the worker's branch is created from. It is checked before anything is created, recorded in the worker record as `base`,
and the setup text the pod receives then says to create the branch from `origin/BASE` in place of the default branch. With no `--base` nothing here
changes: the setup text is today's text byte for byte. The option is NOT handed to the launcher (its `create` does not know it): only the text carries it.
`H` is the globals dictionary of bin/cloud_worker.py (set by `bind`, read at the call). It is loaded by bin/cloud_pr_labels.py's `bind`, whose hooks call it.
"""
import re

OPTION = "--base"
SENTENCE = "create it from the default branch if it does not exist yet"   # the one sentence of SETUP_PREAMBLE that names the default branch
H: dict = {}
_KEPT: dict = {}   # "orig": the preamble before this part changed it; "text": the preamble this part set


def bind(namespace):
    global H
    H = namespace


def base_of(args):
    """The launch's base as given (None when the option was not given; an empty string stays an empty string, to be refused)."""
    return getattr(args, "base", None)


def problem(base):
    """Why `base` is not a branch name of a repository ('' when it is): the branch rule (A-Z a-z 0-9 . _ / -, 1 to 200, no leading dash) and no `.`, `..` or empty path part."""
    if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", base) or base.startswith("-"):
        return "not a branch name"
    if any(part in ("", ".", "..") for part in base.split("/")):
        return "has an empty, `.` or `..` part"
    return ""


def check(args):
    """Refuse (exit 2, nothing created) a base that is not a branch name."""
    base = base_of(args)
    if base is not None and problem(base):
        raise H["Refusal"]("--base %r is not a branch name of the repository (%s); nothing was created" % (str(base)[:80], problem(base)))


def apply(record, args):
    """Record the base in the worker record and put it in the setup text of this launch; with no base, today's text (restored if an earlier launch of this process changed it)."""
    base = base_of(args)
    current = H["SETUP_PREAMBLE"]
    if current is _KEPT.get("text"):
        current = _KEPT["orig"]
    if base is None:
        H["SETUP_PREAMBLE"] = current
        return
    text = current.replace(SENTENCE, "create it from origin/%s (git fetch origin %s first) if it does not exist yet" % (base, base))
    if text == current:
        raise H["Refusal"]("the setup text has no default-branch sentence to replace with --base")
    _KEPT.update(orig=current, text=text)
    H["SETUP_PREAMBLE"] = text
    record["base"] = base
