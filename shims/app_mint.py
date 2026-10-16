"""A short-lived, per-repository GitHub App installation token for the fleet's shims (gh, and git through gh's credential route).

For a repository the owner maps to an App (the switchboard config's `app_installations`), the shim authenticates every call with a token minted HERE, from
the App's private key, instead of the owner's stored login: an installation token lives one hour at most, is scoped to that one repository and carries only
the permissions the config allows (never Administration), so an agent holding it cannot change settings, rulesets, branch protection or collaborators.

Configuration (the switchboard config, all neutral names):

    "app_installations": {"acme-org/widget-api": {"app_id": 123456, "installation_id": 7654321}},   # installation_id is optional (looked up, then)
    "app_key_path": "/path/to/app-key.pem"        # OR "app_key_command": ["vault-tool", "read", "app-key"]   (exactly one; a command prints the PEM, no shell)
    "app_permissions": {"contents": "write", "pull_requests": "write", "issues": "write", "checks": "read", "metadata": "read"}   # the default
    "app_api_base": "https://api.github.com"      # http only for 127.0.0.1 / localhost (the tests' fake endpoint)

Rules that never bend: fail CLOSED (any doubt is a refusal, and a failed mint never falls back to the stored login); the key, the signed JWT and the token are never
printed, never put in an argument or in a child's environment (the key reaches openssl through an inherited pipe; the token reaches the real gh as a private
`hosts.yml` the caller removes after the call); a token whose permissions go beyond the configured set (Administration above all) is refused; FleetKit's own repository
(any repository named `fleet-kit`) can never be mapped; repositories are matched with the shared matcher (repo_match.py beside this file).
Standard library only, plus the system openssl at a fixed absolute path (never looked up on PATH); loaded by path, never imported as a package.
"""
import base64
import calendar
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write", "checks": "read", "metadata": "read"}
LEVELS = {"read": 1, "write": 2}
OPENSSL_CANDIDATES = ("/usr/bin/openssl", "/opt/homebrew/bin/openssl", "/usr/local/bin/openssl")
DEFAULT_BASE = "https://api.github.com"
MARGIN_SECONDS = 300          # a cached token with less than this left is not used
LIFETIME_CAP = 3600           # a token is never trusted for longer than an hour, whatever the endpoint said
HTTP_SECONDS = 10.0
KEY_COMMAND_SECONDS = 15.0
KEY_MAX_BYTES = 16384
OWN_REPO_NAME = "fleet-kit"   # FleetKit's own repository stays with local workers: never mappable, under any owner
TOKEN_RE = re.compile(r"[A-Za-z0-9_]{8,255}")
PERMISSION_RE = re.compile(r"[a-z][a-z_]{0,63}")
TOKEN_ENV_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


class ConfigError(Exception):
    """The App configuration is present but cannot be trusted: every network call is refused until the owner fixes it."""


class MintRefused(Exception):
    """A token could not be had (or was not acceptable): the call is refused. The text is fixed wording, never a response body, key or token."""


