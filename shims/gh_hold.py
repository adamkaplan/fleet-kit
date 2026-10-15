"""gh_hold: the hold label and the write audit of the gh shim (#376).

Two jobs, both called from `shims/gh`, which loads this file by path from its own directory (never `import`: the shim has no
package and runs through a symlink).

1. HOLD LABEL. The switchboard config may list, per repository, a label that holds a pull request for its project:

       "hold_labels": {"acme-org/widget-api": {"label": "acme:hold-proof", "message": "...", "allowed_removers": ["coordinator"]}}

   On a pull request that carries the label, the shim refuses merge (auto-merge enable and disable and `--admin` too), close, an approving
   review and a base change, and refuses to remove the label unless the caller is named in `allowed_removers`. It only ADDS refusals: a
   command that ran before still runs unless it is one of these on a labelled pull request. A repository with no entry costs no GitHub call.
   FAIL CLOSED: a label read that fails, times out or is unreadable, a config that cannot be read, a command shape the shim cannot
   follow, refuse the write (exit 1, a retry message) rather than let it through. Reads are never touched.

2. WRITE AUDIT. Every gh write the shim passes on, and every one it refuses, leaves one JSON line (time, verb, repository, number, caller,
   outcome) in `gh-writes.jsonl` in the directory beside the switchboard state directory named like it with `-audit` added. No argument, body, token or query value is ever copied into it, and a
   failure to write it never changes what the command does.

Only the standard library. Names of repositories, labels and agents are configuration: nothing here knows one.
"""
import json
import os
import re
import subprocess
import time
import unicodedata
import urllib.parse

HOLD_SECONDS = 5.0
AUDIT_FILE = "gh-writes.jsonl"
AUDIT_LIMIT = 1048576
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
PART_RE = re.compile(r"^[a-z0-9_.-]+$")
URL_RE = re.compile(r"^https?://[^/\s]+/([^/\s]+/[^/\s]+)/(pull|issues)/([0-9]+)(?:[/?#].*)?$", re.I)
MAX_FILE = 262144
# the top-level gh commands that are not an alias: an alias cannot shadow one
KNOWN_COMMANDS = {"pr", "issue", "api", "repo", "label", "auth", "config", "alias", "browse", "codespace", "completion", "extension", "ext",
                  "gist", "gpg-key", "help", "release", "run", "search", "secret", "ssh-key", "status", "variable", "workflow", "cache",
                  "org", "attestation", "project", "ruleset", "agent-task", "preview", "licenses", "version", "copilot", "skill"}
GRAPHQL_GUARDED = re.compile(r"\b(mutation)\b", re.I)   # every mutation: a node id cannot be tied to a repository, so none can be checked

RETRY = ("gh: hold-label check failed, retry (the fleet shim could not read whether this pull request carries a hold label, so it refuses "
         "the write rather than let it through)")
BAD_CONFIG = ("gh: the hold-label configuration could not be read: retry, or ask whoever owns the switchboard config (the fleet shim refuses a "
              "guarded write rather than let it through)")
UNFOLLOWED = ("gh: this command could not be checked against a hold label (the shim could not tell which pull request it names): spell it as "
              "`gh pr <verb> <number> -R <owner/repo>` and retry")


def clean(text):
    """One printable line: control characters (terminal escapes, newlines) removed."""
    return "".join(ch for ch in str(text) if ch.isprintable()).strip()


def fold(text):
    """A label or name as compared: NFKC, case-folded, whitespace collapsed. A lookalike that folds to the label counts as the label (the refusal side)."""
    return " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split())


def norm_repo(text):
    """`owner/name` lower-cased from `owner/name`, `owner/name.git`, `HOST/owner/name`, a URL prefix, with spaces or a trailing slash; None if not that."""
    try:
        t = unicodedata.normalize("NFKC", str(text)).strip().casefold()
        t = re.sub(r"^https?://", "", t).strip("/")
        parts = t.split("/")
        if len(parts) == 3:
            parts = parts[1:]                      # HOST/OWNER/REPO: the host is not part of the name
        if len(parts) != 2:
            return None
        if parts[1].endswith(".git"):
            parts[1] = parts[1][:-4]
        if not all(PART_RE.match(p) for p in parts):
            return None
        return "/".join(parts)
    except Exception:
        return None


