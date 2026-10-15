"""The per-repository switch for the gh shim's agent marker (ask #417).

The shim (shims/gh) adds an invisible marker (`<!-- fleet-switchboard:from=NAME -->`) to the body of an issue, pull request or comment an agent writes.
The configuration key `gh_unmarked_repos` lists repositories (`owner/name`) and owner wildcards (`owner/*`) where it adds none. The list is read from
the switchboard's config.json only.

Everything here fails toward the marker: a config that is missing, unreadable or not a list, an entry that is not a plain `owner/name` or `owner/*`, a
repository the command names in a way this module cannot read with certainty, two different repositories named, no repository known, or any error at all,
leaves the marker exactly as it is today. Only a repository that is resolved with certainty and matches an entry turns it off. Matching is case-insensitive
(GitHub names are) and ignores one trailing `.git`; nothing else is normalised, so whitespace, a non-ASCII look-alike or a path variant is "not the same
repository" and keeps the marker."""

import json
import os
import re

KEY = "gh_unmarked_repos"
MAX_ENTRIES = 200
NAME = r"[A-Za-z0-9_.-]+"
ENTRY_RE = re.compile(r"(%s)/(%s|\*)" % (NAME, NAME))          # matched with fullmatch: a trailing newline is not a name
REPO_RE = re.compile(r"(%s)/(%s)" % (NAME, NAME))
URL_RE = re.compile(r"https://github\.com/(%s/%s)(?:/?|/(?:issues|pull)/[0-9]+(?:[/?#][^\s]*)?)" % (NAME, NAME))
GROUPS = {"issue", "pr"}


def validate(raw, source):
    """Raises ValueError naming the key: absent or null is the empty list; else a list of at most 200 "owner/name" or "owner/*" strings, each once."""
    if raw is None:
        return
    if (not isinstance(raw, list) or len(raw) > MAX_ENTRIES or not all(isinstance(r, str) and r.isascii() and ENTRY_RE.fullmatch(r) for r in raw)
            or any(r.split("/")[0] in (".", "..") for r in raw)):
        raise ValueError("%s: '%s' must be a list of at most %d \"owner/name\" or \"owner/*\" repository names, got %r" % (source, KEY, MAX_ENTRIES, raw))
    if len({r.lower() for r in raw}) != len(raw):
        raise ValueError("%s: '%s' names a repository twice" % (source, KEY))


def normal(repo):
    """`owner/name` lower-cased with one trailing `.git` off, or None when `repo` is not a plain ASCII owner/name."""
    if not isinstance(repo, str) or not repo.isascii() or not REPO_RE.fullmatch(repo):
        return None
    text = repo[:-4] if repo.lower().endswith(".git") else repo
    owner, _, name = text.partition("/")
    if not name or owner in (".", "..") or name in (".", ".."):
        return None
    return text.lower()


def entries(config):
    """The usable entries of a config, lower-cased: a bad entry is skipped (never matches), a non-list is empty."""
    raw = config.get(KEY) if isinstance(config, dict) else None
    if not isinstance(raw, list):
        return set()
    return {r.lower() for r in raw[:MAX_ENTRIES] if isinstance(r, str) and r.isascii() and ENTRY_RE.fullmatch(r)}


def listed(config, repo):
    """True when the repository `repo` (any spelling `normal` accepts) is named by an entry or covered by its owner's wildcard."""
    return listed_normal(config, normal(repo))


def listed_normal(config, want):
    """`listed` for a repository that is ALREADY normalised (normalising twice would strip a second `.git`)."""
    if want is None:
        return False
    found = entries(config)
    return want in found or "%s/*" % want.split("/")[0] in found


def load_config():
    """The switchboard's config.json as a dict, {} when it is missing, unreadable or not an object."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    try:
        with open(os.path.join(base, "fleet-switchboard", "config.json")) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _selector(value):
    """The normalised repository a `-R`/`--repo`/GH_REPO value or a target URL names, or None when it cannot be read with certainty."""
    if not isinstance(value, str):
        return None
    hit = URL_RE.fullmatch(value)
    return normal(hit.group(1)) if hit else normal(value)


def named_repo(args, value_flags, env=None):
    """(named, repo): `named` is True when the command names a repository at all (a -R/--repo flag, an issue or pull request URL target, or GH_REPO); `repo`
    is the one normalised repository it names, or None when it names two different ones or one this module cannot read. Values of other flags (a body, a
    title) are never read as a repository."""
    env = os.environ if env is None else env
    found, named, skip = [], False, False
    for i, arg in enumerate(args):
        if not isinstance(arg, str):
            return True, None
        if skip:
            skip = False
        elif arg == "--":
            break
        elif arg in ("-R", "--repo"):
            named = True
            found.append(_selector(args[i + 1]) if i + 1 < len(args) else None)
            skip = True
        elif arg.startswith("--repo="):
            named = True
            found.append(_selector(arg[len("--repo="):]))
        elif arg.startswith("-R") and not arg.startswith("--"):
            named = True
            found.append(_selector(arg[2:].lstrip("=")))
        elif arg in value_flags:
            skip = True
        elif arg.lower().startswith(("http://", "https://")):   # a URL target: read when it is a github.com issue or pull request, else "unreadable"
            named = True
            found.append(_selector(arg))
    if not named and env.get("GH_REPO"):
        named = True
        found.append(_selector(env.get("GH_REPO")))
    if not named:
        return False, None
    if len(set(found)) != 1 or found[0] is None:
        return True, None
    return True, found[0]


def marker_off(args, identity, value_flags, config=None, env=None):
    """True when the marker is to be left off this call: a write of an issue or pull request body (the shim decides which commands sign) whose ONE
    resolved repository is listed. `identity` is the shim's identity lookup answer ({"repo", "own"}); it is only used when the command names no repository
    and the caller has a repository of its own. Never raises: any trouble is False (the marker is added)."""
    try:
        config = load_config() if config is None else config
        if not entries(config):
            return False
        if len(args) < 3 or args[0] not in GROUPS:
            return False
        named, repo = named_repo(args, value_flags, env)
        if named:
            return listed_normal(config, repo)
        if not isinstance(identity, dict) or identity.get("own") is False:
            return False
        return listed(config, identity.get("repo"))
    except Exception:
        return False
