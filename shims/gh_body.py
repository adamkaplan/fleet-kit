"""gh_body: the body guard of the gh shim (ask #1751).

A body write to an ask must keep the `## Intent` heading line and the `Done when:` line of the body it replaces. `shims/gh` loads this file by path
(like gh_hold.py, never `import`) and calls `guard` once per write, right after the hold check. The decision is the pure `refusal(current, new)`; the
rest reads the NEW body out of each form that writes one:

  gh issue edit N [N...] with --body, -b, --body-file, -F (a file or - for stdin; the joined spellings too),
  gh api to repos/O/R/issues/N (or repositories/ID/issues/N) with -f/-F body=..., -F body=@file, -F body=@-, --input FILE or --input -,
  gh api graphql with a query that names updateIssue (refused: it names a node id, so the body it replaces cannot be read).

The current body is read through the real gh with the account the write would use and FLEET_SHIM_HOLD_SECONDS as the limit of EACH read (counted from the read), and a command-wide budget of three times that limit, after which every further read fails closed ("the command's read budget is spent"). FAIL CLOSED: a current body
that cannot be read or is empty, a form whose new body cannot be read, and a call that fails here for any reason refuse a WRITE. A current body with
neither part is not an ask: the write passes. Pull requests (an `issues/N` endpoint whose answer has `pull_request`) are not touched. Nothing else is.
Stdin that was read is handed back: fd 0 is replaced by a file holding the same text, so the shim and gh read it as before.

Only the standard library. No repository name is known here.
"""
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse

HEAD_RE = re.compile(r"^## Intent[ \t]*\r?$", re.M)
DONE_RE = re.compile(r"^Done when:", re.M)
UPDATE_RE = re.compile(r"\bupdateIssue\b")
MAX_TEXT = 1048576
WHY_CHARS = 200   # the read's stderr as the refusal shows it: the first this many characters, one line
WHY_READ = 4096   # only this much of the stderr is ever scanned (a limit on the work, whatever the read printed)
TOKEN_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")   # the same four as shims/gh (a test compares them)
# A deliberate subset of `_SECRET_PATTERNS` in bin/fleet-switchboard (the shapes a gh error carries; this file stands alone, a test shows what each one catches). A net, not a promise (a token with no known prefix that is not one the read ran with is not caught): the shapes of `_SECRET_PATTERNS` in bin/fleet-switchboard (this file stands alone), every run of name characters bounded so no scan is quadratic.
SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic|token|digest|negotiate)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [redacted]"),
    (re.compile(r"(?i)(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]{0,64}(?:token|secret|passw(?:or)?d|api[_-]?key|credential|auth)[A-Za-z0-9_.-]{0,64}[\"']?\s{0,4}[=:]\s{0,4})"
                r"(\"[^\"]{0,512}\"|'[^']{0,512}'|\S+)"), r"\1[redacted]"),
    (re.compile(r"(?i)(--?(?:token|password|passwd|secret|api-key)[ =])\S+"), r"\1[redacted]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{12,}|xox[abprs]-[A-Za-z0-9-]{8,})"), "[redacted]"),
    (re.compile(r"\b[0-9a-fA-F]{40}\b"), "[redacted]"),
    (re.compile(r"(://[^/\s:@]+:)\S{1,256}@"), r"\1[redacted]@"),
    (re.compile(r"(://)[^/\s@:]{1,256}@"), r"\1[redacted]@"),
    (re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}(?:\.[A-Za-z0-9_-]*)?"), "[redacted]"),
)
DROP = ("gh: refused: this body would drop the Intent or the Done-when of the issue; change one line with `fleet-switchboard issue-line`, "
        "or send the whole body with both parts")
UNREAD = ("gh: refused: the current body of the issue could not be read, or is empty, so a body write cannot be checked: retry "
          "(or change one line with `fleet-switchboard issue-line`)")
UNREAD_WHY = ("gh: refused: the current body of the issue could not be read (%s), so a body write cannot be checked: retry "
              "(or change one line with `fleet-switchboard issue-line`)")
