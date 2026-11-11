# ---- ask #2320: the repositories that land by `spire land` (no pull request). One switch, the top-level config key `land_repos`.
# A list of "owner/repo" or "owner/*" globs; absent or null means LAND_REPOS_DEFAULT, which is EMPTY: no repository lands by `spire land` until the key names one, e.g. ["janus-infra/*"] (the owner's order: OFF until he says so); an empty list is the same.
# This part reads no name of the entry except ConfigError (validate_land_repos); `uses_spire_land` is pure and is the one reader of the key.
import re

LAND_REPOS_KEY = "land_repos"
LAND_REPOS_DEFAULT = ()
LAND_REPOS_MAX = 200
_LAND_ENTRY = re.compile(r"[A-Za-z0-9_.-]+/(?:[A-Za-z0-9_.-]+|\*)")   # matched with fullmatch: a trailing newline is not a name


def uses_spire_land(config, repo):
    """True when `repo` ("owner/repo") lands by `spire land` under the configuration's `land_repos` (case-insensitive; "owner/*" matches every repository of
    the owner). An absent or null key means the default, which is empty; an empty list means none. A value that is not a list of names matches nothing (the loader refuses it first)."""
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
    """`land_repos`: null or absent is the default (empty: no repository on the land path); else a list of at most 200 "owner/repo" or "owner/*" strings (an empty list is valid: every repository on pull requests)."""
    raw = config.get(LAND_REPOS_KEY)
    if raw is None:
        config.pop(LAND_REPOS_KEY, None)
        return
    if not isinstance(raw, list) or len(raw) > LAND_REPOS_MAX or not all(isinstance(r, str) and _LAND_ENTRY.fullmatch(r) for r in raw):
        raise ConfigError("%s: 'land_repos' must be a list of at most %d \"owner/repo\" or \"owner/*\" names (an empty list puts every repository on pull requests), got %r" % (source, LAND_REPOS_MAX, raw))


# ---- the launch text (this branch): the one landing line a launch for a land repository carries
LAND_STEP = ('run it DETACHED: `nohup sh -c \'.spire/tool/bin/spire land > /work/land.log 2>&1; r=$?; { echo; echo "exit $r"; } >> /work/land.log\' > /dev/null 2>&1 &`, then poll with short calls (`sleep 120; tail -n 20 /work/land.log`, each under 5 minutes) and report `fleet-switchboard report working "land step <where it is>"` about every 10 minutes; never one foreground `spire land` call (it is moved to a background shell and your turn ends unwoken). The run is over when the log\'s last line is `exit <code>`: report `landed <sha>` or the exit code and what stopped it; exit 22 = held for a person: stop and say so')
LAND_SETUP_LINE = ("- This repository lands with `spire land`: when done, commit, "
                   + LAND_STEP + ".\n")


def setup_with_land(preamble, config, repo):
    """The setup text of a launch: for a land repository the one landing line goes before `Task:`; for any other repository the text is returned unchanged."""
    marker = "\nTask:\n"
    if not uses_spire_land(config, repo) or marker not in preamble:
        return preamble
    head, _, tail = preamble.rpartition(marker)
    return head + LAND_SETUP_LINE + marker + tail


# ---- ask #2373: a landed commit as the subject of prove, verdict and gates. ASSUMPTION: the real trailer forms are not known; a trailer is `Key: <anything non-empty>` in the LAST paragraph of the message
# (the `git interpret-trailers --parse` form), the key case-insensitive. This is the one place to change when the real form is known.
LAND_PROOF_TRAILERS = ("Land-Checks", "Land-Council")


def land_trailers_missing(message):
    """The names of LAND_PROOF_TRAILERS the commit message lacks (an empty list: it has them all). Pure. The trailer block is the last paragraph, every line of which is a trailer or the continuation of one; prose there makes it no block."""
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", (message or "").strip().replace("\r\n", "\n")) if p.strip()]
    block = paragraphs[-1].split("\n") if len(paragraphs) > 1 else []   # the subject paragraph alone is never a trailer block
    parsed = [re.fullmatch(r"([A-Za-z0-9-]+)[ \t]*:[ \t]*(\S.*)?", line) for line in block]
    if any(m is None and not line[:1].isspace() for m, line in zip(parsed, block)):
        return list(LAND_PROOF_TRAILERS)
    found = {m.group(1).lower() for m in parsed if m and m.group(2)}   # an empty value is a trailer line but no proof
    return [name for name in LAND_PROOF_TRAILERS if name.lower() not in found]