# ---- the config

def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "fleet-switchboard", "config.json")


def parse_entry(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("label"), str) or not fold(entry["label"]):
        return {"bad": True}
    message = entry.get("message")
    allowed = entry.get("allowed_removers", [])
    if not isinstance(allowed, list) or not all(isinstance(a, str) for a in allowed):
        return {"bad": True}
    return {"label": fold(entry["label"]), "message": clean(message)[:600] if isinstance(message, str) else "",
            "allowed": {a.strip().lower() for a in allowed}}


def load_holds():
    """("none", {}) when no hold is configured (no file, no key, empty), ("bad", {}) when the file or the key cannot be read, else ("ok", {repo: entry})."""
    try:
        with open(config_path()) as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return "none", {}
    except (OSError, ValueError):
        return "bad", {}
    if not isinstance(raw, dict):
        return "bad", {}
    section = raw.get("hold_labels")
    if section is None or section == {}:
        return "none", {}
    if not isinstance(section, dict):
        return "bad", {}
    holds = {}
    for key, entry in section.items():
        repo = norm_repo(key)
        if repo is None:
            return "bad", {}
        holds[repo] = {"bad": True} if repo in holds else parse_entry(entry)
    return "ok", holds


def validate_config(section):
    """For the switchboard's config check: raises ValueError naming the problem; returns the section."""
    if not isinstance(section, dict):
        raise ValueError("'hold_labels' must be an object of \"owner/repo\" -> {label, message, allowed_removers}")
    seen = set()
    for key, entry in section.items():
        repo = norm_repo(key)
        if repo is None:
            raise ValueError("'hold_labels' key %r is not an owner/repo" % (key,))
        if repo in seen:
            raise ValueError("'hold_labels' names %s twice" % repo)
        seen.add(repo)
        if not isinstance(entry, dict) or set(entry) - {"label", "message", "allowed_removers"}:
            raise ValueError("'hold_labels' entry for %s must be an object with only label, message, allowed_removers" % repo)
        label = entry.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 100 or not label.isprintable():
            raise ValueError("'hold_labels' entry for %s needs a printable 'label' of at most 100 characters" % repo)
        message = entry.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 600 or not message.isprintable():
            raise ValueError("'hold_labels' entry for %s needs a one-line printable 'message' of at most 600 characters" % repo)
        allowed = entry.get("allowed_removers", [])
        if not isinstance(allowed, list) or any(not isinstance(a, str) or not NAME_RE.match(a) for a in allowed):
            raise ValueError("'hold_labels' entry for %s: 'allowed_removers' must be a list of agent names" % repo)
    return section


# ---- reading the command line

class Parsed:
    def __init__(self):
        self.positional, self.flags, self.repos, self.ambiguous, self.help = [], [], [], False, False


PR_MERGE = {"bool": {"d": "--delete-branch", "m": "--merge", "r": "--rebase", "s": "--squash"},
            "bool_long": {"--delete-branch", "--admin", "--auto", "--disable-auto", "--merge", "--rebase", "--squash"},
            "val": {"R": "--repo", "b": "--body", "F": "--body-file", "A": "--author-email", "t": "--subject"},
            "val_long": {"--repo", "--body", "--body-file", "--author-email", "--subject", "--match-head-commit"}}
PR_CLOSE = {"bool": {"d": "--delete-branch"}, "bool_long": {"--delete-branch"}, "val": {"R": "--repo", "c": "--comment"},
            "val_long": {"--repo", "--comment"}}
PR_REVIEW = {"bool": {"a": "--approve", "r": "--request-changes", "c": "--comment"}, "bool_long": {"--approve", "--request-changes", "--comment"},
             "val": {"R": "--repo", "b": "--body", "F": "--body-file"}, "val_long": {"--repo", "--body", "--body-file"}}
EDIT_VALUES = {"--repo", "--title", "--body", "--body-file", "--base", "--milestone", "--add-assignee", "--remove-assignee", "--add-label",
               "--remove-label", "--add-project", "--remove-project", "--add-reviewer", "--remove-reviewer"}
