#!/usr/bin/env python3
"""app-token: mint a one-hour GitHub App installation token for ONE repository and write it to the route the caller names, and nowhere else.

usage: app_token.py OWNER/REPO [--role worker|orchestrator] --fd N       (programs: a pipe descriptor the caller opened and passed down; N is 3 or more, open, writable, not a terminal)
       app_token.py OWNER/REPO --out PATH   (a person: a new file, mode 0600, in a private directory you own; refused when PATH exists)
       app_token.py OWNER/REPO              (no route: the token is minted, never shown, revoked, and only its byte count is said on stderr)

The token is never written to standard output or standard error. Why: standard output is the channel that terminals, tool transcripts
and logs capture by default, so a slip in a redirect or a debugging run showed a one-hour token (twice). Errors go to stderr, never a value.

The App id and private key are read from the vault by commands (stdout of each, in memory only):
  CW_APP_ID_CMD, CW_APP_KEY_CMD
  (a command, or the vault default; CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME have no defaults)
  default: az keyvault secret show --subscription $CW_VAULT_SUBSCRIPTION --vault-name $CW_VAULT_NAME --name cw-app-id
           | cw-app-key --query value -o tsv   (the subscription and vault come from the env)
The installation is NOT read from the vault: GET /repos/OWNER/REPO/installation (with the App JWT) names the installation that
covers the repository, and its account must be the repository owner. CW_APP_INSTALLATION_CMD and a stored installation id are obsolete:
accepted, never run, ignored.
The key goes to `openssl dgst -sha256 -sign /dev/fd/N` through a pipe: never a file, an argument or the environment.
The 9-minute app JWT and the token are never printed. A token that could not be delivered to its route is revoked.
Only https://api.github.com is used; CW_GITHUB_API_BASE may name a loopback http URL (tests).
The request asks for, and the answer must carry EXACTLY, the permissions of the role's set in PERMISSIONS_BY_ROLE below (default worker) (no extra key, no
higher level, none missing), else no token is delivered. A missing key is the App not yet granted (or the installation not yet
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


class Interrupted(SystemExit):
    """The exit a SIGTERM or SIGHUP makes (128 + the signal number): an interrupt, unlike the exit a refusal makes."""


INTERRUPTS = (KeyboardInterrupt, Interrupted)


def revoke(token):
    """Revoke an installation token. An interrupt during the request: one more request, then the interrupt is raised again (the
    run ends interrupted, never swallowed); a revoke that still did not complete says its one line first. True only on HTTP 204."""
    interrupt, ok = None, False
    for _ in range(2):
        try:
            ok = revoke_request(token)
            break
        except INTERRUPTS as e:
            interrupt = interrupt or e
    if interrupt is None:
        return ok
    if not ok:
        say_revoke_failed()
    raise interrupt


def revoke_request(token):
    """Best effort: one DELETE /installation/token, authorised by that token. True only on HTTP 204. An interrupt passes through."""
    REVOKE_STATUS[:] = ["unreachable"]
    try:
        req = urllib.request.Request(api_base() + "/installation/token", method="DELETE",
                                     headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "cw-app-token"})
        with open_url(req, 15) as r:
            REVOKE_STATUS[:] = [str(r.status)]
            return r.status == 204
    except urllib.error.HTTPError as e:
        REVOKE_STATUS[:] = [str(e.code)]
        return False
    except INTERRUPTS:
        raise
    except BaseException:  # any failure (including a redirect or an exit) is "revocation failed", never a crash and never the token
        return False


LABELS = ("vault firewall", "vault auth", "vault other", "App key", "installation", "permissions", "GitHub error", "other")
REFUSAL = {}  # the last refusal: {"label": one of LABELS, "line": the reason}; the preflight reads it after it catches the exit


REVOKE_STATUS = ["unreachable"]  # what the last revoke met: an HTTP status, or the word unreachable


def say_revoke_failed():
    """The one line for a revoke that did not succeed: the status or the word unreachable, never the token."""
    print("app-token: the token could not be revoked (status %s)" % REVOKE_STATUS[0], file=sys.stderr)



LAST_FAILURE = {}  # the last die(): {'transient': bool, 'reason': text, 'retry_after': int or None}; a caller that retries reads it


def _wait_seconds(value):
    """A Retry-After header as whole seconds, or None when it is not a plain number."""
    if not (isinstance(value, str) and value.isascii() and value.isdigit()):
        return None   # an HTTP date, a sign, a float, a non-ASCII digit (int() would raise on it): not a number
    return int(value) if len(value) <= 6 else 999999   # a huge wait is over the cap: the retries end at once


EXIT_TEMPFAIL = 75   # sysexits "temporary failure": the process status of a passing failure (a launch retries on this status only)


def die(msg, label=None, code=1, transient=False, retry_after=None):
    """Print the reason and exit. transient=True marks a PASSING failure (the vault, the network, HTTP 5xx or 429, a 403 with Retry-After);
    everything else is a refusal. LAST_FAILURE tells the caller which."""
    if isinstance(label, int):   # die(msg, 2): the code, as before the label
        label, code = None, label
    REFUSAL.clear()
    REFUSAL.update(label=label if label in LABELS else "other", line=msg)
    failed = False
    interrupt = None
    if MINTED:
        token = MINTED.pop()
        try:
            ok = revoke(token)
        except INTERRUPTS as e:   # interrupted during the revoke: it was made once more, and said its line if it failed
            interrupt, ok = e, REVOKE_STATUS[0] == "204"
        failed = not ok
        if interrupt is not None:
            failed = False
        msg += (" The token was revoked." if ok
                else " Revocation failed: the token expires within an hour and was never printed.")
        del token
    LAST_FAILURE.clear()
    LAST_FAILURE.update({"transient": bool(transient), "reason": msg, "retry_after": retry_after})
    print("app-token: " + msg, file=sys.stderr)
    if failed:
        say_revoke_failed()
    if interrupt is not None:   # the interrupt ends the run with its own status
        code = 130 if isinstance(interrupt, KeyboardInterrupt) else interrupt.code
    sys.exit(EXIT_TEMPFAIL if transient and code == 1 else code)


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def vault_label(said):
    """The label of a failed vault command, chosen from its standard error (read here, never printed or kept): the
    exact forbidden-client-address pair of phrases is the vault firewall; a sign-in failure is vault auth."""
    low = str(said or "")[:20000].lower()
    if "forbidden" in low and "client address is not authorized" in low:
        return "vault firewall"
    if "az login" in low or "aadsts" in low:
        return "vault auth"
    return "vault other"


def _vault_reason(exc, secret):
    """The short, scrubbed reason the vault command did not run: a timeout names the 60 s, an OSError its own reason."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "the key vault did not answer within 60 s"
    why = re.sub(r"[^A-Za-z0-9 .,:_-]", "?", str(getattr(exc, "strerror", None) or type(exc).__name__))[:60]
    return "the key vault could not be reached: %s" % why


