# ---- stage events (#1568): a staging stack's check run, told to the lane that owns the pull request

STAGE_LINE_PREFIX = "stage-event: "
STAGE_LINE_CAP = 400                      # the most characters of the machine line a message keeps (cut, with a mark)
STAGE_UNREADABLE = "stage event unreadable"
STAGE_LINK_RE = re.compile(r"(https?://[^\s\"'<>?#\\]*)[?#][^\s\"'<>\\]*", re.I)
STAGE_REDACTED = "?<signed value removed>"


def stage_redact(text):
    """The text with the query string and the fragment of every URL in it replaced by a mark: what the audit and any state file may hold of a sign-in link."""
    return STAGE_LINK_RE.sub(lambda m: m.group(1) + STAGE_REDACTED, str(text))


def stage_line(summary):
    """The message's machine part for a check's output summary: the one `stage-event: <JSON object>` line, parsed with the bounded parse and rebuilt
    compact (so no character of the original line is echoed), cut at STAGE_LINE_CAP; or the words `stage event unreadable` and none of the line's
    characters when there is no such line, it is not a JSON object, or it is too deep, too long or not JSON."""
    lines = [l for l in str(summary or "").splitlines() if l.startswith(STAGE_LINE_PREFIX)]
    if len(lines) != 1:
        return STAGE_UNREADABLE
    try:
        obj = load_module("record_json").parse_bounded(lines[0][len(STAGE_LINE_PREFIX):])
    except (ValueError, RecursionError):
        return STAGE_UNREADABLE
    if not isinstance(obj, dict):
        return STAGE_UNREADABLE
    try:
        text = _one_line(json.dumps(obj, separators=(",", ":"), ensure_ascii=True), 100000)
    except (ValueError, TypeError, RecursionError):
        return STAGE_UNREADABLE
    return STAGE_LINE_PREFIX + (text if len(text) <= STAGE_LINE_CAP else text[:STAGE_LINE_CAP] + "...")


def stage_listed(config, item):
    """True when the item is a check run whose name is on `github_stage_checks` AND whose sender is on `github_stage_bots` (case-insensitive logins)."""
    mark = getattr(item, "stage", None)
    if not mark:
        return False
    names, bots = config.get("github_stage_checks") or [], config.get("github_stage_bots") or []
    return mark[0] in names and str(mark[1] or "").lower() in {str(b).lower() for b in bots}


def _wh_check_run(repo, payload, now):
    """One item per completed check run on a pull request. The item holds the one-line message; `stage` (name, sender) and `head_branch` are
    read by the routing pass, which decides with the configuration whether it is listed (actionable whatever its conclusion) or heard at all."""
    if payload.get("action") != "completed":
        return []
    run = payload.get("check_run") or {}
    name, conclusion = run.get("name"), run.get("conclusion")
    oid, numbers = _id(run), _pr_numbers(run)
    if oid is None or not isinstance(name, str) or not name or not isinstance(conclusion, str) or not conclusion or not numbers:
        return []
    name, conclusion = _one_line(name, 60), _one_line(conclusion, 30)
    summary = "%s %s on PR %s" % (name, conclusion, ", ".join("#%d" % n for n in numbers))
    item = GitHubItem("github.check:cr-%d:%s" % (oid, _one_line(conclusion, 30)), "github.check", summary, repo, [],
                      [("pr", n) for n in numbers], None, now, numbers, conclusion.lower() in ACTIONABLE_CONCLUSIONS)
    sender = payload.get("sender")
    login = sender.get("login") if isinstance(sender, dict) else None
    out = run.get("output") if isinstance(run.get("output"), dict) else {}
    item.stage = (run.get("name"), login if isinstance(login, str) else "")
    item.stage_summary = out.get("summary") if isinstance(out.get("summary"), str) else ""   # in memory only: read for a listed check, never stored
    suite = run.get("check_suite") if isinstance(run.get("check_suite"), dict) else {}
    item.head_branch = suite.get("head_branch") if isinstance(suite.get("head_branch"), str) else None
    return [item]
