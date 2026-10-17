"""Testers from the launch record (#372, part of #130).

Who may record a `tester` verdict is the configured list (`plan_evaluators.tester`) UNION the agents whose launch record says they are testers for the PR's
repo: launched with `--role tester`, recording that repo, reporting to an orchestrator of that repo, and not retired. `reviewer`, `ci` and `owner` are
never derived from a launch record.

Pure: it reads plain values (the fleet metadata of the running agents, the retired list) and returns plain values. No file, no GitHub, no agent.
"""

ROLE = "tester"
ORCHESTRATOR = "orchestrator"


def _plain(value):
    return value if isinstance(value, str) else None


def launched(fleets, sessions, repo, repo_of, retired=None):
    """{name: record} for every agent whose launch record makes it a tester of `repo`.

    `fleets` is {name: metadata.fleet} of the running agents, `sessions` {name: session id}, `repo_of(fleet)` the repo a record names (or None), `retired`
    {name: {"session": id or None}}. A tester counts only when: its role is `tester`; its recorded repo is `repo`; its recorded boss is an agent whose
    own record says orchestrator and the same repo; the launch was run BY that boss (`launched_by`, which the launch records from the caller's pane; a
    launch from a plain shell or before this record existed does not count: use the configured list); and it is not retired (the name and its session, as retirement is recorded)."""
    found = {}
    for name, fleet in sorted((fleets or {}).items()):
        if not isinstance(fleet, dict) or fleet.get("role") != ROLE or repo_of(fleet) != repo:
            continue
        boss = _plain(fleet.get("reports_to"))
        boss_fleet = (fleets or {}).get(boss) if boss else None
        if not isinstance(boss_fleet, dict) or boss_fleet.get("role") != ORCHESTRATOR or repo_of(boss_fleet) != repo:
            continue
        if fleet.get("launched_by") != boss:   # the launch must have been RUN by that orchestrator: a coder cannot mint a tester by naming an orchestrator as its boss
            continue
        gone = (retired or {}).get(name)
        if isinstance(gone, dict) and gone.get("session") in (None, (sessions or {}).get(name)):
            continue
        found[name] = {"name": name, "role": ROLE, "repo": repo, "reports_to": boss, "issue": fleet.get("issue"), "session": (sessions or {}).get(name)}
    return found


def evaluators(configured, launched_testers):
    """The `plan_evaluators` map with the launched testers added to `tester` (the configured list is kept, in its order). Nothing else changes."""
    merged = dict(configured or {})
    names = list(merged.get("tester") or [])
    names += [name for name in sorted(launched_testers) if name not in names]
    if names:
        merged["tester"] = names
    return merged


def lane_refusal(tester, launched_testers, authors, sessions, fleets=None):
    """Why `tester` is not independent of the PR's work, or None. One place, so the rule is easy to change (cos's definition): the tester must not be a
    writer of the PR (it recorded a proof or sent its notice), must be a different agent from every coder launched on the tester's work item (same name or
    same session as one), and no writer may run in the tester's own session (a coder relaunched under another name)."""
    issue = ((fleets or {}).get(tester) or {}).get("issue")
    mine = (sessions or {}).get(tester)
    for name, fleet in sorted((fleets or {}).items()):
        coder_here = isinstance(fleet, dict) and fleet.get("role") == "coder" and issue is not None and fleet.get("issue") == issue
        if coder_here and (name == tester or (mine is not None and (sessions or {}).get(name) == mine)):
            return "%s is not a different agent from %s, a coder launched on work item #%s" % (tester, name, issue)
    if tester in (authors or ()):
        return "%s wrote this PR: a tester cannot record a verdict on its own work" % tester
    if mine is not None and any((sessions or {}).get(author) == mine for author in (authors or ())):
        return "%s runs in the session of a writer of this PR: it is not independent of it" % tester
    return None


def source_of(name, configured, launched_testers):
    """'config' when the name is on the configured list, else 'launch record' when it is a launched tester, else None."""
    if name in ((configured or {}).get("tester") or []):
        return "config"
    return "launch record" if name in (launched_testers or {}) else None


def status_lines(configured, fleets, sessions, repos, repo_of, retired=None):
    """The `status` lines: the resolved testers per repo with their source. `repos` is every repo to show (those of the running orchestrators)."""
    lines = []
    for name in (configured or {}).get("tester") or []:
        lines.append("      tester     %s: config (any repo)" % name)
    for repo in sorted(repos):
        for name in sorted(launched(fleets, sessions, repo, repo_of, retired)):
            if name not in ((configured or {}).get("tester") or []):
                lines.append("      tester     %s: launch record (%s)" % (name, repo))
    return lines
