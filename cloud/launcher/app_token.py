#!/usr/bin/env python3
"""app-token: print a one-hour GitHub App installation token for ONE repository, to stdout, and nothing else.

usage: app_token.py OWNER/REPO [--role worker|orchestrator]

The App id and private key are read from the vault by commands (stdout of each, in memory only):
  CW_APP_ID_CMD, CW_APP_KEY_CMD
  (a command, or the vault default; CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME have no defaults)
  default: az keyvault secret show --subscription $CW_VAULT_SUBSCRIPTION --vault-name $CW_VAULT_NAME --name cw-app-id
           | cw-app-key --query value -o tsv   (the subscription and vault come from the env)
The installation is NOT read from the vault: GET /repos/OWNER/REPO/installation (with the App JWT) names the installation that
covers the repository, and its account must be the repository owner. CW_APP_INSTALLATION_CMD and a stored installation id are obsolete:
accepted, never run, ignored.
The key goes to `openssl dgst -sha256 -sign /dev/fd/N` through a pipe: never a file, an argument or the environment.
The 9-minute app JWT and the token are never printed except the token, once, on stdout. Errors go to stderr, never a value.
Only https://api.github.com is used; CW_GITHUB_API_BASE may name a loopback http URL (tests).
The request asks for, and the answer must carry EXACTLY, the permissions of the role's set in PERMISSIONS_BY_ROLE below (default worker) (no extra key, no
higher level, none missing), else no token is printed. A missing key is the App not yet granted (or the installation not yet
accepting) a permission: the refusal names it and the owner step.
CW_OPENSSL names the openssl binary (default openssl).
"""
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import token_mint  # noqa: E402

API = "https://api.github.com"
MAX_TOKEN_SECONDS = 61 * 60
MAX_BYTES = 1_000_000  # the most of any GitHub answer or vault command output that is read
# THE table of the cloud token's permissions: the request body, the exact check, the refusal text, the preflight's
# "token permissions" line and the documents all derive from it. issues: read lets the worker read its ask and plan
# (and their comments); nothing here is a write to an issue. shims/app_mint.py (the Mac shim) keeps its own, different set.
# One table of sets BY ROLE. The orchestrator's set has no contents key at all: it reads issues, pull requests and check
# results and writes to issues only.
PERMISSIONS_BY_ROLE = {
    "worker": {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"},
    "orchestrator": {"issues": "write", "pull_requests": "read", "checks": "read", "metadata": "read"},
}
EXPECTED_PERMISSIONS = PERMISSIONS_BY_ROLE["worker"]  # the worker's set: the name every existing importer uses


def render_permissions(perms=None, role="worker"):
    """The set as text: for the worker `contents: write, pull_requests: write, issues: read, metadata: read`."""
    return ", ".join("%s: %s" % kv for kv in (PERMISSIONS_BY_ROLE[role] if perms is None else perms).items())


def owner_step(role="worker"):
    """The one place that says what the owner does when the App lacks a permission of the role's set."""
    return "the owner adds %s to the App's permissions and accepts them on each installation" % render_permissions(None, role)


def open_url(req, timeout=30):
    """The one opener of the cloud programs (token_mint's): it never follows a redirect, so Authorization stays put."""
    return token_mint.open_no_redirect(req, timeout)


OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?!-)){0,38}")  # GitHub login: <=39, no leading/trailing/double hyphen
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")


def valid_owner(o):
    """A GitHub login: ASCII, 1 to 39, no leading, trailing or doubled hyphen."""
    return isinstance(o, str) and bool(OWNER_RE.fullmatch(o)) and not o.endswith("-")


def valid_target(t):
    """OWNER/REPO by GitHub's name grammar, the one check before a name goes into a URL (ASCII only, no other character)."""
    if not isinstance(t, str):
        return False
    parts = t.split("/")
    if len(parts) != 2:
        return False
    owner, repo = parts
    return bool(valid_owner(owner) and REPO_RE.fullmatch(repo)
                and repo.strip(".") != "" and not repo.endswith(".") and not repo.lower().endswith(".git"))


def http_error_text(code, retry_after=None):
    if code == 429:
        when = " Retry after %s seconds." % retry_after if isinstance(retry_after, str) and retry_after.isdigit() and len(retry_after) <= 6 \
            else " Retry later."
        return "GitHub rate limit hit (HTTP 429): the launch is refused and nothing was minted." + when
    if 300 <= code < 400:
        return "refusing: GitHub answered with a redirect (HTTP %d); a redirect is never followed" % code
    return "GitHub refused the request (HTTP %d)" % code


MINTED = []  # the one token minted and not yet printed: a refusal after the mint revokes it (never in a message)


