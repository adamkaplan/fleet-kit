"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): helper_fakes."""
import ast
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from unittest import mock
from support.paths import REPO
from support.delivery_measure import REPO
from support.installs_and_keys import tree_of
from support.loader import class_source, sb, switchboard_source
from support.scenario_roles import FAKE_GH


class ReaperOnlyFakes:
    """The reaper-only install (ask #982): the parser's own arguments, the fake machine, the fake home."""

    # A file the LAUNCHER's own reader accepts: a digest image, an https server with a DNS host, a GUID subscription.
    CLOUD_ENV_GOOD = ("CW_ALLOWED_REPOS=acme-org/widget-service\nCW_CLOUD_IMAGE=registry.acme.example/widget@sha256:" + "ab" * 32 + "\n"
                      "CW_CLOUD_SERVER=https://api.acme.example:6443\nCW_CLOUD_NAMESPACE=acme-cw\n"
                      "CW_VAULT_NAME=acme-vault\nCW_VAULT_SUBSCRIPTION=00000000-0000-4000-8000-000000000001\n")

    def ensure_cloud_env(self, text=None, mode=0o600):
        """The settings file the reaper-only install requires, as a good one: 0600, the user's own, the cluster login names
        and the vault names (the token file name comes from the environment of the install, set here for the test).
        Written only when there is none yet. Returns its path."""
        path = sb.cloud_settings_path()
        patcher = mock.patch.dict(os.environ, {"CW_CLOUD_TOKEN_FILE": "/home/user/.cw/token"})
        patcher.start()
        self.addCleanup(patcher.stop)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(str(path.parent), 0o700)   # the launcher refuses a directory others can write, whatever the umask of the test
            path.write_text(self.CLOUD_ENV_GOOD if text is None else text)
            os.chmod(str(path), mode)
        return path

    def reaper_only(self, *flags, hook=None):
        """`install --cloud-reaper-only` as the parser gives it. Returns (output, error)."""
        self.ensure_cloud_env()
        out = io.StringIO()
        args = sb.build_parser().parse_args(["install", "--cloud-reaper-only"] + [f for f in flags if f != "--dry-run"] + (
            ["--dry-run"] if "--dry-run" in flags else []))
        installer = sb.Installer(self.machine, self.daemons, time.time, self.PLATFORM, REPO,
                                 dry_run=args.dry_run, out=out, shared_profile=args.shared_profile,
                                 profile=args.profile, gh_user=args.gh_user)
        if hook is not None:
            hook(installer)
        try:
            installer.install_cloud_reaper_only()
        except sb.InstallError as err:
            return out.getvalue(), str(err)
        return out.getvalue(), None

    def run_install(self, *argv):
        """`install` as the CLI runs it: the parser's arguments, the fake machine, the fake daemon. (code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        args = sb.build_parser().parse_args(["install"] + list(argv))
        real = sb.Installer

        def installer(runner, spawn, now, **kwargs):   # the platform and the kit of the test, not the real ones
            return real(runner, spawn, now, self.PLATFORM, REPO, **kwargs)

        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                mock.patch.object(sb, "Installer", installer):
            code = sb.cmd_install(args, self.machine, time.time, self.daemons)
        return code, out.getvalue(), err.getvalue()

    def leaves(self, before):
        """The new paths under the fake home that hold something: the ones no other new path is inside."""
        after = tree_of(self.home)
        fresh = sorted(path for path in after if path not in before)
        found = []
        for path in fresh:
            prefix = path if path.endswith("/") else path + "/"
            if not any(other.startswith(prefix) for other in fresh if other != path):
                found.append(path)
        return found

    def step_names(self):
        """The installer's step methods, from its own code: a new one must be classified."""
        return sorted(set(re.findall(r"def (step_\w+)\(", class_source("Installer"))))

    def installer_functions(self):
        """The module-level functions the installer's steps call, from the code: a new one must be classified."""
        tree = ast.parse(switchboard_source())   # the entry and every part
        module = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        installer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Installer")
        called = {node.func.id for node in ast.walk(installer)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        return sorted(called & module)

    def reaper_path(self):
        if self.PLATFORM == "darwin":
            return self.home / "Library" / "LaunchAgents" / "com.fleet-kit.cloud-reaper.plist"
        return self.home / "xdg-config" / "systemd" / "user" / "fleet-kit-cloud-reaper.service"

    def fresh_state_dir(self):
        """The state directory as a machine that has never run the switchboard has it: not there."""
        shutil.rmtree(str(sb.state_dir_path()), ignore_errors=True)

    def reaper_load_calls(self):
        return [c for c in self.machine.calls if "cloud-reaper" in " ".join(c) and any(word in ("bootstrap", "enable") for word in c)]


class LabelGh:
    """`gh api` over issues with labels, in any number of repos: the reads the daemon makes (a list of the open
    issues with one label) and the writes `decisions` and `labels ensure` make (labels, comments). A runner.
    A write checks its shape, and records the account's token it ran with."""

    def __init__(self, clock):
        self.clock, self.issues, self.labels, self.comments = clock, {}, {}, []
        self.calls, self.envs, self.down, self.fail = [], [], False, set()
        self.fail_writes = set()   # repos whose writes (not reads) fail

    def issue(self, repo, number, title="an issue", labels=(), body=None, pr=False, author=None):
        self.issues[(repo, number)] = {"number": number, "title": title, "state": "open", "labels": list(labels),
                                       "updated_at": sb.iso(self.clock.now() - 60)}
        if body is not None:
            self.issues[(repo, number)]["body"] = body
        if pr:
            self.issues[(repo, number)]["pull_request"] = {"url": "x"}
        if author:
            self.issues[(repo, number)]["user"] = {"login": author}

    def names(self, repo, number):
        return sorted(self.issues[(repo, number)]["labels"])

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(kwargs.get("env"))
        if argv[:2] != [FAKE_GH, "api"]:
            raise AssertionError("fake gh serves `gh api` only, got %r" % argv)
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "gh: network unreachable\n")
        path = argv[2]
        if any(repo in path for repo in self.fail):
            return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 403\n")
        method = argv[argv.index("--method") + 1] if "--method" in argv else "GET"
        if method != "GET" and any(repo in path for repo in self.fail_writes):
            return subprocess.CompletedProcess(argv, 1, "", "gh: HTTP 403\n")
        data = json.loads(kwargs["input"]) if "input" in kwargs else None
        if method != "GET" and "--input" in argv and data is None:
            raise AssertionError("a write passes its body as input")
        missing = subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)\n")
        found = re.match(r"^repos/([^/]+/[^/?]+)/issues\?(.*)$", path)
        if found and method == "GET":
            query = dict(pair.partition("=")[::2] for pair in found.group(2).split("&"))
            label = urllib.parse.unquote(query["labels"]) if "labels" in query else None
            rows = [dict(i, labels=[{"name": n} for n in i["labels"]]) for (repo, _), i in sorted(self.issues.items())
                    if repo == found.group(1) and (label is None or label in i["labels"]) and i["state"] == "open"]
            page = int(query.get("page", "1"))
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows if page == 1 else []), "")
        found = re.match(r"^repos/([^/]+/[^/]+)/issues$", path)
        if found and method == "POST":
            repo = found.group(1)
            number = max([n for (r, n) in self.issues if r == repo] + [100]) + 1
            self.issue(repo, number, data["title"], data.get("labels", ()), data.get("body"))
            self.issues[(repo, number)]["updated_at"] = sb.iso(self.clock.now())
            return subprocess.CompletedProcess(argv, 0, json.dumps({"number": number, "html_url": "https://github.com/%s/issues/%d" % (repo, number)}), "")
        found = re.match(r"^repos/([^/]+/[^/]+)/issues/(\d+)/(labels|comments)(?:/(.+))?$", path)
        if found:
            key = (found.group(1), int(found.group(2)))
            if key not in self.issues:
                return missing
            issue = self.issues[key]
            if found.group(3) == "labels" and method == "POST":
                issue["labels"] = sorted(set(issue["labels"]) | set(data["labels"]))
            elif found.group(3) == "labels" and method == "DELETE":
                name = urllib.parse.unquote(found.group(4))
                if name not in issue["labels"]:
                    return missing
                issue["labels"].remove(name)
            elif found.group(3) == "comments" and method == "POST":
                self.comments.append({"repo": key[0], "issue": key[1], "body": data["body"]})
            else:
                raise AssertionError("fake gh: no such write %r" % argv)
            issue["updated_at"] = sb.iso(self.clock.now())
            return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}), "")
        found = re.match(r"^repos/([^/]+/[^/]+)/labels(?:/(.+))?$", path)
        if found:
            repo = found.group(1)
            have = self.labels.setdefault(repo, {})
            if method == "GET":
                name = urllib.parse.unquote(found.group(2))
                return subprocess.CompletedProcess(argv, 0, json.dumps({"name": name}), "") if name in have else missing
            if method == "POST" and not found.group(2):
                have[data["name"]] = data
                return subprocess.CompletedProcess(argv, 0, json.dumps(data), "")
        raise AssertionError("fake gh: no route for %r" % (argv,))