def _matcher():
    path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "repo_match.py")
    spec = importlib.util.spec_from_file_location("repo_match_for_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "fleet-switchboard", "config.json")


def state_dir():
    return os.environ.get("FLEET_SWITCHBOARD_STATE") or os.path.join(
        os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state"), "fleet-switchboard")


def check_permissions(perms, what):
    """ConfigError unless `perms` is a non-empty map of permission key -> read|write that never names an Administration permission."""
    if not isinstance(perms, dict) or not perms:
        raise ConfigError("%s must be a non-empty map of permission to read|write" % what)
    for key, level in perms.items():
        if not isinstance(key, str) or not PERMISSION_RE.fullmatch(key) or "administration" in key:
            raise ConfigError("%s names a permission that is not allowed: %r" % (what, str(key)[:40]))
        if level not in LEVELS:
            raise ConfigError("%s: %s must be read or write" % (what, key))


def load_config(raw):
    """A Config for the parsed switchboard config `raw`, or None when no repository is mapped (the feature is off). Raises ConfigError for anything off:
    an unreadable entry, FleetKit's own repository, a wildcard, a missing or doubled key source, a permission that is not allowed."""
    if not isinstance(raw, dict) or "app_installations" not in raw:
        return None
    mapped = raw["app_installations"]
    if mapped is None or mapped == {}:
        return None
    if not isinstance(mapped, dict):
        raise ConfigError("app_installations must be a map of repository to {app_id, installation_id}")
    matcher = _matcher()
    table = {}
    for name, entry in mapped.items():
        repo = matcher.normal_repo(name) if isinstance(name, str) and not name.strip().endswith("*") else None
        if repo is None:
            raise ConfigError("app_installations: %r is not an exact owner/name repository" % str(name)[:60])
        if repo.split("/")[1] == OWN_REPO_NAME:
            raise ConfigError("app_installations: %s is FleetKit's own repository and can never be mapped" % repo)
        if repo in table:
            raise ConfigError("app_installations names %s twice" % repo)
        if not isinstance(entry, dict):
            raise ConfigError("app_installations: %s needs {app_id, installation_id}" % repo)
        app_id, install = entry.get("app_id"), entry.get("installation_id")
        for label, value in (("app_id", app_id), ("installation_id", install)):
            if value is None and label == "installation_id":
                continue
            if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.isascii() and value.isdigit())) or int(value) < 1:
                raise ConfigError("app_installations: %s needs a positive number for %s" % (repo, label))
        table[repo] = (str(int(app_id)), None if install is None else int(install))
    path, command = raw.get("app_key_path"), raw.get("app_key_command")
    if (path is None) == (command is None):
        raise ConfigError("exactly one of app_key_path and app_key_command must be set")
    if path is not None and not (isinstance(path, str) and os.path.isabs(path)):
        raise ConfigError("app_key_path must be an absolute path")
    if command is not None and not (isinstance(command, list) and command and all(isinstance(a, str) and a for a in command)):
        raise ConfigError("app_key_command must be a non-empty list of words (no shell)")
    perms = raw.get("app_permissions", dict(DEFAULT_PERMISSIONS))
    check_permissions(perms, "app_permissions")
    base = raw.get("app_api_base", DEFAULT_BASE)
    parsed = urllib.parse.urlsplit(base) if isinstance(base, str) else None
    if parsed is None or not ((base == DEFAULT_BASE) or (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port)):
        raise ConfigError("app_api_base must be %s (or http://127.0.0.1:PORT for a test endpoint)" % DEFAULT_BASE)
    return Config(table, path, command, dict(perms), base.rstrip("/"))


def read_config():
    """(Config or None). A missing file means the feature is off. A file that cannot be read or parsed raises ConfigError only if its text mentions
    `app_installations` (otherwise it is not known to name an App mapping, as every other shim key behaves)."""
    try:
        with open(config_path()) as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError:
        raise ConfigError("the switchboard config could not be read")
    if "app_installations" not in text:
        return None
    try:
        return load_config(json.loads(text))
    except ValueError:
        raise ConfigError("the switchboard config is not valid JSON")


class Config:
    def __init__(self, table, key_path, key_command, permissions, base):
        self.table, self.key_path, self.key_command, self.permissions, self.base = table, key_path, key_command, permissions, base

    def entry(self, repo):
        """(normalised repo, (app_id, installation_id)) when `repo` is mapped, else None. The normalised name is the ONLY name that ever reaches the endpoint."""
        want = _matcher().normal_repo(repo)
        return (want, self.table[want]) if want in self.table else None


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def find_openssl():
    for candidate in OPENSSL_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise MintRefused("openssl was not found at a fixed system path")


def clean_env():
    return {k: v for k, v in os.environ.items() if k not in TOKEN_ENV_NAMES}