PR_EDIT = {"bool": {}, "bool_long": {"--remove-milestone"},
           "val": {"R": "--repo", "t": "--title", "b": "--body", "F": "--body-file", "B": "--base", "m": "--milestone", "a": "--add-assignee",
                   "l": "--add-label", "p": "--add-project", "r": "--add-reviewer"}, "val_long": EDIT_VALUES}
ISSUE_CLOSE = {"bool": {}, "bool_long": set(), "val": {"R": "--repo", "c": "--comment", "r": "--reason"}, "val_long": {"--repo", "--comment", "--reason"}}
ISSUE_EDIT = {"bool": {}, "bool_long": {"--remove-milestone"},
              "val": {"R": "--repo", "t": "--title", "b": "--body", "F": "--body-file", "m": "--milestone", "a": "--add-assignee", "l": "--add-label",
                      "p": "--add-project"}, "val_long": EDIT_VALUES - {"--base"}}
LABEL_DELETE = {"bool": {"y": "--yes"}, "bool_long": {"--yes"}, "val": {"R": "--repo"}, "val_long": {"--repo"}}
LABEL_EDIT = {"bool": {}, "bool_long": set(), "val": {"R": "--repo", "n": "--name", "c": "--color", "d": "--description"},
              "val_long": {"--repo", "--name", "--color", "--description"}}
SPECS = {("pr", "merge"): PR_MERGE, ("pr", "close"): PR_CLOSE, ("pr", "review"): PR_REVIEW, ("pr", "edit"): PR_EDIT,
         ("issue", "close"): ISSUE_CLOSE, ("issue", "edit"): ISSUE_EDIT, ("label", "delete"): LABEL_DELETE, ("label", "edit"): LABEL_EDIT}


def parse_flags(rest, spec):
    """Read the arguments of one verb the way gh (cobra) does: `--flag value`, `--flag=value`, `-Rvalue`, clustered short booleans, positionals anywhere
    (also after flags), `--` ends the flags. A flag this does not know that may take a value makes the result ambiguous."""
    out = Parsed()
    i, ended = 0, False
    while i < len(rest):
        a = rest[i]
        i += 1
        if ended or a == "-" or not a.startswith("-"):
            out.positional.append(a)
            continue
        if a == "--":
            ended = True
            continue
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in ("--help",):
                out.help = True
            elif name in spec["val_long"]:
                if not eq:
                    if i >= len(rest):
                        out.ambiguous = True
                        continue
                    value, i = rest[i], i + 1
                out.flags.append((name, value))
                if name == "--repo":
                    out.repos.append(value)
            elif name in spec["bool_long"]:
                out.flags.append((name, value if eq else None))
            elif not eq:
                out.ambiguous = True           # an unknown flag may swallow the next word
            else:
                out.flags.append((name, value))
            continue
        letters, j = a[1:], 0
        while j < len(letters):
            c = letters[j]
            if c == "h":
                out.help = True
                j += 1
            elif c in spec["bool"]:
                out.flags.append((spec["bool"][c], None))
                j += 1
            elif c in spec["val"]:
                rest_of = letters[j + 1:]
                if rest_of:
                    value = rest_of[1:] if rest_of.startswith("=") else rest_of
                elif i < len(rest):
                    value, i = rest[i], i + 1
                else:
                    out.ambiguous = True
                    break
                out.flags.append((spec["val"][c], value))
                if spec["val"][c] == "--repo":
                    out.repos.append(value)
                break
            else:
                out.ambiguous = True
                break
    return out


def flag_values(parsed, name):
    return [v for n, v in parsed.flags if n == name]


def truthy(value):
    return value is None or str(value).strip().lower() not in ("false", "0", "f", "no")


def command_words(args):
    words, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("-R", "--repo"):
            skip = True
        elif not a.startswith("-"):
            words.append(a)
    return words


def split_command(args):
    """(group, verb, rest, repos before the verb) with a flag before the verb (`gh pr -R o/r merge 5`) kept; None with fewer than two words."""
    words, rest, repos, i = [], [], [], 0
    while i < len(args):
        a = args[i]
        i += 1
        if len(words) >= 2:
            rest.append(a)
        elif a in ("-R", "--repo"):
            if i < len(args):
                repos.append(args[i])
                i += 1
        elif a.startswith("--repo="):
            repos.append(a.split("=", 1)[1])
        elif a.startswith("-R") and not a.startswith("--") and len(a) > 2:
            repos.append(a[2:].lstrip("="))
        elif a.startswith("-"):
            rest.append(a)
        else:
            words.append(a)
    return (words[0], words[1], rest, repos) if len(words) == 2 else None