def vault(env_name, secret):
    cmd = os.environ.get(env_name)
    directory = (os.environ.get("CW_SECRET_DIR") or "").strip()
    if directory and not cmd:   # the secret-file route (the command settings win): no az, the value in memory only
        secret_files = __import__("secret_files")   # here, by name: a copy of this file without the module still mints when the setting is unset
        try:
            return secret_files.read(directory, secret, MAX_BYTES, trim=secret != "cw-app-key")
        except secret_files.SecretError as err:
            die(err.line, "vault other")  # the one kind a SecretError carries
    if cmd:
        argv = ["sh", "-c", cmd]
    else:
        sub, vlt = os.environ.get("CW_VAULT_SUBSCRIPTION"), os.environ.get("CW_VAULT_NAME")
        if not sub or not vlt:
            die("set CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME (or %s)" % env_name, "vault other", 2)
        argv = ["az", "keyvault", "secret", "show", "--subscription", sub, "--vault-name", vlt,
                "--name", secret, "--query", "value", "-o", "tsv"]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if len(p.stdout) > MAX_BYTES:
            die("the vault command for %s printed more than %d bytes" % (secret, MAX_BYTES), "vault other")
    except (OSError, subprocess.TimeoutExpired):
        die(_vault_reason(sys.exc_info()[1], secret), "vault other", transient=True)
    val = p.stdout.strip() if secret != "cw-app-key" else p.stdout
    if p.returncode != 0 or not val.strip():
        label = vault_label(p.stderr)  # the vault's own words are read here only to choose the label, then dropped
        del p
        if label == "vault firewall":
            die("the vault refused this machine's address for %s: the vault owner adds it" % secret, "vault firewall", transient=True)
        if label == "vault auth":
            die("the vault command for %s was refused for lack of a sign-in: sign in to the vault" % secret, "vault auth", transient=True)
        die("the vault command for %s failed or printed nothing" % secret, "vault other", transient=True)
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
        die("could not run openssl", "App key")
    finally:
        os.close(r)
        t.join()
    if p.returncode != 0 or not p.stdout:
        die("openssl could not sign (is the key a valid RSA private key?)", "App key")
    return p.stdout


