#!/usr/bin/env python3
"""app-token: print a one-hour GitHub App installation token for ONE repository, to stdout, and nothing else.

usage: app_token.py OWNER/REPO

The App id, private key and installation id are read from the vault by commands (stdout of each, in memory only):
  CW_APP_ID_CMD, CW_APP_KEY_CMD, CW_APP_INSTALLATION_CMD
  (a command, or the vault default; CW_VAULT_SUBSCRIPTION and CW_VAULT_NAME have no defaults)
  default: az keyvault secret show --subscription $CW_VAULT_SUBSCRIPTION --vault-name $CW_VAULT_NAME --name cw-app-id
           | cw-app-key | cw-app-installation-id --query value -o tsv   (the subscription and vault come from the env)
The key goes to `openssl dgst -sha256 -sign /dev/fd/N` through a pipe: never a file, an argument or the environment.
The 9-minute app JWT and the token are never printed except the token, once, on stdout. Errors go to stderr, never a value.
Only https://api.github.com is used; CW_GITHUB_API_BASE may name a loopback http URL (tests).
The answer must carry EXACTLY the permissions below (no extra key, no higher level, none missing), else no token is printed.
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

API = "https://api.github.com"
MAX_TOKEN_SECONDS = 61 * 60
MAX_BYTES = 1_000_000  # the most of any GitHub answer or vault command output that is read
EXPECTED_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}


def die(msg, code=1):
    print("app-token: " + msg, file=sys.stderr)
    sys.exit(code)


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
        die("could not run the command for %s" % secret)
    val = p.stdout.strip() if secret != "cw-app-key" else p.stdout
    if p.returncode != 0 or not val.strip():
        die("the vault command for %s failed or printed nothing" % secret)
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


def check_permissions(perms):
    """Refuse unless perms is exactly EXPECTED_PERMISSIONS. Names a key, never a value."""
    if not isinstance(perms, dict):
        die("refusing: the answer carries no permissions object")
    for k in sorted(perms):
        if k not in EXPECTED_PERMISSIONS:
            die("refusing: the token carries an extra permission: %s" % str(k)[:40])
    for k, want in EXPECTED_PERMISSIONS.items():
        if k not in perms:
            die("refusing: the token lacks the permission: %s" % k)
        if perms[k] != want:
            die("refusing: the token has a different level for the permission: %s" % k)


def mint(target):
    """Return a token for target (OWNER/REPO), or exit through die(). Never prints."""
    repo = target.split("/", 1)[1]
    app_id = vault("CW_APP_ID_CMD", "cw-app-id").strip()
    inst = vault("CW_APP_INSTALLATION_CMD", "cw-app-installation-id").strip()
    if not app_id.isdigit() or not inst.isdigit():
        die("the app id and the installation id must be numbers")
    key = vault("CW_APP_KEY_CMD", "cw-app-key")
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64(json.dumps({"iat": now - 30, "exp": now + 9 * 60, "iss": app_id}, separators=(",", ":")).encode())
    signing = "%s.%s" % (head, claims)
    jwt = signing + "." + b64(sign(key, signing.encode()))
    del key
    req = urllib.request.Request("%s/app/installations/%s/access_tokens" % (api_base(), inst),
                                 data=json.dumps({"repositories": [repo]}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                                          "User-Agent": "cw-app-token"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            die("the GitHub answer is larger than %d bytes" % MAX_BYTES)
        ans = json.loads(raw)
    except urllib.error.HTTPError as e:
        die("GitHub refused the request (HTTP %d)" % e.code)
    except (OSError, ValueError):
        die("the GitHub request failed or its answer was not JSON")
    token = ans.get("token") if isinstance(ans, dict) else None
    repos = ans.get("repositories") if isinstance(ans, dict) else None
    if not isinstance(token, str) or not token:
        die("the answer has no token")
    if not isinstance(repos, list) or len(repos) != 1 or not isinstance(repos[0], dict) or repos[0].get("name") != repo:
        die("refusing: the token does not cover exactly the one target repository")
    full = repos[0].get("full_name")
    if full is not None and full.lower() != target.lower():
        die("refusing: the token does not cover exactly the one target repository")
    try:
        exp = dt.datetime.strptime(ans["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
    except (KeyError, ValueError, TypeError):
        die("the answer has no readable expiry")
    if exp - dt.datetime.now(dt.timezone.utc).timestamp() > MAX_TOKEN_SECONDS:
        die("refusing: the token lives longer than 61 minutes")
    check_permissions(ans.get("permissions"))
    return token


def main(argv):
    if len(argv) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", argv[0]):
        die("usage: app_token.py OWNER/REPO", 2)
    sys.stdout.write(mint(argv[0]))
    sys.stdout.flush()


if __name__ == "__main__":
    main(sys.argv[1:])