def read_key(config):
    """The PEM bytes of the App key, or MintRefused. Never logged. A key FILE that group or others can read is refused (the owner-only mode is the minimum)."""
    try:
        if config.key_path is not None:
            fd = os.open(config.key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                if os.fstat(handle.fileno()).st_mode & 0o077:
                    raise MintRefused("the key file is readable by others")
                pem = handle.read(KEY_MAX_BYTES + 1)
        else:
            done = subprocess.run(config.key_command, capture_output=True, timeout=KEY_COMMAND_SECONDS, env=clean_env(), stdin=subprocess.DEVNULL)
            if done.returncode != 0:
                raise MintRefused("the key command failed")
            pem = done.stdout[:KEY_MAX_BYTES + 1]
    except MintRefused:
        raise
    except Exception:
        raise MintRefused("the key could not be read")
    if len(pem) > KEY_MAX_BYTES or b"-----BEGIN" not in pem:
        raise MintRefused("the key could not be read")
    return pem


def sign(data, pem):
    """RS256 signature of `data` with the system openssl. The key goes in through an inherited pipe (`/dev/fd/N`), the data on stdin: neither is in argv or the environment."""
    openssl = find_openssl()
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, pem)       # a key is well under the pipe's capacity
        os.close(write_end)
        write_end = None
        done = subprocess.run([openssl, "dgst", "-sha256", "-sign", "/dev/fd/%d" % read_end], input=data, capture_output=True, env={},
                              pass_fds=(read_end,), timeout=HTTP_SECONDS)
    except Exception:
        raise MintRefused("the App JWT could not be signed")
    finally:
        os.close(read_end)
        if write_end is not None:
            os.close(write_end)
    if done.returncode != 0 or not done.stdout:
        raise MintRefused("the App JWT could not be signed")
    return done.stdout


def make_jwt(app_id, pem, now):
    head = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = b64url(json.dumps({"iat": int(now) - 60, "exp": int(now) + 540, "iss": app_id}, separators=(",", ":")).encode())
    signing_input = ("%s.%s" % (head, body)).encode("ascii")
    return "%s.%s" % (signing_input.decode("ascii"), b64url(sign(signing_input, pem)))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None   # the JWT is never forwarded to another address


def http_seconds():
    """The mint's network deadline: 10 seconds, FLEET_APP_MINT_SECONDS may only shorten it (between 0.2 and 10)."""
    try:
        return min(HTTP_SECONDS, max(0.2, float(os.environ.get("FLEET_APP_MINT_SECONDS", HTTP_SECONDS))))
    except ValueError:
        return HTTP_SECONDS