def api_base():
    base = os.environ.get("CW_GITHUB_API_BASE")
    if not base:
        return API
    u = urllib.parse.urlparse(base)
    if u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1"):
        die("CW_GITHUB_API_BASE is only allowed for a loopback http address", "other")
    return base.rstrip("/")


def check_permissions(perms, role="worker"):
    """Refuse unless perms is exactly the role's set. Names a key, never a value."""
    EXPECTED_PERMISSIONS = PERMISSIONS_BY_ROLE[role]  # local: the set of this role (the worker's by default)
    if not isinstance(perms, dict):
        die("refusing: the answer carries no permissions object", "permissions")
    for k in sorted(perms):
        if k not in EXPECTED_PERMISSIONS:
            die("refusing: the token carries an extra permission: %s" % str(k)[:40], "permissions")
    for k, want in EXPECTED_PERMISSIONS.items():
        if k not in perms:
            die("refusing: the token lacks the permission: %s (%s)" % (k, owner_step(role)), "permissions")
        if perms[k] != want:
            die("refusing: the token has a different level for the permission: %s" % k, "permissions")


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
            die("the GitHub answer is larger than %d bytes" % MAX_BYTES, "GitHub error")
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404 and not_found:
            die(not_found, "installation")
        if e.code == 422 and method == "POST":
            die("the App's installation does not cover this repository: add it to the installation (HTTP 422); "
                "if the App lacks a requested permission, %s" % owner_step(role), "installation")
        retry = e.headers.get("Retry-After") if e.headers else None
        passing = e.code == 429 or 500 <= e.code <= 599 or (e.code == 403 and retry is not None)
        flags = {"transient": passing, "retry_after": _wait_seconds(retry) if passing else None}
        if e.code == 403 and method == "POST":  # the App may not hold, or the installation not yet accept, a requested permission
            die("%s; if the App lacks a requested permission, %s" % (http_error_text(e.code, retry), owner_step(role)), "permissions", **flags)
        die(http_error_text(e.code, retry), "GitHub error", **flags)
    except TimeoutError:
        die("the GitHub request timed out: the launch is refused and nothing was minted", "GitHub error", transient=True)
    except Exception:  # OSError, ValueError, UnicodeDecodeError, RecursionError, MemoryError: any failure is a named refusal, never a crash
        die("the GitHub request failed or its answer was not usable JSON: the launch is refused and nothing was minted", "GitHub error", transient=True)


def is_id(x):
    """A GitHub numeric id: an exact integer above zero (a bool, string or float is not one)."""
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def find_installation(target, jwt):
    """The id of the App installation that covers target (OWNER/REPO), asked of GitHub with the App JWT. Checked, never trusted."""
    owner = target.split("/", 1)[0]
    ans = github_json("GET", "/repos/%s/installation" % target, jwt,
                      not_found="the App is not installed on %s: install it on the account that owns the repository and add the repository" % target)
    if not isinstance(ans, dict):
        die("refusing: the installation lookup answered with something other than one installation", "installation")
    inst = ans.get("id")
    if not is_id(inst):
        die("refusing: the installation lookup carries no installation id", "installation")
    account = ans.get("account")
    login = account.get("login") if isinstance(account, dict) else None
    if not valid_owner(login) or login.lower() != owner.lower():  # ASCII only: lower() of raw non-ASCII folds Kelvin to k
        die("refusing: the installation found is not the account that owns %s" % target, "installation")
    if ans.get("suspended_at"):
        die("refusing: the App's installation on %s is suspended" % owner, "installation")
    owner_id = account.get("id")
    if not is_id(owner_id):
        die("refusing: the installation lookup carries no usable account id", "installation")
    return inst, owner_id