# ---- reading GitHub

class Ctx:
    """What the shim gives the check: the real gh and its environment, how to name the caller, whether the command is a write."""

    def __init__(self, gh, env, name_of, write):
        self.gh, self.env, self.name_of, self.write = gh, env, name_of, write
        try:
            seconds = float(os.environ.get("FLEET_SHIM_HOLD_SECONDS", HOLD_SECONDS))
        except ValueError:
            seconds = HOLD_SECONDS
        self.deadline = time.monotonic() + min(10.0, max(0.2, seconds))

    def run(self, argv):
        """stdout of the real gh, or None on any failure or when the one deadline of this check is spent."""
        left = self.deadline - time.monotonic()
        if left <= 0 or not self.gh:
            return None
        try:
            env = dict(self.env if self.env is not None else os.environ)
            env["GH_PROMPT_DISABLED"] = "1"
            env["GH_PAGER"] = "cat"
            done = subprocess.run([self.gh] + argv, capture_output=True, text=True, timeout=left, stdin=subprocess.DEVNULL, env=env)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None


class Result:
    def __init__(self, refusal=None, repo=None, number=None):
        self.refusal, self.repo, self.number = refusal, repo, number


def read_json(ctx, argv):
    raw = ctx.run(argv)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) else None


def label_names(data):
    labels = data.get("labels")
    if not isinstance(labels, list):
        return None
    names = []
    for item in labels:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None
        names.append(fold(item["name"]))
    return names


def hold_refusal(entry, what):
    return entry["message"] or ("gh: %s refused: this pull request carries its repository's hold label" % what)


def removal_refusal(entry, repo, ctx):
    """None when the caller is named as allowed to remove the hold label, else the refusal."""
    name = None
    try:
        name = ctx.name_of()
    except Exception:
        name = None
    if isinstance(name, str) and name.strip().lower() in entry["allowed"]:
        return None
    return "gh: removing the hold label on %s is allowed only to the agents the configuration names" % clean(repo)


# ---- gh pr / issue / label