# ======================================================================
# #117: the waiting-on-the-person sweep (S1 to S5), synthetic repos only
# ======================================================================
class FakeSweepHub:
    """read_pages / _read_one over a canned repo: `open` is {number: issue or PR record}, `comments` a list of records;
    every call is counted."""

    def __init__(self):
        self.open, self.comments, self.closed, self.reads, self.single = {}, [], set(), [], []
        self.closed_records = {}   # the closed or merged items the closed-state list read returns
        self.caps, self.fresh, self.live = [], [], {}   # live: a fresher copy of an item than `open` shows

    def read_pages(self, path, key=None, timeout=None, older=None, cap=None):
        self.reads.append(path)
        self.caps.append(cap)
        match = re.search(r"/issues/(\d+)/comments", path)
        if match:   # one item's own comments (a fresh re-check)
            number = int(match.group(1))
            return [c for c in self.comments if c["issue_url"].endswith("/%d" % number)][::-1], False
        if "/issues/comments" in path:
            return list(self.comments), False
        if "state=closed" in path:
            return list(self.closed_records.values()), False
        return list(self.open.values()), False

    def _read_one(self, repo, path, now, fresh=False):
        self.single.append((repo, path))
        self.fresh.append(fresh)
        number = int(path.rsplit("/", 1)[1])
        data = dict(self.live.get(number, self.open.get(number, self.closed_records.get(number, {}))))
        data["number"] = number
        data["state"] = "closed" if number in self.closed else "open"
        return data


class MutantSpliceError(Exception):
    """A mutant whose target is not in the function it names: a broken test, never a caught mutant."""