def revoke(token):
    """Best effort: revoke an installation token (DELETE /installation/token, authorised by that token). True only on HTTP 204."""
    try:
        req = urllib.request.Request(api_base() + "/installation/token", method="DELETE",
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cw-app-token"})
        with open_url(req, 15) as r:
            return r.status == 204
    except BaseException:  # any failure (including a redirect or an exit) is "revocation failed", never a crash and never the token
        return False


LAST_FAILURE = {}  # the last die(): {'transient': bool, 'reason': text, 'retry_after': int or None}; a caller that retries reads it


def _wait_seconds(value):
    """A Retry-After header as whole seconds, or None when it is not a plain number."""
    return int(value) if isinstance(value, str) and value.isdigit() and len(value) <= 6 else None


EXIT_TEMPFAIL = 75   # sysexits "temporary failure": the process status of a passing failure (a launch retries on this status only)


def die(msg, code=1, transient=False, retry_after=None):
    """Print the reason and exit. transient=True marks a PASSING failure (the vault, the network, HTTP 5xx or 429, a 403 with Retry-After);
    everything else is a refusal. LAST_FAILURE tells the caller which."""
    if MINTED:
        token = MINTED.pop()
        msg += (" The token was revoked." if revoke(token)
                else " Revocation failed: the token expires within an hour and was never printed.")
        del token
    LAST_FAILURE.clear()
    LAST_FAILURE.update({"transient": bool(transient), "reason": msg, "retry_after": retry_after})
    print("app-token: " + msg, file=sys.stderr)
    sys.exit(EXIT_TEMPFAIL if transient and code == 1 else code)


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def vault(env_name, secret):
    cmd = os.environ.get(env_name)
    if cmd:
        argv = ["sh", "-c", cmd]
    else:
        sub, vlt = os.environ.get("CW_VAULT_SUBSCRIPTION"), os.environ.get("CW_VAULT_NAME")
        if not sub or not vlt:
            die("set CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME (or %s)" % env_name, 2)
        argv = ["az", "keyvault", "secret", "show", "--subscription", sub, "--vault-name", vlt,
                "--name", secret, "--query", "value", "-o", "tsv"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if len(p.stdout) > MAX_BYTES:
            die("the vault command for %s printed more than %d bytes" % (secret, MAX_BYTES))
    except (OSError, subprocess.TimeoutExpired):
        die("could not run the command for %s" % secret, transient=True)
    val = p.stdout.strip() if secret != "cw-app-key" else p.stdout
    if p.returncode != 0 or not val.strip():
        die("the vault command for %s failed or printed nothing" % secret, transient=True)
    return val


def sign(key, data):
    """RS256 signature of data (bytes) with the PEM key (str), the key travelling only through a pipe."""
    r, w = os.pipe()

    def feed():
        with os.fdopen(w, "w") as f:
            try:
                f.write(key)
            except OSError:
                pass
    t = threading.Thread(target=feed)
    t.start()
    try:
        p = subprocess.run([os.environ.get("CW_OPENSSL", "openssl"), "dgst", "-sha256", "-sign", "/dev/fd/%d" % r],
                           input=data, capture_output=True, pass_fds=(r,), timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        die("could not run openssl")
    finally:
        os.close(r)
        t.join()
    if p.returncode != 0 or not p.stdout:
        die("openssl could not sign (is the key a valid RSA private key?)")
    return p.stdout


def api_base():
    base = os.environ.get("CW_GITHUB_API_BASE")
    if not base:
        return API
    u = urllib.parse.urlparse(base)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
        die("CW_GITHUB_API_BASE is only allowed for a loopback http address")
    return base.rstrip("/")


def check_permissions(perms, role="worker"):
    """Refuse unless perms is exactly the role's set. Names a key, never a value."""
    EXPECTED_PERMISSIONS = PERMISSIONS_BY_ROLE[role]  # local: the set of this role (the worker's by default)
    if not isinstance(perms, dict):
        die("refusing: the answer carries no permissions object")
    for k in sorted(perms):
        if k not in EXPECTED_PERMISSIONS:
            die("refusing: the token carries an extra permission: %s" % str(k)[:40])
    for k, want in EXPECTED_PERMISSIONS.items():
        if k not in perms:
            die("refusing: the token lacks the permission: %s (%s)" % (k, owner_step(role)))
        if perms[k] != want:
            die("refusing: the token has a different level for the permission: %s" % k)


def github_json(method, path, jwt, body=None, not_found=None, role="worker"):
    """One GitHub call with the App JWT; the parsed JSON answer, or exit through die(). A 404 says not_found when given."""
    headers = {"Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cw-app-token"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(api_base() + path, data=data, method=method, headers=headers)
    try:
        with open_url(req) as r:
            raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            die("the GitHub answer is larger than %d bytes" % MAX_BYTES)
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404 and not_found:
            die(not_found)
        if e.code == 422 and method == "POST":
            die("the App's installation does not cover this repository: add it to the installation (HTTP 422); "
                "if the App lacks a requested permission, %s" % owner_step(role))
        retry = e.headers.get("Retry-After") if e.headers else None
        passing = e.code == 429 or 500 <= e.code <= 599 or (e.code == 403 and retry is not None)
        flags = {"transient": passing, "retry_after": _wait_seconds(retry) if passing else None}
        if e.code == 403 and method == "POST":  # the App may not hold, or the installation not yet accept, a requested permission
            die("%s; if the App lacks a requested permission, %s" % (http_error_text(e.code, retry), owner_step(role)), **flags)
        die(http_error_text(e.code, retry), **flags)
    except TimeoutError:
        die("the GitHub request timed out: the launch is refused and nothing was minted", transient=True)
    except Exception:  # OSError, ValueError, UnicodeDecodeError, RecursionError, MemoryError: any failure is a named refusal, never a crash
        die("the GitHub request failed or its answer was not usable JSON: the launch is refused and nothing was minted", transient=True)


def is_id(x):
    """A GitHub numeric id: an exact integer above zero (a bool, string or float is not one)."""
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def find_installation(target, jwt):
    """The id of the App installation that covers target (OWNER/REPO), asked of GitHub with the App JWT. Checked, never trusted."""
    owner = target.split("/", 1)[0]
    ans = github_json("GET", "/repos/%s/installation" % target, jwt,
                      not_found="the App is not installed on %s: install it on the account that owns the repository and add the repository" % target)
    if not isinstance(ans, dict):
        die("refusing: the installation lookup answered with something other than one installation")
    inst = ans.get("id")
    if not is_id(inst):
        die("refusing: the installation lookup carries no installation id")
    account = ans.get("account")
    login = account.get("login") if isinstance(account, dict) else None
    if not valid_owner(login) or login.lower() != owner.lower():  # ASCII only: lower() of raw non-ASCII folds Kelvin to k
        die("refusing: the installation found is not the account that owns %s" % target)
    if ans.get("suspended_at"):
        die("refusing: the App's installation on %s is suspended" % owner)
    owner_id = account.get("id")
    if not is_id(owner_id):
        die("refusing: the installation lookup carries no usable account id")
    return inst, owner_id


def mint(target, role="worker"):
    """Return a token for target (OWNER/REPO), or exit through die(). Never prints."""
    repo = target.split("/", 1)[1]
    app_id = vault("CW_APP_ID_CMD", "cw-app-id").strip()
    if not app_id.isdigit():
        die("the app id must be a number")
    key = vault("CW_APP_KEY_CMD", "cw-app-key")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64(json.dumps({"iat": now - 30, "exp": now + 9 * 60, "iss": app_id}, separators=(",", ":")).encode())
    signing = "%s.%s" % (head, claims)
    jwt = signing + "." + b64(sign(key, signing.encode()))
    del key
    inst, owner_id = find_installation(target, jwt)
    ans = github_json("POST", "/app/installations/%d/access_tokens" % inst, jwt,
                      {"repositories": [repo], "permissions": dict(PERMISSIONS_BY_ROLE[role])}, role=role)
    token = ans.get("token") if isinstance(ans, dict) else None
    repos = ans.get("repositories") if isinstance(ans, dict) else None
    if not isinstance(token, str) or not token:
        die("the answer has no token")
    MINTED.append(token)
    if not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict) or repos[0].get("name") != repo:
        die("refusing: the token does not cover exactly the one target repository")
    full = repos[0].get("full_name")
    if not (isinstance(full, str) and valid_target(full) and full.lower() == target.lower()):
        die("refusing: the token does not cover exactly the one target repository")
    owner = repos[0].get("owner")  # the account id is stable where the login is not: both answers must carry it, and it must agree
    if not isinstance(owner, dict) or not is_id(owner.get("id")):
        die("refusing: the token's repository carries no usable owner id")
    if owner.get("id") != owner_id:
        die("refusing: the token's repository is not owned by the installation's account")
    try:
        exp = dt.datetime.strptime(ans["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        die("the answer has no readable expiry")
    if exp - dt.datetime.now(dt.timezone.utc).timestamp() > MAX_TOKEN_SECONDS:
        die("refusing: the token lives longer than 61 minutes")
    check_permissions(ans.get("permissions"), role)
    MINTED.clear()
    return token


def main(argv):
    role = "worker"
    if len(argv) == 3 and argv[1] == "--role":
        role, argv = argv[2], argv[:1]
    if len(argv) != 1 or not valid_target(argv[0]):
        die("usage: app_token.py OWNER/REPO [--role ROLE]", 2)
    if role not in PERMISSIONS_BY_ROLE:
        die("unknown role (one of: %s)" % ", ".join(PERMISSIONS_BY_ROLE), 2)
    sys.stdout.write(mint(argv[0], role))
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