def check_pr_issue(args, ctx, holds):
    sc = split_command(args)
    if sc is None or (sc[0], sc[1]) not in SPECS:
        return Result()
    group, verb, rest, pre = sc
    parsed = parse_flags(rest, SPECS[(group, verb)])
    if parsed.help:
        return Result()
    parsed.repos = pre + parsed.repos
    actions = []                                   # (kind, detail)
    if (group, verb) == ("pr", "merge"):
        actions.append(("merge", "merge"))
    elif (group, verb) == ("pr", "close"):
        actions.append(("guard", "close"))
    elif (group, verb) == ("issue", "close"):
        actions.append(("guard", "close"))
    elif (group, verb) == ("pr", "review"):
        if any(truthy(v) for v in flag_values(parsed, "--approve")):
            actions.append(("guard", "approving review"))
    elif (group, verb) == ("pr", "edit") and flag_values(parsed, "--base"):
        actions.append(("guard", "branch edit"))
    removed = []
    for value in flag_values(parsed, "--remove-label"):
        removed += [fold(x) for x in str(value).split(",")]
    if removed:
        actions.append(("remove", removed))
    if group == "label" and parsed.positional and (verb == "delete" or flag_values(parsed, "--name")):
        actions.append(("remove", [fold(parsed.positional[0])]))
    if removed or group == "label":
        every = {e.get("label") for e in holds.values()}
        if any(e.get("bad") for e in holds.values()):
            every.add(None)
        actions = [(k, d) for k, d in actions if k != "remove" or any(n in every for n in d) or None in every]
    if not actions:
        return Result()
    pos = list(parsed.positional)
    urls = [URL_RE.match(p) for p in pos]
    known = [norm_repo(r) for r in parsed.repos] + [norm_repo(m.group(1)) for m in urls if m]
    env_repo = os.environ.get("GH_REPO")
    if env_repo:
        known.append(norm_repo(env_repo))
    number = None
    for m in urls:
        if m:
            number = int(m.group(3))
    if number is None and len(pos) == 1 and pos[0].isdigit():
        number = int(pos[0])
    if group != "label" and known and all(k is not None for k in known) and not any(k in holds for k in known):
        return Result(None, known[-1], number)    # conclusively another repository: no read, nothing refused
    if group == "label":
        # `gh label delete|edit NAME`: the repository's own hold label name only
        distinct = {k for k in known if k}
        if not distinct or any(k is None for k in known):
            distinct = set(holds)
        if not (distinct & set(holds)):
            return Result(None, known[-1] if known else None, None)
        for repo in sorted(distinct & set(holds)):
            entry = holds[repo]
            if entry.get("bad"):
                return Result(BAD_CONFIG)
            if any(entry["label"] == name for kind, names in actions if kind == "remove" for name in names):
                why = removal_refusal(entry, repo, ctx)
                if why:
                    return Result(why, repo, None)
        return Result(None, known[-1] if known else None, None)
    distinct = {k for k in known if k}
    if removed and len(distinct) == 1 and len(known) >= 1 and all(k is not None for k in known) and next(iter(distinct)) in holds:
        entry = holds[next(iter(distinct))]
        if entry.get("bad"):
            return Result(BAD_CONFIG)
        if entry["label"] in removed:
            why = removal_refusal(entry, next(iter(distinct)), ctx)
            if why:
                return Result(why, next(iter(distinct)), number)
        if not any(k != "remove" for k, _ in actions):
            return Result(None, next(iter(distinct)), number)
    # the repository is not known to be elsewhere: the real gh says which pull request this is, and what it carries (one call)
    if parsed.ambiguous or len(pos) > 1 or (pos and pos[0].startswith("-")):
        return Result(UNFOLLOWED)
    view = [group, "view"] + pos[:1] + (["-R", parsed.repos[-1]] if parsed.repos else []) + ["--json", "url,number,labels"]
    data = read_json(ctx, view)
    if data is None:
        return Result(RETRY)
    url = URL_RE.match(str(data.get("url") or ""))
    names = label_names(data)
    if not url or names is None or not isinstance(data.get("number"), int):
        return Result(RETRY)
    repo, kind, number = norm_repo(url.group(1)), url.group(2).lower(), int(url.group(3))
    if repo is None:
        return Result(RETRY)
    entry = holds.get(repo)
    if entry is None:
        return Result(None, repo, number)
    if entry.get("bad"):
        return Result(BAD_CONFIG, repo, number)
    for kind_of, detail in actions:
        if kind_of == "remove":
            if entry["label"] in detail:
                why = removal_refusal(entry, repo, ctx)
                if why:
                    return Result(why, repo, number)
        elif kind == "pull" and entry["label"] in names:
            return Result(hold_refusal(entry, detail), repo, number)
    return Result(None, repo, number)


# ---- gh api

def clean_endpoint(text):
    """The path segments of an `gh api` endpoint: scheme and host dropped, query and fragment dropped, backslashes and doubled slashes folded, each
    segment percent-decoded ONCE and dot segments resolved. Returns the list of segments (original case)."""
    t = str(text).strip()
    t = re.sub(r"^https?://[^/]+/", "", t, flags=re.I)
    t = re.split(r"[?#]", t, 1)[0].replace("\\", "/")
    segs = []
    for seg in t.split("/"):
        seg = urllib.parse.unquote(seg)
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    if segs[:2] == ["api", "v3"]:
        segs = segs[2:]
    return segs


API_VALUE_FLAGS = {"-H", "--header", "-q", "--jq", "-t", "--template", "--cache", "-p", "--preview", "--hostname"}
API_FIELD_FLAGS = {"-f": "raw", "--raw-field": "raw", "-F": "field", "--field": "field"}


