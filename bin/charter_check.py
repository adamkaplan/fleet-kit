"""One check of the endpoints of a command that moves or copies between charters (asks #347): used by `move` (bin/lanes.py) and `orders copy`
(bin/orders_copy.py), so there is one definition of what a charter is and which repository may be touched.

A charter is an OPEN ISSUE (never a pull request, whatever its body holds) that carries the orchestrator label AND the yaml `orchestrator:`
block, in a repository the switchboard watches and does not exclude. Every refusal is a `Refused` with a clear sentence, raised BEFORE any
write (and before any order is read for copying); a read that fails is a refusal, never a pass.

The caller hands in `sb`: a namespace with `engine` and the switchboard's own helpers named in `NEEDS` (this module imports nothing from it).
"""

NEEDS = ("IntentError", "_CHARTER_BLOCK", "_one_line", "orchestrator_label", "load_module")


class Refused(Exception):
    """An endpoint that cannot be used, and why. Nothing was written."""


def label_names(raw):
    return [l.get("name") if isinstance(l, dict) else l for l in raw.get("labels") or [] if isinstance(l, (dict, str))]


def has_charter_block(sb, raw):
    body = raw.get("body") if isinstance(raw.get("body"), str) else ""
    return bool(sb._CHARTER_BLOCK.search(body))


def is_charter(sb, raw):
    """A charter carries the orchestrator label AND the yaml block."""
    return sb.orchestrator_label(sb.engine.config) in label_names(raw) and has_charter_block(sb, raw)


def read_issue(sb, repo, number):
    """The raw issue, read now (never from the cache). Refused when it cannot be read."""
    try:
        raw = sb.engine.intents._api("repos/%s/issues/%d" % (repo, number), repo)
    except sb.IntentError as err:
        raise Refused("cannot read %s#%d: %s" % (repo, number, sb._one_line(str(err), 160))) from None
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
        raise Refused("%s#%d came back without an id" % (repo, number))
    return raw


def require_open_issue(raw, what, repo, number):
    """Refused unless `raw` is an OPEN issue that is not a pull request (`what`: "the ask", "the target", "the source")."""
    if raw.get("pull_request"):
        raise Refused("%s %s#%d is a pull request, not an issue" % (what, repo, number))
    if raw.get("state") != "open":
        raise Refused("%s %s#%d is %s: only an open issue counts%s" % (
            what, repo, number, raw.get("state") or "in a state that cannot be read",
            " (a closed charter has no orchestrator)" if what in ("the target", "the source") else ""))


def require_charter(sb, repo, number, what):
    """Read `repo#number` and refuse unless it is an open charter issue. Returns the raw issue."""
    raw = read_issue(sb, repo, number)
    require_open_issue(raw, what, repo, number)
    if not is_charter(sb, raw):
        raise Refused("%s %s#%d is not a charter (it needs the %s label and the yaml orchestrator block)"
                      % (what, repo, number, sb.orchestrator_label(sb.engine.config)))
    return raw


def check_repo(sb, repo, watched):
    """Refused unless `repo` is a watched repository that is not excluded (an empty or missing `watched` refuses: fail closed)."""
    if sb.load_module("repo_exclusion").is_excluded(sb.engine.config, repo):
        raise Refused("repository %s is excluded from the fleet (excluded_repos)" % repo)
    if repo not in (watched or []):
        raise Refused("repository %s is not a watched repository" % repo)
