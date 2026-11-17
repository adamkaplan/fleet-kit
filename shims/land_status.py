"""Commit-status dual-write for a gated pull request (#2560). Stdlib only; loaded by path from the `gh` shim beside it.

Before a `pr merge` runs, `land/gates`, `land/reviews` and `land/merge-gate` are posted as `success` on the checked head; after it
exits 0 they are posted on the commit GitHub reports for the pull request. The token is minted HOST-ONLY here, for one repository
and `{"statuses": "write"}` only, and is revoked in a `finally:`. Nothing in this module can stop a merge: every failure is
caught, reduced to a step name plus an HTTP status or an exception class name (never a token, never a body), and handed to the spool.
"""
import base64
import json
import os
import re
import subprocess
import pwd
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONTEXTS = ("land/gates", "land/reviews", "land/merge-gate")
DEFAULT_API = "https://api.github.com"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
MAX_ENV_BYTES = 65536
MAX_KEY_BYTES = 65536
MAX_RESPONSE_BYTES = 1 << 20
OPENSSL_CANDIDATES = ("/usr/bin/openssl", "/opt/homebrew/bin/openssl", "/usr/local/bin/openssl")
# Test hooks: module attributes, never read from the environment (a caller's environment must not move the token or the API).
API_BASE = DEFAULT_API
SECRET_DIR = None
HOME_DIR = None
TOKEN = None
OPENSSL = None
WANTED = {"statuses": "write"}
TIMEOUT = 15


class StatusError(Exception):
    """A failed step; its text is only ever the step name and an HTTP status, so it is safe to log."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # a redirect is an HTTP error: the token is never sent to another place


def valid_repo(repo):
    """A strict OWNER/REPO: no `.` or `..` segment, no leading or trailing dot, no `.git` suffix."""
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        return False
    return all(part not in (".", "..") and not part.startswith(".") and not part.endswith(".") and not part.endswith(".git")
               for part in repo.split("/"))


def api_base():
    base = (API_BASE or "").rstrip("/")
    if base == DEFAULT_API:
        return DEFAULT_API
    try:
        u = urllib.parse.urlsplit(base)
        if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost") and u.username is None and u.password is None:
            return base
    except ValueError:
        pass
    return DEFAULT_API


def secret_dir():
    if SECRET_DIR:
        return SECRET_DIR
    try:
        home = HOME_DIR or pwd.getpwuid(os.getuid()).pw_dir
        with open(os.path.join(home, ".config", "fleet-switchboard", "cloud.env")) as handle:
            text = handle.read(MAX_ENV_BYTES + 1)
        if len(text) > MAX_ENV_BYTES:
            return None
        for line in text.splitlines():
            if line.startswith("CW_SECRET_DIR="):
                return line.split("=", 1)[1].strip().strip("\"'")
    except (OSError, KeyError, UnicodeError):
        pass
    return None


def is_configured():
    if TOKEN or api_base() != DEFAULT_API:
        return True
    directory = secret_dir()
    return bool(directory) and os.path.isdir(directory)


def call(method, path, token, body=None, expect=(200, 201, 204), jwt=False):
    """One request; returns (status, parsed json or None). Raises StatusError(step, status) only."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(api_base() + path, data=data, method=method)
    request.add_header("Authorization", ("Bearer " if jwt else "token ") + token)
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("User-Agent", "fleet-land-status")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as answer:
            status, raw = answer.status, answer.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as err:
        raise StatusError("%s HTTP %d" % (method, err.code))
    if len(raw) > MAX_RESPONSE_BYTES:
        raise StatusError("%s response oversize" % method)
    if status not in expect:
        raise StatusError("%s HTTP %d" % (method, status))
    try:
        return status, (json.loads(raw) if raw else None)
    except ValueError:
        return status, None


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def sign_jwt(app_id, key_text):
    """A 9-minute RS256 JWT. The key goes to openssl through a pipe: never a file, never argv."""
    now = int(time.time())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = b64(json.dumps({"iat": now - 30, "exp": now + 540, "iss": app_id}).encode())
    signing = (head + "." + claims).encode()
    read_end, write_end = os.pipe()
    def feed():
        try:
            with os.fdopen(write_end, "wb") as out:
                out.write(key_text.encode())
        except OSError:
            pass
    threading.Thread(target=feed, daemon=True).start()
    try:
        openssl = OPENSSL or next((c for c in OPENSSL_CANDIDATES if os.access(c, os.X_OK)), None)
        if not openssl:
            raise StatusError("sign failed")
        done = subprocess.run([openssl, "dgst", "-sha256", "-sign", "/dev/fd/%d" % read_end], input=signing, capture_output=True,
                              pass_fds=(read_end,), timeout=TIMEOUT, env={})
    except (OSError, subprocess.SubprocessError):
        raise StatusError("sign failed")
    finally:
        os.close(read_end)
    if done.returncode != 0 or not done.stdout:
        raise StatusError("sign failed")
    return (head + "." + claims + "." + b64(done.stdout))