def parse_api(args):
    """(endpoints, method, fields, input_unknown): every positional is an endpoint candidate; the LAST method wins; fields are (key, value or None when
    the value is not known: `@file`, a typed placeholder, no `=`); `--input` makes the body unknown."""
    endpoints, method, fields, inp, i, skipped_api = [], None, [], False, 0, False
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            endpoints += args[i:]
            break
        if not a.startswith("-") or a == "-":
            if not skipped_api and a == "api":
                skipped_api = True
            else:
                endpoints.append(a)
            continue
        name, eq, inline = a.partition("=")
        if a.startswith("--"):
            if name in ("--method",):
                method, i = (inline, i) if eq else ((args[i] if i < len(args) else ""), i + 1)
            elif name == "--input":
                inp = True
                i += 0 if eq else 1
            elif name in API_FIELD_FLAGS:
                raw, i = (inline, i) if eq else ((args[i] if i < len(args) else ""), i + 1)
                fields.append(split_field(raw, API_FIELD_FLAGS[name]))
            elif name in API_VALUE_FLAGS and not eq:
                i += 1
            continue
        letters, j = a[1:], 0
        while j < len(letters):                       # a cluster as cobra reads it: bool letters, then the first value letter takes the rest or the next word
            c = letters[j]
            if c in "XfFHqtp":
                rest = letters[j + 1:]
                if rest:
                    value = rest.lstrip("=") if rest.startswith("=") else rest
                else:
                    value, i = (args[i] if i < len(args) else ""), i + 1
                if c == "X":
                    method = value
                elif c in "fF":
                    fields.append(split_field(value, "raw" if c == "f" else "field"))
                break
            j += 1
    return endpoints, (method.upper() if method is not None else None), fields, inp


def split_field(raw, kind):
    key, eq, value = raw.partition("=")
    key = fold(re.split(r"\[", key, 1)[0])
    if not eq or (kind == "field" and (value.startswith("@") or value.startswith(":") or value.startswith("{") or value.startswith("["))):
        return (key, None)
    return (key, value)


def read_file_limited(path):
    if path == "-" or path.startswith("-"):
        return None
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(MAX_FILE)
    except (OSError, ValueError):
        return None


def graphql_check(args, endpoints, fields, inp):
    """Refusal text when a graphql call may carry one of the guarded mutations (a mutation names a node id, not a repository, so it cannot be tied to a
    labelled pull request: refused while any hold is configured), else None. Reads the query from the call itself; an unreadable one is refused."""
    texts, unknown = [], False
    for key, value in fields:
        if key == "query":
            if value is None:
                unknown = True
            else:
                texts.append(value)
    # a `-F query=@file`: the file is read
    for i, a in enumerate(args):
        if a in ("-F", "--field", "-f", "--raw-field") and i + 1 < len(args) and fold(args[i + 1].split("=", 1)[0]) == "query":
            value = args[i + 1].split("=", 1)[1] if "=" in args[i + 1] else ""
            if value.startswith("@"):
                text = read_file_limited(value[1:])
                if text is None:
                    return "unreadable"
                texts.append(text)
                unknown = False
    for i, a in enumerate(args):
        if a == "--input" or a.startswith("--input="):
            path = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            text = read_file_limited(path)
            if text is None:
                return "unreadable"
            texts.append(text)
    if unknown:
        return "unreadable"
    found = sorted({m.group(1) for t in texts for m in GRAPHQL_GUARDED.finditer(t)})
    return ", ".join(found) if found else None


def api_actions(method, segs, fields, inp):
    """[(kind, detail)] for the part of an endpoint after `repos/o/r`: the guarded writes on a pull request and the label removals."""
    out = []
    if not segs:
        return out

    def num(s):
        m = re.match(r"^([0-9]+)(\.[a-z]+)?$", s, re.I)
        return int(m.group(1)) if m else None

    def field(key):
        return [v for k, v in fields if k == key]

    first = fold(segs[0])
    n = num(segs[1]) if len(segs) > 1 else None
    low = [fold(s) for s in segs]
    if first == "pulls" and n is not None:
        if len(segs) == 3 and low[2] == "merge":
            out.append(("guard", "merge"))
        elif len(segs) == 2 and method not in ("GET",):
            if inp or field("base") or any(v is None or fold(v) == "closed" for v in field("state")):
                out.append(("guard", "close or branch edit"))
        elif len(segs) == 3 and low[2] == "reviews" and method == "POST":
            if inp or any(v is None or fold(v) == "approve" for v in field("event")):
                out.append(("guard", "approving review"))
        elif len(segs) == 5 and low[2] == "reviews" and low[4] == "events" and method == "POST":
            if inp or any(v is None or fold(v) == "approve" for v in field("event")):
                out.append(("guard", "approving review"))
    elif first == "issues" and n is not None:
        if len(segs) == 2 and method not in ("GET",):
            if inp or any(v is None or fold(v) == "closed" for v in field("state")):
                out.append(("guard", "close"))
            if inp or field("labels"):
                out.append(("remove_read", None))
        elif len(segs) == 3 and low[2] == "labels" and method in ("DELETE", "PUT", "PATCH"):
            out.append(("remove_read", None))
        elif len(segs) == 4 and low[2] == "labels" and method not in ("GET", "POST"):
            out.append(("remove_name", [fold(segs[3]), fold(urllib.parse.unquote(segs[3]))]))
    elif first == "labels" and len(segs) == 2 and method not in ("GET",):
        out.append(("remove_name", [fold(segs[1]), fold(urllib.parse.unquote(segs[1]))]))
    return out


