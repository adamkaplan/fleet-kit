"""The call-site table of the daemon's GitHub reads (#1515) and the scan that derives the list of sites from the source.

A site is the function (qualified by its classes) that holds a call of `accounts.read`, of `gh_api`, or a use of `config["gh"]` (the way a `gh` child is
started). `SITES` says, for each, what it reads, what drives it (a pass or interval constant), its runs per hour and its API requests per run;
`derived_sites(paths)` finds the list from the source, and `missing(paths)` names a site that is in no row (and a row with no site). `hourly(...)`
computes the total per hour for a stated fleet (repos watched, open pulls per repo).

Counting rule: one request per HTTP request made (each page of a paginated read, each request of a batched read), by account and endpoint class; a
conditional read answered 304 costs nothing against the budget but is still shown (as `not_modified`). Loaded by path; outside the clear closure."""
import ast

POLL = 60.0   # github_poll_seconds default: a repo without a forwarder is read this often (the catch-up pass)

# key (file, qualified function) -> (reads, driven by, runs per hour for the stated fleet f, requests per run for f)
# f = {"repos": watched repos without a forwarder, "pulls": open pulls per repo, "pages": pages of a list read}
SITES = {
    ("fleet-switchboard", "GitHubHub.gh_api"): ("the one hub read: every catch-up, sweep and triage read of a repo goes through it", "github_poll_seconds (60)",
        lambda f: 3600 / POLL * f["repos"], lambda f: 3 * f["pages"] + 2 * f["pulls"] * 1 + f["pages"]),   # comments, issues, pulls pages + reviews and check suites per pull + runs
    ("fleet-switchboard", "GitHubHub.read_pages"): ("a bounded paged read (the sweep's pull and issue lists): one request per page", "the sweep pass (SWEEP_PASS_SECONDS 8 s budget per pass)", lambda f: 0, lambda f: f["pages"]),
    ("fleet-switchboard", "GitHubHub._read_one"): ("one issue, pull or comment read for routing", "GITHUB_ROUTE_SECONDS (5 s of lookups per pass)", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "GitHubHub.open_issues_count"): ("the repo read for its open issue count", "per triage pass", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "GitHubHub.plan_kind"): ("one issue read to classify a plan", "per new plan item", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "GitHubHub.catch_up_items.present_pull"): ("one pull read when a pull is presented", "per changed pull", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "GitHubHub._start"): ("`gh webhook forward` child per repo with a forwarder (a stream, not a poll)", "once per start", lambda f: 0, lambda f: 0),
    ("fleet-switchboard", "GitHubHub.argv_for"): ("builds the `gh webhook forward` argv (no request itself)", "once per start", lambda f: 0, lambda f: 0),
    ("fleet-switchboard", "GhAccounts.read"): ("the counting layer itself: every named-repo read ends here", "every read", lambda f: 0, lambda f: 0),
    ("fleet-switchboard", "GhAccounts.read_usage"): ("`gh api rate_limit` per account (not counted against the budget)", "each status / 5-minute sample", lambda f: 12, lambda f: 0),
    ("fleet-switchboard", "GhAccounts.token_for"): ("`gh auth token` (a local process, no API request)", "per token expiry", lambda f: 0, lambda f: 0),
    ("fleet-switchboard", "IntentReader._api"): ("the intent and chain reads: one issue, its comments", "the intent / chain pass", lambda f: 0, lambda f: 2),
    ("fleet-switchboard", "IntentReader._api_raw_text"): ("a raw file read of the intent repo", "per intent read", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "_RedsReader.json"): ("the red-CI read of a pull request's runs", "the red pass", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "_RedsReader.pull_state"): ("one GraphQL pull state read (the cloud watch and alerts)", "per watched pull per alert pass", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "_RedsReader.zip"): ("a run log archive read", "per red run", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "read_open_overlap"): ("`gh pr list` for the overlap check", "per pull review", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "read_pr_json"): ("`gh pr view` of one pull request", "per merge-gate / clear read", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "read_ref_state"): ("`gh issue view` for one item's state", "per ref check", lambda f: 0, lambda f: 1),
    ("fleet-switchboard", "verify_account"): ("`gh api user` to check a login", "per account check (start, config change)", lambda f: 0, lambda f: 1),
}
ON_DEMAND = ("a command a person or an agent runs (`plan`, `review`, `merge-gate`, `check-*`): not driven by a pass",)


def _is_gh_use(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if node.func.attr == "gh_api":
            return True
        if node.func.attr == "read" and ((isinstance(owner, ast.Attribute) and owner.attr == "accounts") or (isinstance(owner, ast.Name) and owner.id == "accounts")):
            return True
    return isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and node.slice.value == "gh"


def derived_sites(paths):
    """{(file name, qualified function)} of every call to `accounts.read`, `gh_api` or use of `config["gh"]` found in `paths` (source parsed, never run)."""
    found = set()
    for path in paths:
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        tree = ast.parse(open(path, encoding="utf-8").read())

        def walk(node, stack):
            for child in ast.iter_child_nodes(node):
                inner = stack + [child.name] if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else stack
                if stack and _is_gh_use(child):
                    found.add((name, ".".join(stack)))
                walk(child, inner)
        walk(tree, [])
    return found


# functions that start a gh child for a one-shot command, or validate config (on demand, never in a pass): grouped under ON_DEMAND
ON_DEMAND_SITES = {
    ("fleet-switchboard", f) for f in (
        "_changed_plan", "_gh_write", "_review_api", "_validate_github", "carry_get.get", "check_approval", "check_charter_workspace", "cmd_plan_template",
        "cmd_review_checkout", "cmd_review_measure", "gates_suites_finding", "intent_reader_for", "load_plan", "merge_gate_gh.gh", "plan_peers", "review_laws_text")
} | {("plan_review.py", "_ask_facts"), ("plan_review.py", "_plan_ask_problem")}


def missing(paths, extra=()):
    """(sites in the source that are in no row, rows with no site in the source), sorted."""
    have = derived_sites(paths) | set(extra)
    rows = set(SITES) | ON_DEMAND_SITES
    return sorted(have - rows), sorted(rows - have)


def hourly(fleet):
    """[(key, runs per hour, requests per run, requests per hour)] and the total, for `fleet` = {"repos", "pulls", "pages"}."""
    out = [(k, v[2](fleet), v[3](fleet)) for k, v in sorted(SITES.items())]
    rows = [(k, runs, per, runs * per) for k, runs, per in out]
    return rows, sum(r[3] for r in rows)