UNCLASSIFIED = ("gh: refused: this write to an issue cannot be classified (the new body cannot be read from it), so it cannot be checked: send the body "
                "with -f body=..., -F body=@file or --input with a JSON object, or use `fleet-switchboard issue-line`")
GRAPHQL = ("gh: refused: a GraphQL updateIssue cannot be checked against the body it replaces: use `gh issue edit` with a body that keeps both parts, "
           "or `fleet-switchboard issue-line`")
FAILED = "gh: refused: the body guard failed, retry (the fleet shim refuses this write rather than let it through)"
ISSUE_VALUE = {"-R", "--repo", "-t", "--title", "-m", "--milestone", "-a", "--add-assignee", "--remove-assignee", "-l", "--add-label",
               "--remove-label", "-p", "--add-project", "--remove-project", "-b", "--body", "-F", "--body-file"}

REPO_FLAGS = ("-R", "--repo")   # the one table of the spellings that name the repository; every loop reads this table, the issue-edit loops through repo_flag


def repo_flag(args, i):
    """(the repository named by args[i], the words it takes) when args[i] spells -R V, -RV, -R=V, --repo V or --repo=V, else None."""
    a = args[i]
    if a in REPO_FLAGS:
        return (args[i + 1], 2) if i + 1 < len(args) else None
    for flag in REPO_FLAGS:
        rest = a[len(flag):]
        if a.startswith(flag) and rest and (not flag.startswith("--") or rest.startswith("=")):
            return (rest[1:] if rest.startswith("=") else rest), 1
    return None


def has_parts(body):
    return bool(HEAD_RE.search(body)) and bool(DONE_RE.search(body))


def refusal(current_body, new_body):
    """The one refusal line, or None. Pure. A current body that is not text or is blank is unreadable; one without both parts is not an ask."""
    if not isinstance(current_body, str) or not current_body.strip():
        return UNREAD
    if not has_parts(current_body):
        return None
    if not isinstance(new_body, str) or not has_parts(new_body):
        return DROP
    return None


def clean_stderr(text, env=None, limit=WHY_CHARS):
    """The first `limit` characters of a read's stderr as ONE line of printable ASCII: control and other characters become spaces, anything token-shaped (and any
    token the read ran with, also URL- or JSON-escaped) is redacted. Only the first WHY_READ characters are scanned; when there were more, the last word of them,
    which may be a token cut in two, is dropped. Redacting BEFORE the cut to `limit` is deliberate: a token is never shown half."""
    raw = str(text or "")
    cut = len(raw) > WHY_READ
    raw = raw[:WHY_READ]

    def known(text):   # the tokens the read ran with (a value of 4 or more characters), as they are, URL-escaped and JSON-escaped
        for name in TOKEN_ENV_NAMES:
            value = (env or {}).get(name)
            if value and len(value) >= 4:
                for form in {value, urllib.parse.quote(value, safe=""), json.dumps(value)[1:-1]}:
                    text = text.replace(form, "[redacted]")
        return text
    raw = known(raw)   # before the control characters go, so a value that holds one is still found
    raw = "".join(c if c.isascii() and c.isprintable() else " " for c in raw)
    if cut:
        raw = raw.rstrip().rpartition(" ")[0]
    raw = known(raw)
    for pattern, replacement in SECRET_PATTERNS:
        raw = pattern.sub(replacement, raw)
    return " ".join(raw.split())[:limit]


class Unclassified(Exception):
    pass