def mint(target, role="worker", keep=False):
    """Return a token for target (OWNER/REPO), or exit through die(). Never prints. With keep the token stays in MINTED (so a failure
    after the return still revokes it) and the one who asked clears it."""
    if os.environ.get("CW_TOKEN_SERVICE_URL"):  # the service route (ask #2002); unset, the signing route below is unchanged
        return mint_from_service(target, role, keep)
    repo = target.split("/", 1)[1]
    app_id = vault("CW_APP_ID_CMD", "cw-app-id").strip()
    if not app_id.isdigit():
        die("the app id must be a number", "App key")
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
    if isinstance(token, str) and token:
        # kept the moment it is known: the caller never holds a token that is not in the list
        MINTED.append(token)
    repos = ans.get("repositories") if isinstance(ans, dict) else None
    if not isinstance(token, str) or not token:
        die("the answer has no token", "GitHub error")
    if not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict) or repos[0].get("name") != repo:
        die("refusing: the token does not cover exactly the one target repository", "installation")
    full = repos[0].get("full_name")
    if not (isinstance(full, str) and valid_target(full) and full.lower() == target.lower()):
        die("refusing: the token does not cover exactly the one target repository", "installation")
    owner = repos[0].get("owner")  # the account id is stable where the login is not: both answers must carry it, and it must agree
    if not isinstance(owner, dict) or not is_id(owner.get("id")):
        die("refusing: the token's repository carries no usable owner id", "installation")
    if owner.get("id") != owner_id:
        die("refusing: the token's repository is not owned by the installation's account", "installation")
    try:
        exp = dt.datetime.strptime(ans["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        die("the answer has no readable expiry", "GitHub error")
    if exp - dt.datetime.now(dt.timezone.utc).timestamp() > MAX_TOKEN_SECONDS:
        die("refusing: the token lives longer than 61 minutes", "GitHub error")
    check_permissions(ans.get("permissions"), role)
    if not keep:
        MINTED.clear()
    return token


def caller_token():
    """The cluster token the service reviews: CW_TOKEN_SERVICE_CALLER_TOKEN_CMD prints one, else the launcher's own cluster login. Never printed."""
    cmd = (os.environ.get("CW_TOKEN_SERVICE_CALLER_TOKEN_CMD") or "").strip()
    try:
        if cmd:
            p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True, timeout=60)
            tok = p.stdout.strip() if p.returncode == 0 and len(p.stdout) <= MAX_BYTES else ""
        else:
            import cloud_worker  # no cycle: cloud_worker does not import this file; imported only on this route
            tok = cloud_worker.cluster_token(dict(os.environ), "10m")
    except Exception:  # a failed login of any kind is a passing failure, never a value
        tok = ""
    if not tok or re.search(r"\s", tok):
        die("the caller's cluster token could not be made", "other", transient=True)
    return tok


def mint_from_service(target, role, keep=False):
    """The token of the service named by CW_TOKEN_SERVICE_URL (POST {repo, role}, Authorization: Bearer <cluster token>). The answer gets the
    same checks as the signing route's. A passing failure exits 75, a refusal 1."""
    url = os.environ["CW_TOKEN_SERVICE_URL"].strip()
    u = urllib.parse.urlparse(url)
    if u.scheme == "http" and u.hostname in ("127.0.0.1", "localhost", "::1"):
        pass  # a loopback address: for tests only
    else:
        server = urllib.parse.urlparse((os.environ.get("CW_CLOUD_SERVER") or "").strip())
        exact = "https://%s/api/v1/namespaces/%s/services/token-mint:8080/proxy/mint" % (server.netloc, os.environ.get("CW_CLOUD_NAMESPACE") or "cw")
        if not server.netloc or url != exact:
            die("CW_TOKEN_SERVICE_URL must be exactly the cluster's service proxy of the token service", "other")
    repo = target.split("/", 1)[1]
    bearer = caller_token()
    req = urllib.request.Request(url, data=json.dumps({"repo": target, "role": role}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + bearer, "X-Caller-Token": "Bearer " + bearer,
                                          "Content-Type": "application/json", "Accept": "application/json"})
    del bearer
    handlers = [token_mint._NoRedirect]
    cafile = (os.environ.get("CW_CLOUD_CA_FILE") or "").strip()
    if cafile:
        import ssl
        handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cafile)))
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=60) as r:
            raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            die("the token service's answer is larger than %d bytes" % MAX_BYTES, "GitHub error")
        ans = json.loads(raw)
    except urllib.error.HTTPError as e:
        passing = e.code == 429 or 500 <= e.code <= 599
        die("the token service refused the request (HTTP %d)" % e.code, "GitHub error", transient=passing)
    except ValueError:  # status 200 with a body that is not JSON: not retried
        die("the token service's answer was not JSON", "GitHub error")
    except Exception:  # a timeout, a refused connection, a failed TLS check: the service could not be asked
        die("the token service could not be asked", "GitHub error", transient=True)
    token = ans.get("token") if isinstance(ans, dict) else None
    repos = ans.get("repositories") if isinstance(ans, dict) else None
    if not isinstance(token, str) or not token:
        die("the answer has no token", "GitHub error")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
        die("refusing: the token holds a character that a token does not", "GitHub error")
    MINTED.append(token)
    if not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict) or repos[0].get("name") != repo:
        die("refusing: the token does not cover exactly the one target repository", "installation")
    full = repos[0].get("full_name")
    if not (isinstance(full, str) and full.lower() == target.lower()):
        die("refusing: the token does not cover exactly the one target repository", "installation")
    try:
        exp = dt.datetime.strptime(ans["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        die("the answer has no readable expiry", "GitHub error")
    if exp - dt.datetime.now(dt.timezone.utc).timestamp() > MAX_TOKEN_SECONDS:
        die("refusing: the token lives longer than 61 minutes", "GitHub error")
    check_permissions(ans.get("permissions"), role)
    if not keep:
        MINTED.clear()
    return token


USAGE = "usage: app_token.py OWNER/REPO [--role worker|orchestrator] [--fd N | --out PATH]"


def parse_route(rest):
    """(kind, value) with kind in None, 'fd', 'out'; any other shape is exit 2 and nothing is minted."""
    if not rest:
        return None, None
    if len(rest) != 2 or rest[0] not in ("--fd", "--out"):
        die(USAGE, "other", 2)
    if rest[0] == "--fd":
        if not (rest[1].isascii() and rest[1].isdigit() and len(rest[1]) <= 6):
            die(USAGE, "other", 2)
        return "fd", int(rest[1])
    return "out", rest[1]


def check_fd(fd):
    """Refuse (exit 2) before minting unless fd is 3 or more, open, writable and not a terminal."""
    import fcntl
    if fd < 3:
        die("the route --fd must be 3 or more (0, 1 and 2 are the terminal's)", "other", 2)
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        tty = os.isatty(fd)
    except OSError:
        die("the route --fd %d is not an open descriptor" % fd, "other", 2)
    if flags & os.O_ACCMODE == os.O_RDONLY:
        die("the route --fd %d is not writable" % fd, "other", 2)
    if tty:
        die("the route --fd %d is a terminal" % fd, "other", 2)
    if same_stream_as_output(fd):
        die("the route --fd %d is the same open file or pipe as standard output or standard error" % fd, "other", 2)


def same_stream_as_output(fd):
    """True when fd is another descriptor of the same open file or pipe (same device and inode) as descriptor 1 or 2."""
    try:
        mine = os.fstat(fd)
    except OSError:
        return False
    for std in (1, 2):
        try:
            st = os.fstat(std)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == (mine.st_dev, mine.st_ino):
            return True
    return False


def link_is_system_made(link_st, parent_st):
    """True for a link made by the system itself: owned by uid 0, in a directory others cannot write (or one that is sticky)."""
    import stat
    if link_st.st_uid != 0:
        return False
    return not parent_st.st_mode & 0o022 or bool(parent_st.st_mode & stat.S_ISVTX)


def link_in_parents(path):
    """True when a component of the directory chain above path is a link that is not made by the system itself, or is not a directory.
    Each component is judged with lstat from the root down (a link is followed only when it is judged); a missing one is not judged here."""
    import stat
    todo = [p for p in path.split("/") if p not in ("", ".")][:-1]
    cur, budget = "/", 40
    try:
        st_cur = os.lstat("/")
        while todo:
            part = todo.pop(0)
            nxt = os.path.join(cur, part)
            st = os.lstat(nxt)
            if stat.S_ISLNK(st.st_mode):
                budget -= 1
                target = os.readlink(nxt)
                tparts = [p for p in target.split("/") if p not in ("", ".")]
                if budget < 0 or ".." in tparts or not link_is_system_made(st, st_cur):
                    return True
                if target.startswith("/"):
                    cur, st_cur = "/", os.lstat("/")
                todo = tparts + todo
                continue
            if not stat.S_ISDIR(st.st_mode):
                return True
            cur, st_cur = nxt, st
    except OSError:
        return False
    return False


def check_length(path):
    """Refuse (exit 2), before anything is minted, a path over the file system's name or path limit (the smaller of PC_PATH_MAX and 4096)."""
    try:
        name_max = os.pathconf("/", "PC_NAME_MAX")
        path_max = min(os.pathconf("/", "PC_PATH_MAX"), 4096)
    except (OSError, ValueError):
        name_max, path_max = 255, 4096
    raw = os.fsencode(path)
    if len(raw) >= path_max or any(len(p) > name_max for p in raw.split(b"/")):
        die("the route --out is longer than the file system allows", "other", 2)


def check_out(path):
    """Refuse (exit 2) before minting unless path is new, in a private directory this user owns (no group or world access, no link but the system's own).
    Returns the path to create, with every judged link resolved."""
    import stat
    if not path or "\0" in path or "\n" in path:
        die("the route --out needs a path", "other", 2)
    if ".." in path.split("/"):
        die("the route --out must not hold a '..' component", "other", 2)
    if path.endswith("/"):
        die("the route --out must name a file", "other", 2)
    path = os.path.join(os.getcwd(), path)
    check_length(path)
    d = os.path.dirname(os.path.abspath(path))
    if link_in_parents(os.path.join(d, "x")):
        die("the directory of --out is a link or not a directory", "other", 2)
    d = os.path.realpath(d)   # every link in it was judged above
    path = os.path.join(d, os.path.basename(path))
    try:
        st = os.lstat(d)
    except OSError:
        die("the directory of --out does not exist", "other", 2)
    if not stat.S_ISDIR(st.st_mode):
        die("the directory of --out is a link or not a directory", "other", 2)
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        die("the directory of --out must be owned by you with no group or world access (mode 0700)", "other", 2)
    if os.path.lexists(path):
        die("refusing: --out path exists; the mint never overwrites", "other", 1)
    return path


def write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def deliver(kind, value, token):
    """Write exactly the token to the route. Raises OSError when it cannot."""
    data = token.encode()
    if kind == "fd":
        write_all(value, data)
        return
    d, name = os.path.split(value)
    dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)   # the checked directory itself: nothing can redirect the create
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
    except BaseException:
        os.close(dfd)
        raise
    try:
        write_all(fd, data)
        os.fsync(fd)
    except BaseException:   # any error or interrupt: the file this run created is removed
        os.close(fd)
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError:
            pass
        os.close(dfd)
        raise
    os.close(dfd)
    os.close(fd)


def main(argv):
    if len(argv) < 1 or not valid_target(argv[0]):
        die(USAGE, "other", 2)
    target = argv[0]
    role, rest = "worker", argv[1:]
    if len(rest) >= 2 and rest[0] == "--role":
        role, rest = rest[1], rest[2:]
    elif len(rest) >= 2 and rest[-2] == "--role":
        role, rest = rest[-1], rest[:-2]
    if role not in PERMISSIONS_BY_ROLE:
        die("unknown role (one of: %s)" % ", ".join(PERMISSIONS_BY_ROLE), "other", 2)
    kind, value = parse_route(rest)
    if kind == "fd":
        check_fd(value)
    elif kind == "out":
        value = check_out(value)
    import signal

    def stop(signum, frame):
        raise Interrupted(128 + signum)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    try:   # ONE handler over the whole span: from the mint to the end of the delivery
        token = mint(target, role, keep=True)
        note_minted(token)
        if kind is None:
            try:
                revoke(token)  # nobody received it: revoke first, forget the token after the call has ended
            finally:
                MINTED.clear()
            revoked = REVOKE_STATUS[0] == "204"
            print("app-token: minted a token of %d bytes and did not deliver it: name a route with --fd N or --out PATH" % len(token), file=sys.stderr)
            if not revoked:
                say_revoke_failed()
                sys.exit(1)
            return
        deliver(kind, value, token)
    except KeyboardInterrupt:   # an interrupt or a signal at any point: revoke, leave nothing, exit non-zero
        die("the delivery was interrupted", "other", 130)
    except SystemExit as e:
        if not MINTED:   # a refusal already revoked, or nothing was minted yet, or the run ended well
            raise
        die("the delivery was interrupted", "other", e.code if isinstance(e.code, int) and e.code else 1)
    except Exception:   # OSError, a full disk, a broken pipe, an error in the code between the mint and the route
        if not MINTED:   # before any token exists the failure is not this handler's
            raise
        die("the token could not be delivered to the route", "other")
    MINTED.clear()


def note_minted(token):
    """The step right after the mint: nothing to do, the token is already kept in MINTED until its delivery ends."""
    return token


if __name__ == "__main__":
    main(sys.argv[1:])
