# ---- ask #2320: the repositories that land by `spire land` (no pull request). One switch, the top-level config key `land_repos`.
# A list of "owner/repo" or "owner/*" globs; absent means LAND_REPOS_DEFAULT; an empty list means every repository is on pull requests (the way back).
# This part reads no name of the entry except ConfigError (validate_land_repos); `uses_spire_land` is pure and is the one reader of the key.
import re

LAND_REPOS_KEY = "land_repos"
LAND_REPOS_DEFAULT = ("janus-infra/*",)
LAND_REPOS_MAX = 200
_LAND_ENTRY = re.compile(r"[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.-]+|\*)")   # matched with fullmatch: a trailing newline is not a name


def uses_spire_land(config, repo):
    """True when `repo` ("owner/repo") lands by `spire land` under the configuration's `land_repos` (case-insensitive; "owner/*" matches every repository of
    the owner). An absent key means the default; an empty list means none. A value that is not a list of names matches nothing (the loader refuses it first)."""
    if not isinstance(repo, str) or not _LAND_ENTRY.fullmatch(repo) or repo.endswith("/*"):
        return False
    raw = (config or {}).get(LAND_REPOS_KEY) if isinstance(config, dict) else None
    entries = LAND_REPOS_DEFAULT if raw is None else raw
    if not isinstance(entries, (list, tuple)) or len(entries) > LAND_REPOS_MAX or not all(isinstance(e, str) and _LAND_ENTRY.fullmatch(e) for e in entries):
        entries = LAND_REPOS_DEFAULT   # the launch text's rule: a malformed value fails to the default, never to nobody (the loader refuses it first)
    if not isinstance(entries, (list, tuple)):
        return False
    owner, _, name = repo.lower().partition("/")
    for entry in entries:
        if not isinstance(entry, str) or not _LAND_ENTRY.fullmatch(entry):
            continue
        e_owner, _, e_name = entry.lower().partition("/")
        if e_owner == owner and e_name in ("*", name):
            return True
    return False


is_land_repo = uses_spire_land   # the same reader under a name the entry may carry (a hygiene check keeps the product's own name out of the entry)


def validate_land_repos(config, source):
    """`land_repos`: null or absent is the default; else a list of at most 200 "owner/repo" or "owner/*" strings (an empty list is valid: every repository on pull requests)."""
    raw = config.get(LAND_REPOS_KEY)
    if raw is None:
        config.pop(LAND_REPOS_KEY, None)
        return
    if not isinstance(raw, list) or len(raw) > LAND_REPOS_MAX or not all(isinstance(r, str) and _LAND_ENTRY.fullmatch(r) for r in raw):
        raise ConfigError("%s: 'land_repos' must be a list of at most %d \"owner/repo\" or \"owner/*\" names (an empty list puts every repository on pull requests), got %r" % (source, LAND_REPOS_MAX, raw))


# ---- the launch text (this branch): the one landing line a launch for a land repository carries
LAND_SETUP_LINE = ("- This repository lands with `spire land`: when done, commit, run `.spire/tool/bin/spire land` and report `landed <sha>`, "
                   "or the exit code and what stopped it; exit 22 = held for a person: stop and say so.\n")


def setup_with_land(preamble, config, repo):
    """The setup text of a launch: for a land repository the one landing line goes before `Task:`; for any other repository the text is returned unchanged."""
    marker = "\nTask:\n"
    if not uses_spire_land(config, repo) or marker not in preamble:
        return preamble
    head, _, tail = preamble.rpartition(marker)
    return head + LAND_SETUP_LINE + marker + tail
