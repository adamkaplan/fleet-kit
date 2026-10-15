"""Excluded repositories (ask #446): a repository the owner keeps out of the fleet's view is never watched and never read.

The list is the configuration key `excluded_repos` (a list of "owner/name"), matched case-insensitively, taken from the configuration only. It is applied where
the switchboard derives the set of watched repositories, so every consumer of that set (forwarders, the sweep, triage) honours it, and by the intent reader before
it makes a call. An agent working in an excluded repository still launches, sends, reports and retires: only watching and reading stop.

Also here: the one warning for an external-decisions key command that starts with `az` and names no `--subscription` (it used whichever subscription was active,
and failed when that changed). The warning names the key and the fix, never the command's other arguments (a vault and a secret name stay out)."""

import json
import os
import re

KEY = "excluded_repos"
MAX_ENTRIES = 200
MAPPING_MAX = 65536       # bytes of the mapping file read for the warning
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")   # matched with fullmatch: a trailing newline is not a repository name


def validate(raw, source):
    """Raises ValueError naming the key: absent or null is the empty list; else a list of at most 200 "owner/name" strings, each once (case-insensitively)."""
    if raw is None:
        return
    if not isinstance(raw, list) or len(raw) > MAX_ENTRIES or not all(isinstance(r, str) and REPO_RE.fullmatch(r) for r in raw):
        raise ValueError("%s: '%s' must be a list of at most %d \"owner/name\" repository names, got %r" % (source, KEY, MAX_ENTRIES, raw))
    if len({r.lower() for r in raw}) != len(raw):
        raise ValueError("%s: '%s' names a repository twice" % (source, KEY))


def excluded_set(config):
    """The excluded repositories of a configuration, lower-cased (empty when the key is absent or not a list)."""
    raw = (config or {}).get(KEY)
    return {r.lower() for r in raw if isinstance(r, str)} if isinstance(raw, list) else set()


def is_excluded(config, repo):
    return isinstance(repo, str) and repo.lower() in excluded_set(config)


def without_excluded(config, repos):
    """`repos` with the excluded ones taken out (order kept)."""
    out = excluded_set(config)
    return [r for r in repos if not (isinstance(r, str) and r.lower() in out)] if out else list(repos)


def _commands(mapping):
    """The argv of every command key source in a mapping: `key_source` is one source or a list of them (a rotation); a source is {"command": [argv...]}
    or the string form `command:[...]` (a JSON list), the two forms `external_adapter.parse_key_source` accepts."""
    sources = mapping.get("key_source") if isinstance(mapping, dict) else None
    sources = sources if isinstance(sources, list) else [sources]
    for source in sources:
        command = None
        if isinstance(source, dict):
            command = source.get("command")
        elif isinstance(source, str) and source.startswith("command:"):
            try:
                command = json.loads(source[len("command:"):])
            except ValueError:
                command = None
        if isinstance(command, list) and command and all(isinstance(a, str) for a in command):
            yield command


def az_without_subscription(mapping_path):
    """True when the mapping file's key command starts with `az` and has no `--subscription` option (`--subscription=<id>` and `-s` count). Never raises: an
    unreadable or odd file, or one over the size cap (read truncated, so not valid JSON), says False."""
    try:
        with open(mapping_path) as handle:
            mapping = json.loads(handle.read(MAPPING_MAX))
        for command in _commands(mapping):
            if os.path.basename(command[0]) == "az" and not any(a == "--subscription" or a.startswith("--subscription=") or a == "-s" for a in command[1:]):
                return True
    except (OSError, ValueError):
        pass
    return False


AZ_WARNING = ("external_decisions: the key command in the mapping file starts with az and names no --subscription, so it uses whichever subscription is active "
              "and fails when that changes: add `--subscription <id>` to the command")