def api(config, method, path, jwt, payload=None):
    """(status, parsed JSON or None). Network and HTTP errors are a status of 0 or the code; no body or URL is ever reported."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(config.base + path, data=data, method=method, headers={
        "Authorization": "Bearer " + jwt, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "fleet-kit-shim",
        "Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=http_seconds()) as answer:
            body = answer.read(1 << 20)
            status = answer.status
    except urllib.error.HTTPError as err:
        return err.code, None
    except Exception:
        return 0, None
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    return status, parsed


def parse_expiry(text):
    try:
        return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def validate_answer(answer, repo, config, now):
    """(token, expiry) of an access-token answer, or MintRefused: a token outside the configured permission set (Administration above all), for a different repository
    or with no sound expiry is never used."""
    if not isinstance(answer, dict):
        raise MintRefused("the mint answer could not be read")
    token, perms, expiry = answer.get("token"), answer.get("permissions"), parse_expiry(answer.get("expires_at"))
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token) or expiry is None:
        raise MintRefused("the mint answer could not be read")
    if not isinstance(perms, dict) or not perms:
        raise MintRefused("the mint answer does not state the token's permissions")
    for key, level in perms.items():
        if key not in config.permissions or level not in LEVELS or LEVELS[level] > LEVELS[config.permissions[key]]:
            raise MintRefused("the token carries a permission beyond the configured set (%s)" % re.sub(r"[^a-z_]", "?", str(key))[:40])
    if answer.get("repository_selection") not in (None, "selected"):
        raise MintRefused("the token is not limited to selected repositories")
    named = answer.get("repositories")
    if named is not None and (not isinstance(named, list) or [str(r.get("full_name", "")).lower() if isinstance(r, dict) else "" for r in named] != [repo]):
        raise MintRefused("the token is not limited to the one repository")
    if expiry <= now + MARGIN_SECONDS:
        raise MintRefused("the token is already (nearly) expired")
    return token, min(expiry, int(now) + LIFETIME_CAP)


def mint(config, repo, now):
    """A fresh (token, expiry) for the normalised `repo`, from the App key. Raises MintRefused."""
    app_id, install = config.table[repo]
    jwt = make_jwt(app_id, read_key(config), now)
    owner, name = repo.split("/")
    if install is None:
        status, found = api(config, "GET", "/repos/%s/%s/installation" % (owner, name), jwt)
        if status == 404:
            raise MintRefused("the App is not installed on %s" % repo)
        if status != 200 or not isinstance(found, dict) or not isinstance(found.get("id"), int) or found["id"] < 1:
            raise MintRefused("the installation of %s could not be looked up" % repo)
        install = found["id"]
    status, answer = api(config, "POST", "/app/installations/%d/access_tokens" % install, jwt,
                         {"repositories": [name], "permissions": config.permissions})
    if status == 404:
        raise MintRefused("the App is not installed on %s" % repo)
    if status not in (200, 201):
        raise MintRefused("the mint answered with an error (%s)" % ("no answer" if status == 0 else status))
    return validate_answer(answer, repo, config, now)


def fingerprint(config, repo):
    app_id, install = config.table[repo]
    text = json.dumps([repo, app_id, install, sorted(config.permissions.items()), config.base])
    return hashlib.sha256(text.encode()).hexdigest()


def private_dir(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not os.path.isdir(path) or os.path.islink(path) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise MintRefused("the token cache directory is not private")
    return path


def cached(path, finger, now):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd) as handle:
            if os.fstat(handle.fileno()).st_uid != os.getuid() or os.fstat(handle.fileno()).st_mode & 0o077:
                return None
            data = json.load(handle)
        token, expiry = data["token"], data["expiry"]
        if data["finger"] == finger and isinstance(token, str) and TOKEN_RE.fullmatch(token) and isinstance(expiry, int) and expiry - now > MARGIN_SECONDS:
            return token, expiry
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def token_for(config, repo, now=None):
    """The installation token for `repo` (a spelling of a mapped repository), from the file cache while it has more than five minutes left, else freshly minted
    and cached (mode 0600 in a private directory). Raises MintRefused; never returns anything but a validated token for the one normalised repository."""
    found = config.entry(repo)
    if found is None:
        raise MintRefused("%s is not mapped to the App" % str(repo)[:80])
    repo = found[0]
    now = time.time() if now is None else now
    directory = private_dir(os.path.join(state_dir(), "app-tokens"))
    finger = fingerprint(config, repo)
    path = os.path.join(directory, hashlib.sha256(repo.encode()).hexdigest()[:32] + ".json")
    have = cached(path, finger, now)
    if have:
        return have[0]
    token, expiry = mint(config, repo, now)
    try:
        handle, temp = tempfile.mkstemp(dir=directory, prefix=".new-")
        with os.fdopen(handle, "w") as out:
            json.dump({"token": token, "expiry": int(expiry), "finger": finger}, out)
        os.replace(temp, path)
    except OSError:
        pass   # an uncached token is still a good one: the next call mints again
    return token


def write_hosts_dir(token):
    """A private directory (mode 0700) holding a gh `hosts.yml` with the token, for the real gh's GH_CONFIG_DIR. The caller removes it with remove_dir() after the call.
    Stale ones (an hour old, left by a killed shim) are swept first."""
    if not TOKEN_RE.fullmatch(token):
        raise MintRefused("the token is not usable")
    root = private_dir(os.path.join(state_dir(), "app-run"))
    cutoff = time.time() - 3600
    for name in os.listdir(root):
        old = os.path.join(root, name)
        try:
            if name.startswith("gh-") and os.lstat(old).st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            pass
    directory = tempfile.mkdtemp(prefix="gh-", dir=root)
    try:
        fd = os.open(os.path.join(directory, "hosts.yml"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            out.write("github.com:\n    oauth_token: %s\n    user: x-access-token\n    git_protocol: https\n" % token)
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise MintRefused("the token file could not be written")
    return directory


def remove_dir(directory):
    shutil.rmtree(directory, ignore_errors=True)


def child_env(directory):
    """The environment for the real gh: the caller's, with every token variable taken out and GH_CONFIG_DIR pointing at `directory`. No token value in it."""
    env = clean_env()
    env["GH_CONFIG_DIR"] = directory
    return env