class Probe:
    def __init__(self, gh, env_of, stdin):
        self.gh, self.env_of, self.stdin, self.text = gh, env_of, stdin, None
        try:
            seconds = float(os.environ.get("FLEET_SHIM_HOLD_SECONDS", 5.0))
        except ValueError:
            seconds = 5.0
        self.seconds, self.why = min(10.0, max(0.2, seconds)), None   # the body read's OWN limit, counted from the read, not from here
        self.budget = 3 * self.seconds   # the whole command's read time: three times the per-read limit

    def stdin_text(self):
        if self.text is None:
            try:
                self.text = self.stdin.read()
            except (OSError, ValueError):
                raise Unclassified()
        return self.text

    def give_back(self):
        """Put the stdin text that was read back on fd 0 (a file), so a later reader sees the whole text again."""
        if self.text is None:
            return
        with tempfile.TemporaryFile() as handle:
            handle.write(self.text.encode("utf-8", "surrogateescape"))
            handle.flush()
            handle.seek(0)
            os.dup2(handle.fileno(), 0)
        sys.stdin = open(0, "r", closefd=False)

    def file_text(self, path):
        if path == "-":
            return self.stdin_text()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)   # never blocks on a FIFO; only a regular file is read
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise Unclassified()
                with os.fdopen(fd, "r", closefd=False) as handle:
                    text = handle.read(MAX_TEXT + 1)
            finally:
                os.close(fd)
        except (OSError, UnicodeError):
            raise Unclassified()
        if len(text) > MAX_TEXT:
            raise Unclassified()
        return text

    def fetch(self, argv):
        """The JSON object gh prints for a read, or None on any failure; self.why then says which (a time-out, an exit status, unparsable output)."""
        if self.budget <= 0:
            self.why = "the command's read budget is spent"
            return None
        left = min(self.seconds, self.budget)
        started = time.monotonic()
        try:
            return self.run_read(argv, left)
        finally:
            self.budget -= time.monotonic() - started
            if self.budget <= 0 and self.why is None:
                self.budget = 0

    def run_read(self, argv, left):
        if not self.gh:
            self.why = "no gh to read it with"
            return None
        try:
            env = dict(self.env_of() or os.environ)
            env["GH_PROMPT_DISABLED"] = "1"
            env["GH_PAGER"] = "cat"
            done = subprocess.run([self.gh] + argv, capture_output=True, text=True, errors="replace", timeout=left, stdin=subprocess.DEVNULL, env=env)
            if done.returncode != 0:
                said = clean_stderr(done.stderr, env)
                self.why = "the read exited with status %d%s" % (done.returncode, ": " + said if said else "")
                return None
            data = json.loads(done.stdout) if done.stdout else None
        except subprocess.TimeoutExpired:
            self.why = "the read timed out after %.1f seconds" % left
            return None
        except ValueError:
            self.why = "the read printed output that is not JSON"
            return None
        except Exception as error:
            self.why = "the read could not run (%s)" % type(error).__name__
            return None
        if not isinstance(data, dict):
            self.why = "the read printed output that is not a JSON object"
            return None
        return data


def check_current(probe, argv, new_body):
    data = probe.fetch(argv)
    if data is None:
        return UNREAD_WHY % (probe.why or "no reason given")
    if isinstance(data.get("pull_request"), dict):
        return None
    return refusal(data.get("body") or "", new_body)


# ---- gh issue edit

def issue_edit(probe, args):
    words, repos, i, edit_at = [], [], 0, None
    while i < len(args):
        a = args[i]
        named = repo_flag(args, i)
        if named:
            repos.append(named[0])
            i += named[1]
            continue
        if not a.startswith("-"):
            words.append(a)
            if len(words) == 2:
                edit_at = i
                break
        i += 1
    if edit_at is None:
        return None
    body, targets, i = None, [], edit_at + 1
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            targets += args[i:]
            break
        kind, value = None, None
        named = repo_flag(args, i - 1)
        if named:
            repos.append(named[0])
            i += named[1] - 1
            continue
        if a in ("--body", "-b", "--body-file", "-F"):
            kind = "body" if a in ("--body", "-b") else "file"
            value = args[i] if i < len(args) else None
            i += 1
        elif a.startswith("--body="):
            kind, value = "body", a[7:]
        elif a.startswith("--body-file="):
            kind, value = "file", a[12:]
        elif a.startswith(("-b", "-F")) and not a.startswith("--"):
            kind, value = ("body" if a[1] == "b" else "file"), a[3:] if a[2:3] == "=" else a[2:]   # one "=" is the separator, as pflag reads it
        elif a in ISSUE_VALUE:
            i += 1
        elif not a.startswith("-") or a == "-":
            targets.append(a)
        if kind is not None:
            if value is None:
                raise Unclassified()
            body = (kind, value)
    if body is None:
        return None
    new = body[1] if body[0] == "body" else probe.file_text(body[1])
    if not targets:
        raise Unclassified()
    for target in targets:
        found = check_current(probe, ["issue", "view", target, "--json", "body"] + (["-R", repos[-1]] if repos else []), new)
        if found:
            return found
    return None