def check_api(args, ctx, holds):
    endpoints, method, fields, inp = parse_api(args)
    if not endpoints:
        return Result()
    for ep in endpoints:
        if (clean_endpoint(ep) or [""])[0].lower() == "graphql":
            found = graphql_check(args, endpoints, fields, inp)
            if found == "unreadable":
                return Result("gh: a graphql query that cannot be read cannot be checked against a hold label: refused (pass the query inline with -f query=...)")
            if found:
                return Result("gh: graphql %s cannot be tied to a pull request, so it is refused while a hold label is configured: use the gh pr verbs" % clean(found))
            return Result()
    if method is None:
        method = "POST" if (fields or inp) else "GET"
    if method == "GET":
        return Result()
    result = Result()
    for ep in endpoints:
        segs = clean_endpoint(ep)
        if not segs:
            continue
        if fold(segs[0]) == "repositories":
            if len(segs) > 2 and fold(segs[2]) in ("pulls", "issues", "labels"):
                return Result(UNFOLLOWED)      # a numeric repository id: the repository cannot be named, refuse
            continue
        if fold(segs[0]) != "repos" or len(segs) < 3:
            continue
        owner_repo, after = segs[1] + "/" + segs[2], segs[3:]
        actions = api_actions(method, after, fields, inp)
        if not actions:
            continue
        if re.search(r"[{}:]", owner_repo):             # gh fills {owner}/{repo} from the directory (or GH_REPO)
            repo = norm_repo(os.environ["GH_REPO"]) if os.environ.get("GH_REPO") else None
            if repo is None:
                got = ctx.run(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
                repo = norm_repo(got.strip()) if got else None
            if repo is None:
                return Result(RETRY)
        else:
            repo = norm_repo(owner_repo)
            if repo is None:
                return Result(UNFOLLOWED)
        entry = holds.get(repo)
        if entry is None:
            continue
        if entry.get("bad"):
            return Result(BAD_CONFIG, repo)
        number = None
        if len(after) > 1 and re.match(r"^[0-9]+", after[1]):
            number = int(re.match(r"^[0-9]+", after[1]).group(0))
        result.repo, result.number = repo, number
        for kind, detail in actions:
            if kind == "remove_name":
                if entry["label"] in detail:
                    why = removal_refusal(entry, repo, ctx)
                    if why:
                        return Result(why, repo, number)
                continue
            if kind == "remove_read" and removal_refusal(entry, repo, ctx) is None:
                continue
            if number is None:
                return Result(UNFOLLOWED, repo)
            data = read_json(ctx, ["api", "repos/%s/issues/%d" % (repo, number)])
            names = label_names(data) if data is not None else None
            if names is None:
                return Result(RETRY, repo, number)
            if not isinstance(data.get("pull_request"), dict) or entry["label"] not in names:
                continue
            if kind == "remove_read":
                return Result(removal_refusal(entry, repo, ctx) or None, repo, number)
            return Result(hold_refusal(entry, detail), repo, number)
    return result


# ---- the entry point

def alias_check(first, ctx):
    """A word that is no gh command may be an alias that expands to a guarded verb: refused when `gh alias list` names it (or cannot be read)."""
    raw = ctx.run(["alias", "list"])
    if raw is None:
        return Result(RETRY)
    for line in raw.splitlines():
        if line.split(":", 1)[0].strip() == first:
            return Result("gh: %s is an alias, and an alias cannot be checked against a hold label: run the full command instead" % clean(first)[:40])
    return Result()


def check(args, ctx):
    """Result(refusal text or None, repo or None, number or None). Never raises: an unexpected error refuses a WRITE (fail closed) and lets a read run."""
    try:
        state, holds = load_holds()
        if state == "none":
            return Result()
        words = command_words(args)
        first = words[0] if words else ""
        is_api = first == "api"
        if not ctx.write and not is_api:
            return Result()
        if state == "bad":
            guarded = is_api or (len(words) >= 2 and (first, words[1]) in SPECS) or first not in KNOWN_COMMANDS
            return Result(BAD_CONFIG) if guarded and ctx.write else Result()
        if is_api:
            return check_api(args, ctx, holds)
        if first in ("pr", "issue", "label"):
            return check_pr_issue(args, ctx, holds)
        if first and first not in KNOWN_COMMANDS and ctx.write:
            return alias_check(first, ctx)
        return Result()
    except Exception:
        return Result(RETRY) if ctx.write else Result()


# ---- the audit

def write_audit(args, name, outcome, repo=None, number=None):
    """One line per gh write: time, verb, repository, number, caller, outcome (`passed` = handed to gh, `refused`). Never a command line. Best effort."""
    try:
        state = os.environ.get("FLEET_SWITCHBOARD_STATE") or os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"), "fleet-switchboard")
        if not os.path.isdir(state):
            return False
        words, endpoints, method = [], [], None
        words = command_words(args)
        if words[:1] == ["api"]:
            words = ["api"]
        else:
            words = words[:2]
            words = words if all(re.match(r"^[a-z][a-z0-9-]{0,24}$", a) for a in words) else []
        if words[:1] == ["api"]:
            endpoints, method, _fields, _inp = parse_api(args)
            if method is None:
                method = "POST" if (_fields or _inp) else "GET"
            verb = "api " + (method if re.match(r"^[A-Z]{3,7}$", method or "") else "?")
            if endpoints and repo is None:
                segs = clean_endpoint(endpoints[0])
                if len(segs) >= 3 and fold(segs[0]) == "repos":
                    repo = norm_repo(segs[1] + "/" + segs[2])
                    if len(segs) > 4 and re.match(r"^[0-9]+$", segs[4]) and number is None:
                        number = int(segs[4])
        else:
            verb = " ".join(words) if words else "?"
            sc = split_command(args)
            if sc is not None and repo is None:
                cands = [norm_repo(r) for r in sc[3]]
                for k, a in enumerate(sc[2]):
                    if a in ("-R", "--repo") and k + 1 < len(sc[2]):
                        cands.append(norm_repo(sc[2][k + 1]))
                    elif a.startswith("--repo="):
                        cands.append(norm_repo(a.split("=", 1)[1]))
                    elif a.startswith("-R") and not a.startswith("--") and len(a) > 2:
                        cands.append(norm_repo(a[2:].lstrip("=")))
                positional = [a for a in sc[2] if not a.startswith("-")]
                for a in positional:
                    m = URL_RE.match(a)
                    if m:
                        cands.append(norm_repo(m.group(1)))
                        number = number or int(m.group(3))
                if not cands and os.environ.get("GH_REPO"):
                    cands.append(norm_repo(os.environ["GH_REPO"]))
                repo = next((c for c in cands if c), None)
                if number is None:
                    for a in positional:
                        if a.isdigit():
                            number = int(a)
                            break
        line = json.dumps({"ts": int(time.time()), "verb": verb, "repo": repo if isinstance(repo, str) else None,
                           "number": number if isinstance(number, int) else None, "caller": name if isinstance(name, str) else "agent",
                           "outcome": outcome}, sort_keys=True)
        folder = state.rstrip(os.sep) + "-audit"      # beside the state directory, not in it: the state directory holds only what the daemon reads
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, AUDIT_FILE)
        if os.path.exists(path) and os.path.getsize(path) >= AUDIT_LIMIT:
            os.replace(path, path + ".1")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as out:
            out.write(line + "\n")
        return True
    except Exception:
        return False