def mint(repo):
    """An installation token for `repo` with exactly {"statuses": "write"}, or StatusError."""
    if not valid_repo(repo):
        raise StatusError("bad repository or sha")
    directory = secret_dir()
    if not directory:
        raise StatusError("no secret directory")
    try:
        with open(os.path.join(directory, "cw-app-id")) as handle:
            app_id = handle.read(MAX_KEY_BYTES + 1)
        with open(os.path.join(directory, "cw-app-key")) as handle:
            key_text = handle.read(MAX_KEY_BYTES + 1)
    except (OSError, UnicodeError):
        raise StatusError("app credentials unreadable")
    if len(app_id) > MAX_KEY_BYTES or len(key_text) > MAX_KEY_BYTES:
        raise StatusError("app credentials oversize")
    app_id = app_id.strip()
    if not app_id or not key_text:
        raise StatusError("app credentials empty")
    jwt = sign_jwt(app_id, key_text)
    _, found = call("GET", "/repos/%s/installation" % repo, jwt, jwt=True, expect=(200,))
    installation = (found or {}).get("id")
    if not (isinstance(installation, int) and not isinstance(installation, bool) and installation > 0):
        raise StatusError("installation lookup bad response")
    name = repo.split("/", 1)[1]
    _, made = call("POST", "/app/installations/%d/access_tokens" % installation, jwt, {"repositories": [name], "permissions": dict(WANTED)}, jwt=True, expect=(201,))
    token = (made or {}).get("token")
    if not isinstance(token, str) or not token:
        raise StatusError("token mint bad response")
    repos = (made or {}).get("repositories")
    named = isinstance(repos, list) and len(repos) == 1 and isinstance(repos[0], dict) and \
        isinstance(repos[0].get("name"), str) and repos[0]["name"].lower() == name.lower()
    if (made or {}).get("permissions") != WANTED or not named:
        revoke(token)
        raise StatusError("token permissions not narrowed")
    return token


def revoke(token):
    try:
        call("DELETE", "/installation/token", token, expect=(204,))
    except Exception:
        pass


def post_statuses(repo, sha, token, descriptions=None):
    """POST each context as success on `sha`."""
    if not valid_repo(repo) or not isinstance(sha, str) or not SHA_RE.match(sha):
        raise StatusError("bad repository or sha")
    for context in CONTEXTS:
        text = (descriptions or {}).get(context) or "checked by the fleet gates"
        call("POST", "/repos/%s/statuses/%s" % (repo, sha), token, {"state": "success", "context": context, "description": text[:140]}, expect=(201,))


def write_all(repo, sha, descriptions=None):
    """Mint (or take the explicit token), post, and always revoke."""
    token = TOKEN
    minted = False
    if not token:
        token = mint(repo)
        minted = True
    try:
        post_statuses(repo, sha, token, descriptions)
    finally:
        if minted:
            revoke(token)


def read_merge_commit_oid(gh, repo, number, env=None, timeout=15):
    argv = [gh, "pr", "view", str(number), "-R", repo, "--json", "mergeCommit", "--jq", ".mergeCommit.oid"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (done.stdout or "").strip()
    return text if done.returncode == 0 and SHA_RE.match(text) else None


def enabled(repo, listed):
    return bool(repo) and repo.lower() in {str(r).lower() for r in (listed or ())} and is_configured()


def _note(spool_fn, repo, number, reason):
    try:
        spool_fn("land_status", repo, number, "merge", reason)
    except Exception:
        pass


def _reason(step, err):
    if isinstance(err, StatusError):
        return "%s: %s" % (step, err)   # StatusError text is built only from fixed words and an HTTP code
    return "%s: %s" % (step, type(err).__name__)


def dual_write_before(repo, number, head, spool_fn, listed=None):
    """Statuses on the checked head. Never raises. True when written."""
    try:
        if not enabled(repo, listed):
            return False
        write_all(repo, head, {c: "gates passed on the checked head" for c in CONTEXTS})
        return True
    except BaseException as err:
        if isinstance(err, (KeyboardInterrupt, SystemExit)):
            raise
        _note(spool_fn, repo, number, _reason("before", err))
        return False


def dual_write_after(repo, number, head, gh, env, spool_fn, listed=None):
    """Statuses on the commit the merge made. Never raises. True when written."""
    try:
        if not enabled(repo, listed):
            return False
        oid = read_merge_commit_oid(gh, repo, number, env)
        if oid is None:
            raise StatusError("no mergeCommit.oid")
        write_all(repo, oid, {c: "gates passed on the merged pull request" for c in CONTEXTS})
        return True
    except BaseException as err:
        if isinstance(err, (KeyboardInterrupt, SystemExit)):
            raise
        _note(spool_fn, repo, number, _reason("after", err))
        return False


def target(args, release_ref=None, bound=None):
    """(repo, number, head) from the gate-verified binding `bound`, or None. Nothing is parsed from `args`: a caller-supplied
    -R, number or --match-head-commit never reaches a status write."""
    try:
        repo, number, head = bound
        if not valid_repo(repo) or not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            return None
        if not isinstance(head, str) or not SHA_RE.match(head):
            return None
        return repo, number, head
    except Exception:
        return None