# ---- gh api

def clean_endpoint(text):
    t = re.sub(r"^https?://[^/]+/", "", str(text).strip(), flags=re.I)
    segs = []
    for seg in re.split(r"[?#]", t, 1)[0].replace("\\", "/").split("/"):
        seg = urllib.parse.unquote(seg)
        if seg in ("", "."):
            continue
        if seg == "..":
            if segs:
                segs.pop()
            continue
        segs.append(seg)
    return segs[2:] if segs[:2] == ["api", "v3"] else segs


def query_texts(probe, parsed):
    texts = []
    for _i, name, _j, text in parsed.fields:
        key, eq, value = text.partition("=")
        if key == "query" and eq:
            texts.append(probe.file_text(value[1:]) if name == "field" and value.startswith("@") else value)
    if parsed.input is not None:
        try:
            data = json.loads(probe.file_text(parsed.input[3]))
        except ValueError:
            raise Unclassified()
        if not isinstance(data, dict):
            raise Unclassified()
        if isinstance(data.get("query"), str):
            texts.append(data["query"])
    return texts


def new_bodies(probe, parsed):
    """Every new body the call carries (texts; None for a JSON null), in order; [] when it carries none."""
    found = []
    for _i, name, _j, text in parsed.fields:
        key, eq, value = text.partition("=")
        if key.split("[", 1)[0].lower() != "body":
            continue
        if not eq:
            raise Unclassified()
        found.append(probe.file_text(value[1:]) if name == "field" and value.startswith("@") else value)
    if parsed.input is not None:
        try:
            data = json.loads(probe.file_text(parsed.input[3]))
        except ValueError:
            raise Unclassified()
        if not isinstance(data, dict):
            raise Unclassified()
        for key in data:
            if isinstance(key, str) and key.lower() == "body":
                if data[key] is not None and not isinstance(data[key], str):
                    raise Unclassified()
                found.append(data[key])
    return found


def api_write(probe, args, api_parser):
    parsed = api_parser(args)
    if parsed.effective_method == "GET" or not parsed.positionals:
        return None
    endpoint = parsed.positionals[0][1]
    segs = clean_endpoint(endpoint)
    low = [s.lower() for s in segs]
    if low[:1] == ["graphql"]:
        try:
            texts = query_texts(probe, parsed)
        except Unclassified:
            return None   # an unreadable query or --input is refused by the shim's own GraphQL rule, with its own line
        return GRAPHQL if any(UPDATE_RE.search(t) for t in texts) else None
    if low[:1] == ["repos"] and len(segs) == 5 and low[3] == "issues":
        number = segs[4]
    elif low[:1] == ["repositories"] and len(segs) == 4 and low[2] == "issues":
        number = segs[3]
    else:
        return None
    if not number.isdigit():
        if re.search(r"[{}:]", number):
            raise Unclassified()
        return None
    for new in new_bodies(probe, parsed):
        found = check_current(probe, ["api"] + ([] if parsed.hostname is None else ["--hostname", parsed.hostname]) + [endpoint], new)
        if found:
            return found
    return None


def guard(args, write, api_parser, gh, env_of, stdin=None):
    """The refusal line for a body write that would drop the Intent or the Done-when of the issue it replaces, else None. Reads never refuse."""
    if not write:
        return None
    probe = Probe(gh, env_of, stdin or sys.stdin)
    try:
        words, skip = [], False
        for a in args:
            if skip:
                skip = False
            elif a in REPO_FLAGS:
                skip = True
            elif not a.startswith("-"):
                words.append(a)
        if words[:2] == ["issue", "edit"]:
            return issue_edit(probe, args)
        if words[:1] == ["api"]:
            return api_write(probe, args, api_parser)
        return None
    except Unclassified:
        return UNCLASSIFIED
    except Exception:
        return FAILED
    finally:
        try:
            probe.give_back()
        except Exception:
            pass
