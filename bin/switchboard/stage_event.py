# ---- stage events (ask #1575): a staging bot's comment whose FIRST LINE is a marker

STAGE_MARKER = "<!-- stage:event "   # a first line that starts so is a stage line; the version is checked in the parse
STAGE_END = " -->"
STAGE_LINE_MAX = 400
STAGE_STATUSES = ("requested", "up", "updated", "building", "rebuilding", "suspended", "resumed", "expiring", "renewed", "expired", "failed")
STAGE_UNREADABLE = "stage event unreadable"
STAGE_HEAD_RE = re.compile(r"[0-9a-fA-F]{40}")
STAGE_UNTIL_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]{1,6})?)?(?:Z|[+-][0-9]{2}:[0-9]{2})?")
STAGE_LISTS = {"repos": frozenset(), "bots": frozenset()}   # set from the config by the GitHub source; empty: no comment is a stage event


def stage_lists_set(config):
    """Remember the two configured lists (lower case). Empty by default."""
    STAGE_LISTS["repos"] = frozenset(str(r).lower() for r in (config.get("github_stage_repos") or []))
    STAGE_LISTS["bots"] = frozenset(str(b).lower() for b in (config.get("github_stage_bots") or []))


def stage_check_config(config, source):
    """Both keys are lists of strings (empty by default); anything else is a ConfigError."""
    for key, rule in (("github_stage_repos", GITHUB_REPO_RE), ("github_stage_bots", GITHUB_LOGIN_RE)):
        value = config[key]
        if not isinstance(value, list) or not all(isinstance(v, str) and rule.match(v) for v in value):
            raise ConfigError("%s: '%s' must be a list of strings (%s), got %r" % (source, key, "owner/repo names" if "repos" in key else "GitHub logins", value))
        config[key] = list(value)


def _stage_url(value):
    """A url the parser cannot read (urlsplit, port or hostname raise) is no url: the line is unreadable, whatever the exception."""
    try:
        if not isinstance(value, str) or len(value) > 200 or not re.fullmatch(r"[\x21-\x7e]+", value):
            return None
        parts = urllib.parse.urlsplit(value)
        parts.port
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or "?" in value or "#" in value or "@" in parts.netloc:
            return None
        return value
    except Exception:
        return None


def stage_parse(first):
    """The validated fields of a marker line, or None (unreadable). `first` is the first line only, already cut at STAGE_LINE_MAX + 1."""
    if len(first) > STAGE_LINE_MAX or not (first.startswith(STAGE_MARKER) and first.endswith(STAGE_END)) or len(first) < len(STAGE_MARKER) + len(STAGE_END):
        return None
    rest = first[len(STAGE_MARKER):len(first) - len(STAGE_END)]
    if not rest.startswith("v1 "):
        return None
    try:
        data = load_module("record_json").parse_bounded(rest[3:])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    status, head = data.get("status"), data.get("head")
    if status not in STAGE_STATUSES or not isinstance(head, str) or not STAGE_HEAD_RE.fullmatch(head):
        return None
    fields = {"status": status, "head": head.lower(), "url": None, "until": None}
    if "url" in data:
        fields["url"] = _stage_url(data["url"])
        if fields["url"] is None:
            return None
    until = data.get("until")
    if isinstance(until, str) and STAGE_UNTIL_RE.fullmatch(until):
        try:
            datetime.fromisoformat(until.replace("Z", "+00:00"))
            fields["until"] = until
        except ValueError:
            pass
    return fields


def stage_item_parts(repo, comment, number, cid, kind=None):
    """(key, summary) of a stage comment, or None when it is not one (the old path). Reads ONLY the first line of the body, cut at 401 characters first."""
    if not STAGE_LISTS["repos"] or not STAGE_LISTS["bots"]:
        return None
    user = comment.get("user") if isinstance(comment, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    body = comment.get("body")
    if not (isinstance(login, str) and isinstance(body, str) and login.lower() in STAGE_LISTS["bots"] and str(repo).lower() in STAGE_LISTS["repos"]):
        return None
    first = body.split("\n", 1)[0][:STAGE_LINE_MAX + 1]
    if not first.startswith(STAGE_MARKER):
        return None
    fields = stage_parse(first)
    if fields is None:
        return "github.stage:%d:unreadable:%s" % (cid, hashlib.sha256(first.encode("utf-8", "replace")).hexdigest()[:12]), STAGE_UNREADABLE
    key = "github.stage:%d:%s:%s:%s" % (cid, fields["status"], fields["head"], fields["until"] or "")
    what = {"pr": "PR #", "issue": "issue #"}.get(kind, "#")   # #1591: only this word depends on what the number is (None: unknown, neither word)
    text = "stage %s on %s%d, head %s" % (fields["status"], what, number, fields["head"][:7])
    if fields["url"]:
        text += ", " + fields["url"]
    if fields["until"]:
        text += ", until " + fields["until"]
    return key, text
