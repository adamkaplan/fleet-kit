# ---- #2320: which repositories land with `spire land` instead of a pull request. ONE switch: the top-level config key `land_repos` (a list of 'owner/repo' or
# 'owner/*' globs; default ["janus-infra/*"]; an empty list = every repository on pull requests, the way back). Pure: no read, no call.

LAND_REPOS_DEFAULT = ("janus-infra/*",)
LAND_SETUP_LINE = ("- This repository lands with `spire land`: when done, commit, run `.spire/tool/bin/spire land` and report `landed <sha>`, "
                   "or the exit code and what stopped it; exit 22 = held for a person: stop and say so.\n")


def uses_spire_land(config, repo):
    """True when `repo` ('owner/repo') matches an entry of the config's `land_repos`. A missing key means the default (so does a malformed value: the switch fails to its default, it is never silently off); an empty list means nobody; a value that is not a list of text means the default."""
    patterns = config.get("land_repos", LAND_REPOS_DEFAULT) if isinstance(config, dict) else LAND_REPOS_DEFAULT
    if not isinstance(patterns, (list, tuple)) or not all(isinstance(p, str) for p in patterns):
        patterns = LAND_REPOS_DEFAULT
    if not isinstance(repo, str) or not repo:
        return False
    name = repo.lower()
    return any(name == p.lower() or (p.endswith("/*") and name.startswith(p[:-1].lower()) and len(name) > len(p) - 1) for p in patterns)


def setup_with_land(preamble, config, repo):
    """The setup text of a launch: for a land repository the one landing line goes before `Task:`; for any other repository the text is returned unchanged."""
    marker = "\nTask:\n"
    if not uses_spire_land(config, repo) or marker not in preamble:
        return preamble
    head, _, tail = preamble.rpartition(marker)
    return head + LAND_SETUP_LINE + marker + tail
