"""Test classes moved out of bin/test-switchboard (text unchanged): cases_12."""
import argparse
import ast
import traceback
import base64
import calendar
import collections
import contextlib
import errno
import copy
import datetime as _dt
import fcntl
import gzip
import fnmatch
import hashlib
import hmac
import importlib.machinery
import importlib.util
import inspect
import http.client
import io
import itertools
import select
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote
import shlex
import shutil
import textwrap
import zipfile
import signal
import socket
import stat
import tarfile
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
import unittest
import uuid
import urllib.parse
from unittest import mock
from support.core import Base, SAFE_PATH
from support.fake_jev import FAKE_SB_SOURCE, SHIM, SHIM_LOOKUP_SECONDS, write_fake_env
from support.loader import captured, sb, switchboard_source
from support.paths import REPO
from support.rebind import rebound
from support.scenario_roles import FAKE_GH


class WorkflowPlanPins(unittest.TestCase):
    """ask #990: the macOS leg runs where it protects. The workflow's `plan` job is RUN here (its real script, in a throwaway git
    repository), and the structure around it is pinned by its words; every mutant is an edit of a copy of the text in memory."""

    WORKFLOW = REPO / ".github" / "workflows" / "test.yml"
    LIST = REPO / ".github" / "macos-platform-paths.txt"
    REQUIRED = ("test (macos-latest)", "test (ubuntu-latest)")
    GROUPS = ("shims", "plugins", "install-and-service", "heavy-slot", "workflow")

    @staticmethod
    def entries(listtext):
        return [l.strip() for l in listtext.splitlines() if l.strip() and not l.lstrip().startswith("#")]

    @classmethod
    def retypable(cls, listtext):
        """The entries a second copy could duplicate: all but the list file's own name (the workflow must name its file)."""
        return [e for e in cls.entries(listtext) if e != str(cls.LIST.relative_to(REPO))]

    @staticmethod
    def groups(listtext):
        found, current = {}, None
        for line in listtext.splitlines():
            m = re.match(r"#\s*group:\s*(\S+)\s*$", line)
            if m:
                current = m.group(1)
                found[current] = []
            elif line.strip() and not line.lstrip().startswith("#") and current:
                found[current].append(line.strip())
        return found

    @staticmethod
    def touching(entry):
        return entry.replace("**", "sub/file.txt") if "**" in entry else entry

    def plan(self, text, listtext, event, base="", draft=False, files=("docs/other.md",), integration=True, moves=(), head_list=None, unrelated=False):
        """Run the plan job's script from `text` and return the matrix rows it writes."""
        start = text.index("          python3 - <<'PY'\n")
        end = text.index("\n          PY\n", start)
        script = "\n".join(l[10:] for l in text[start:end + len("\n          PY")].splitlines()) + "\n"
        tmp = self.repo_for(listtext, tuple(files), integration, tuple(moves), head_list, unrelated)
        out = Path(tmp) / "out.txt"
        out.write_text("")
        summary = Path(tmp) / "summary.txt"
        summary.write_text("")
        env = dict(os.environ, EVENT=event, BASE=base, DRAFT="true" if draft else "false", GITHUB_OUTPUT=str(out), GITHUB_STEP_SUMMARY=str(summary))
        done = subprocess.run(["bash", "-c", script], cwd=tmp, env=env, capture_output=True, text=True)
        if done.returncode:
            raise AssertionError("the plan script failed: " + done.stderr[-400:])
        self.log, self.summary = done.stdout, summary.read_text()
        line = [l for l in out.read_text().splitlines() if l.startswith("matrix=")][-1]
        return json.loads(line[len("matrix="):])

    _repos = {}

    @classmethod
    def repo_for(cls, listtext, files, integration, moves=(), head_list=None, unrelated=False):
        """A throwaway repository (made once per shape, the script only reads it): a base commit holding the list and, on top, a change that touches `files`."""
        key = (listtext, files, integration, moves, head_list, unrelated)
        if key not in cls._repos:
            tmp = tempfile.mkdtemp(prefix="plan-pin-")
            def git(*args):
                subprocess.run(["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.com"] + list(args), check=True, capture_output=True)
            git("init", "-q", "-b", "main")
            (Path(tmp) / ".github").mkdir()
            if listtext is not None:
                (Path(tmp) / ".github" / "macos-platform-paths.txt").write_text(listtext)
            (Path(tmp) / "keep").write_text("x\n")
            git("add", "-A")
            git("commit", "-q", "-m", "base")
            for name in ("main", "integration") if integration else ("main",):
                git("update-ref", "refs/remotes/origin/" + name, "HEAD")
            for src, dst in moves:   # a file that exists on the base and is moved (same content) to another path
                (Path(tmp) / src).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / src).write_text("a moved file\n" * 20)
            if moves:
                git("add", "-A")
                git("commit", "-q", "-m", "base2")
                for name in ("main", "integration"):
                    git("update-ref", "refs/remotes/origin/" + name, "HEAD")
                for src, dst in moves:
                    (Path(tmp) / dst).parent.mkdir(parents=True, exist_ok=True)
                    git("mv", src, dst)
            if unrelated:   # a head with no common history with the base: the three-dot diff fails while the base list reads fine
                git("checkout", "-q", "--orphan", "pr")
                git("rm", "-rq", "--cached", ".")
                (Path(tmp) / "keep").write_text("y\n")
            if head_list is not None:   # the pull request rewrites the list on its own side
                (Path(tmp) / ".github" / "macos-platform-paths.txt").write_text(head_list)
            for f in files:
                path = Path(tmp) / f
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.read_text() + "# edit\n" if path.exists() else "x\n")
            git("add", "-A")
            git("commit", "-q", "--allow-empty", "-m", "change")
            cls._repos[key] = tmp
        return cls._repos[key]

    @classmethod
    def tearDownClass(cls):
        for tmp in cls._repos.values():
            shutil.rmtree(tmp, ignore_errors=True)
        cls._repos.clear()

    def names(self, rows, key="name"):
        return sorted(r[key] for r in rows)

    def pin(self, text, listtext):
        """The whole pin: raises AssertionError at the first rule the text breaks."""
        def check(ok, message):
            if not ok:
                raise AssertionError(message)
        top, jobs = text.split("\njobs:\n", 1)
        on = top.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        check(re.findall(r"^  ([a-z_]+):", on, re.M) == ["pull_request", "push", "schedule", "workflow_dispatch"], "exactly the 4 triggers")
        check("    types: [opened, synchronize, reopened, ready_for_review]\n" in on, "ready_for_review is among the pull_request types, exactly 4")
        check("    branches: [main]\n" in on, "a push to main")
        check(re.search(r'^    - cron: "\d{1,2} \d{1,2} \* \* \*"$', on, re.M), "a daily schedule")
        check(re.findall(r"uses: (\S+)", text) == ["actions/checkout@v5"] * 3, "the only action is actions/checkout@v5 (no third-party action)")
        check("permissions:\n  contents: read\n\n" in top and top.count("permissions:") == 1, "contents: read only")
        check("secrets." not in text.replace("secrets.PRIVATE_NAMES", ""), "no new secret")
        check("cancel-in-progress: ${{ github.event_name == 'pull_request' && github.event.action != 'reopened' }}" in top, "the concurrency rule: cancel a push run, never a reopened one (#1235)")
        check(re.findall(r"^  ([a-z][a-z-]*):$", jobs, re.M) == ["plan", "test", "test-cloud"], "the jobs")
        check("\n  test:\n    needs: plan\n    name: ${{ matrix.name }}\n" in text, "test is named by the plan's name")
        check("\n  test-cloud:\n    needs: plan\n    name: ${{ matrix.cloud }}\n" in text, "test-cloud is named by the plan's cloud name")
        check(jobs.count("include: ${{ fromJSON(needs.plan.outputs.matrix) }}") == 2, "both take the plan's matrix")
        check(jobs.count("ref: ${{ matrix.ref }}") == 2, "the nightly leg checks out its own ref")
        check("PATH_LIST" not in text and 'LIST = ".github/macos-platform-paths.txt"' in text and "origin/\" + base + \":\" + LIST" in text, "the plan reads the one list file, from the base")
        groups = self.groups(listtext)
        check(sorted(groups) == sorted(self.GROUPS) and all(groups.values()), "the list names all 5 groups")
        for entry in self.retypable(listtext):
            check(entry not in text, "an entry of the list is re-typed in the workflow: " + entry)
        def run(*a, **k):
            return self.plan(text, k.pop("listtext", listtext), *a, **k)
        key = lambda rows: self.names(rows)
        both = list(self.REQUIRED)
        check(key(run("pull_request", "main", files=("shims/x",))) == both, "a platform path in a pull request into main runs macOS (#1235)")
        check(key(run("pull_request", "main", files=("docs/other.md",))) == ["test (ubuntu-latest)"], "a docs-only pull request into main runs ubuntu only (#1235)")
        check(key(run("pull_request", "main", files=("shims/x",), draft=True)) == ["test (ubuntu-latest)"], "a draft into main skips macOS")
        check(key(run("pull_request", "main", files=("shims/x",), draft=False)) == both, "marked ready with a platform path, macOS runs")
        for event in ("push", "workflow_dispatch"):
            check(key(run(event)) == both, event + " runs both")
        check(key(run("pull_request", "integration")) == ["test (ubuntu-latest)"], "a docs-only pull request into integration skips macOS")
        check(key(run("pull_request", "integration", files=("bin/fleet-switchboard",))) == ["test (ubuntu-latest)"], "the monolith is not on the list")
        for entry in self.entries(listtext):
            check(key(run("pull_request", "integration", files=(self.touching(entry),))) == both, "a pull request into integration touching " + entry + " runs macOS")
        first = self.entries(listtext)[0] if self.entries(listtext) else None
        if first:
            src = self.touching(first)
            check(key(run("pull_request", "integration", files=(), moves=((src, "docs/moved.txt"),))) == both, "a file moved OUT of a listed path runs macOS")
        check(key(run("pull_request", "gone-branch")) == both, "a failed diff (no such base) runs macOS")
        # Which list each probe reads: the BASE branch's (the throwaway repository's base commit holds `listtext`); `head_list` is what
        # the pull request itself writes. A pull request must not be able to rewrite the list that gates it.
        nothing = "# group: nothing\nnever/matches/**\n"
        check(key(run("pull_request", "integration", files=("shims/x",), head_list=nothing)) == both, "a pull request that rewrites the list to match nothing AND touches a shim runs macOS")
        check(key(run("pull_request", "integration", files=(), head_list=nothing)) == both, "a pull request that only rewrites the list runs macOS (the list file itself is a fixed path)")
        check(key(run("pull_request", "integration", files=(".github/macos-platform-paths.txt",))) == both, "a list edit runs macOS")
        check(key(run("pull_request", "integration", files=("docs/other.md",), head_list="# group: x\ndocs/**\n")) == both, "a pull request that adds a harmless entry runs macOS (the list file is touched)")
        check(key(run("pull_request", "integration", files=("docs/other.md",))) == ["test (ubuntu-latest)"], "the base list decides: docs only stays Ubuntu only")
        check(key(run("pull_request", "integration", files=("docs/other.md",), unrelated=True)) == both, "a diff that fails (no common history) runs macOS although the base list reads fine")
        bare = "\n".join(l for l in listtext.splitlines() if l.strip() != ".github/macos-platform-paths.txt") + "\n"
        check(key(run("pull_request", "integration", files=(".github/macos-platform-paths.txt",), listtext=bare)) == both, "the list file's own path is fixed in the script: a list edit runs macOS even if the base list does not name itself")
        # The workflow directory is hard-coded too: another workflow file runs macOS even when the base list has no entry for the directory.
        nowf = "\n".join(l for l in listtext.splitlines() if not l.strip().startswith(".github/workflows")) + "\n"
        check(key(run("pull_request", "integration", files=(".github/workflows/other.yml",), listtext=nowf)) == both, "a change under the workflow directory runs macOS even if the base list has no workflow entry")
        check("workflow file is changed" in self.summary, "the workflow-directory reason is in the step summary")
        check(key(run("pull_request", "integration", files=(".github/workflows-not/x.yml",), listtext=nowf)) == ["test (ubuntu-latest)"], "only the workflow directory itself is hard-coded, not a lookalike")
        for bad in ("", "# group: shims\n\n", None):
            check(key(run("pull_request", "integration", listtext=bad)) == both, "a list that is empty, comment-only or missing runs macOS")
            check("fail safe" in self.log and "fail safe" in self.summary, "the fail-safe line is in the step log AND the step summary")
        check(key(run("pull_request", "integration", files=("docs/other.md",), unrelated=True)) == both and "fail safe" in self.summary and "fail safe" in self.log, "a failed diff says so in the log and the summary")
        check(key(run("pull_request", "integration", files=(".github/macos-platform-paths.txt",), listtext=bare)) == both and "is edited" in self.summary, "the list-edit reason is in the step summary")
        check("fetch-depth: 0" in jobs.split("\n  test:\n", 1)[0], "the plan checkout has the full history")
        nightly = run("schedule")
        check(len(nightly) == 4 and sorted({r["ref"] for r in nightly}) == ["integration", "main"], "the nightly: main and integration, two operating systems each")
        check(all(r["name"].startswith("nightly ") and r["cloud"].startswith("nightly-cloud ") for r in nightly), "nightly names")
        check(not set(key(nightly)) & set(self.REQUIRED) and not {r["cloud"] for r in nightly} & {"test-cloud (macos-latest)", "test-cloud (ubuntu-latest)"}, "no nightly name equals a required one")
        check(sorted({r["ref"] for r in run("schedule", integration=False)}) == ["main"], "a missing integration branch drops that leg")
        check("plan" not in self.REQUIRED, "the plan job's name is not a required name")

    def test_the_real_workflow_and_list_pass_every_pin(self):
        self.pin(self.WORKFLOW.read_text(), self.LIST.read_text())

    def test_the_list_is_one_file_and_this_test_does_not_retype_it(self):
        listtext = self.LIST.read_text()
        entries = self.entries(listtext)
        for group, members in self.groups(listtext).items():
            self.assertTrue(members, group)
        self.assertEqual(sorted(self.groups(listtext)), sorted(self.GROUPS))
        source = inspect.getsource(WorkflowPlanPins)
        for entry in self.retypable(listtext):
            self.assertNotIn('"' + entry + '"', source, "the test re-types an entry: " + entry)
        every = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True).stdout.split("\n")
        for entry in entries:
            if any(c in entry for c in "*?["):
                self.assertTrue([f for f in every if fnmatch.fnmatchcase(f, entry)], "a glob matches no tracked file: " + entry)
            else:
                self.assertIn(entry, every, "an entry is not a tracked file (typo?): " + entry)
        tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", ".github"], capture_output=True, text=True).stdout.split()
        self.assertEqual([t for t in tracked if t.endswith(".txt")], [".github/macos-platform-paths.txt"])

    def mutants(self):
        """Each mutant: (name, workflow text, list text). Derived from the real files; one per member of each family."""
        text, listtext = self.WORKFLOW.read_text(), self.LIST.read_text()
        def swap(old, new):
            self.assertIn(old, text, "the mutant's anchor moved: " + old)
            return text.replace(old, new, 1)
        out = [
            ("macOS for every pull request", swap("macos = diff.returncode != 0 or LIST in files or any(", "macos = True or LIST in files or any("), listtext),
            ("macOS dropped from every pull request into integration", swap("macos = diff.returncode != 0 or LIST in files or any(", "macos = False and any("), listtext),
            ("the nightly trigger dropped", swap('  schedule:\n    - cron: "0 5 * * *"\n', ""), listtext),
            ("the nightly legs dropped", swap('if event == "schedule":', 'if event == "never":'), listtext),
            ("the nightly without integration", swap('["main"] + (["integration"]', '["main"] + ([]'), listtext),
            ("a nightly leg named like a required one", swap('name="nightly %s (%s)" % (r, o)', 'name="test (%s)" % o'), listtext),
            ("macOS skipped for every pull request into main", swap('elif base == "main" and draft:', 'elif base == "main":'), listtext),
            ("the draft rule dropped", swap('elif base == "main" and draft:', 'elif base == "main" and False:'), listtext),
            ("ready_for_review dropped", swap("types: [opened, synchronize, reopened, ready_for_review]", "types: [opened, synchronize, reopened]"), listtext),
            ("the path list not read", swap('LIST = ".github/macos-platform-paths.txt"', 'LIST = ".github/missing.txt"'), listtext),
            ("the list read from the head, no self-touch rule", swap('shown = git("show", "origin/" + base + ":" + LIST)', 'shown = git("show", "HEAD:" + LIST)').replace("or LIST in files ", ""), listtext),
            ("no self-touch rule", swap("or LIST in files ", ""), listtext),
            ("the workflow directory not hard-coded", swap("or any(f.startswith(WORKFLOWS) for f in files) ", ""), listtext),
            ("the workflow directory prefix widened", swap('WORKFLOWS = ".github/workflows/"', 'WORKFLOWS = ".github/workflows"'), listtext),
            ("the fail-safe lines not in the summary", swap("summary.write(message", "summary.write(''"), listtext),
            ("the summary written only for the log", swap('if os.environ.get("GITHUB_STEP_SUMMARY"):', "if False:"), listtext),
            ("the path list emptied", text, "# group: shims\n"),
            ("renames detected", swap('"diff", "--no-renames", "--name-only"', '"diff", "--name-only"'), listtext),
            ("a failed diff skips macOS", swap("macos = diff.returncode != 0 or LIST in files or any(", "macos = diff.returncode == 0 and any("), listtext),
            ("an empty list runs no macOS", swap("if not patterns:", "if False:"), listtext),
            ("a missing list crashes", swap("if shown.returncode == 0 else []", "if shown.returncode == 0 else [][0]"), listtext),
            ("the plan checkout shallow", swap("    steps:\n      - uses: actions/checkout@v5\n        with:\n          fetch-depth: 0\n", "    steps:\n      - uses: actions/checkout@v5\n"), listtext),
            ("edited among the types", swap("types: [opened, synchronize, reopened, ready_for_review]", "types: [opened, synchronize, reopened, ready_for_review, edited]"), listtext),
            ("the job renamed", swap("name: ${{ matrix.name }}", "name: macos-${{ matrix.os }}"), listtext),
            ("a third-party action", swap("      - name: Decide the runners\n", "      - uses: dorny/paths-filter@v3\n      - name: Decide the runners\n"), listtext),
            ("a wider permission", swap("permissions:\n  contents: read\n", "permissions:\n  contents: read\n  issues: write\n"), listtext),
            ("an unconditional cancel", swap("cancel-in-progress: ${{ github.event_name == 'pull_request' && github.event.action != 'reopened' }}", "cancel-in-progress: true"), listtext),
        ]
        for group, members in self.groups(listtext).items():
            out.append(("the group %s emptied from the list" % group, text, re.sub(r"(?m)^(?!#)(?:%s)\n" % "|".join(re.escape(m) for m in members), "", listtext)))
        for entry in self.retypable(listtext):
            out.append(("the entry %s re-typed in the workflow" % entry, text + "\n# " + entry + "\n", listtext))
        return out

    def test_every_mutant_fails_a_pin(self):
        mutants = self.mutants()
        self.assertGreaterEqual(len(mutants), 15 + len(self.GROUPS) + len(self.retypable(self.LIST.read_text())))
        for name, text, listtext in mutants:
            with self.subTest(mutant=name):
                with self.assertRaises(AssertionError, msg="the mutant passed the pins: " + name):
                    self.pin(text, listtext)

    def test_the_docs_say_which_runs_happen_when_and_how_to_re_run(self):
        docs = (REPO / "docs" / "switchboard.md").read_text()
        section = docs.split("**Which runs happen when (the test workflow).**", 1)[1].split("\n\n**", 1)[0]
        for needle in ("18 minutes", "64", "195", "198", "44 percent", "39 percent", "pull request into `main`", "pull request into `integration`",
                       "push to `main`", "nightly", "manual dispatch", "draft", "ready_for_review", ".github/macos-platform-paths.txt", "--no-renames", "is not re-planned by the retarget itself", "runs macOS and says so on the plan step",
                       "a pull request is re-run with a re-run, never by closing and reopening it"):
            self.assertIn(needle, section, needle)


class TestAgentShellCalls(Base):
    """#367: calls made from an agent's shell are counted per account and per hour, with nothing else recorded."""

    def setUp(self):
        Base.setUp(self)
        if time.time() % 3600 > 3570 or time.time() % 3600 < 10:   # the shim stamps real time: never start these across an hour boundary
            time.sleep(40)
        self.bin = self.home / "realbin"
        self.bin.mkdir()
        fake = self.bin / "gh"
        fake.write_text("#!/bin/sh\n[ \"$1\" = auth ] && { echo faketoken; exit 0; }\necho real gh out\nexit 7\n")
        fake.chmod(0o755)
        self.sb = self.home / "fakesb"
        self.sb.write_text("#!/bin/sh\nif [ \"$1\" = whoami ]; then\n  if [ \"$2\" = --json ]; then echo '{\"name\": \"worker\", \"repo\": \"acme/app\"}'; else echo worker; fi; exit 0; fi\nexit 2\n")
        self.sb.chmod(0o755)
        self.state = self.home / "state"
        self.state.mkdir()
        self.users({"acme/app": "acme-a"})
        sb.agent_calls.fold(str(self.state), time.time() - 7200)     # the daemon switches counting on, two hours ago

    def users(self, mapping):
        config = self.home / ".config" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"gh_users": mapping}))

    def shim(self, *args, env=None):
        environ = {"PATH": "%s:%s:%s" % (SHIM.parent, self.bin, SAFE_PATH), "HOME": str(self.home), "FLEET_SWITCHBOARD_STATE": str(self.state),
                   "FLEET_SWITCHBOARD_BIN": str(self.sb), "FLEET_SHIM_LOOKUP_SECONDS": SHIM_LOOKUP_SECONDS}
        environ.update(env or {})
        return subprocess.run([sys.executable, str(SHIM), *args], capture_output=True, text=True, env=environ, timeout=60)

    def test_s1_calls_are_counted_per_account(self):
        for _ in range(5):
            self.shim("api", "repos/acme/app/issues/5")
        self.users({"acme/app": "acme-b"})
        for _ in range(3):
            self.shim("api", "repos/acme/app/issues/6")
        text = "\n".join(sb.agent_calls.status_lines(str(self.state), time.time()))
        self.assertIn("agent shell calls this hour: acme-a 5, acme-b 3", text)

    def test_s2_the_hours_are_listed_and_an_hour_before_counting_began_is_not_recorded(self):
        now = time.time()
        sb.agent_calls.fold(str(self.state), now, {"acme-a": 4})
        lines = [l for l in sb.agent_calls.status_lines(str(self.state), now) if " hour " in l and "Z:" in l]
        self.assertEqual(len(lines), 6)
        self.assertEqual(len([l for l in lines if "not recorded" in l]), 3)                 # the 3 hours before counting began
        self.assertEqual(len([l for l in lines if "agent shell" in l]), 3)
        self.assertIn("daemon 4", lines[-1])

    def test_s6_a_counting_failure_changes_nothing_the_caller_sees(self):
        (self.state / "agent-calls.jsonl").mkdir()                                            # the spool cannot be opened for appending
        done = self.shim("api", "repos/acme/app/issues/5")
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"))

    def test_s7_only_the_account_the_class_and_the_time_are_stored(self):
        done = self.shim("api", "repos/acme/app/issues/5?token=queryvalue", "-f", "body=secretbody", env={"GH_TOKEN": "secrettoken"})
        text = (self.state / "agent-calls.jsonl").read_text()
        for leak in ("secrettoken", "faketoken", "secretbody", "queryvalue", "acme/app"):
            self.assertNotIn(leak, text)
        row = json.loads(text.splitlines()[0])
        self.assertEqual((row["account"], row["endpoint"], sorted(row)), ("acme-a", "api issues/#", ["account", "endpoint", "lane", "ts"]))   # the lane is the one field added (#867)

    def test_nothing_is_counted_until_the_daemon_has_switched_counting_on_and_the_switchboards_own_calls_are_not(self):
        (self.state / "agent-calls-hours.json").unlink()
        self.shim("api", "repos/acme/app/issues/5")
        self.assertFalse((self.state / "agent-calls.jsonl").exists())
        sb.agent_calls.fold(str(self.state), time.time())
        self.shim("auth", "status")
        self.assertFalse((self.state / "agent-calls.jsonl").exists())

    def test_s8_a_corrupt_count_file_starts_a_fresh_count_and_status_still_reads(self):
        (self.state / "agent-calls-hours.json").write_text("{not json")
        self.assertIn("not recorded", "\n".join(sb.agent_calls.status_lines(str(self.state), time.time())))
        self.shim("api", "repos/acme/app/issues/5")
        self.assertTrue(sb.agent_calls.fold(str(self.state), time.time(), {"acme-a": 1}))   # the daemon's next fold rebuilds the file
        self.assertEqual(json.loads((self.state / "agent-calls-hours.json").read_text())["v"], 1)
        self.assertEqual(sum(sum(r.get("agent", {}).values()) for r in sb.agent_calls.load_hours(str(self.state))[0].values()), 1)

    def test_a_full_spool_takes_no_more_lines_and_the_hours_kept_are_bounded(self):
        (self.state / "agent-calls.jsonl").write_text("x" * 300000)
        self.shim("api", "repos/acme/app/issues/5")
        self.assertEqual(len((self.state / "agent-calls.jsonl").read_text()), 300000)
        (self.state / "agent-calls.jsonl").unlink()
        now = time.time()
        for back in range(sb.agent_calls.HOURS_KEPT + 5):
            sb.agent_calls.fold(str(self.state), now - back * 3600, {"acme-a": 1})
        self.assertLessEqual(len(sb.agent_calls.load_hours(str(self.state))[0]), sb.agent_calls.HOURS_KEPT)

    def test_the_daemon_counts_only_its_new_calls_each_pass_and_keeps_them_when_the_write_fails(self):
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {}}, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""),
                                 time.time, environ={})
        engine = types.SimpleNamespace(accounts=accounts, state_dir=self.state, now=time.time, _note=lambda *a, **k: None)
        accounts.read(accounts.runner, "repos/acme/app/issues/1", "acme/app", 5)
        sb.record_github_calls(engine)
        sb.record_github_calls(engine)                                                      # nothing new: nothing added
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        self.assertEqual(sum(sum(r.get("daemon", {}).values()) for r in hours.values()), 1)
        accounts.read(accounts.runner, "repos/acme/app/issues/2", "acme/app", 5)
        with mock.patch.object(sb.agent_calls, "_write_json", return_value=False):
            sb.record_github_calls(engine)
        sb.record_github_calls(engine)                                                      # the failed pass's call is counted now
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        self.assertEqual(sum(sum(r.get("daemon", {}).values()) for r in hours.values()), 2)

    def test_lane_s1_calls_are_split_per_lane_and_account_and_status_shows_them(self):
        self.shim("api", "repos/acme/app/issues/5")                                        # lane worker (the fake switchboard's whoami name)
        self.shim("api", "repos/acme/app/issues/6")
        self.users({"acme/app": "acme-b"})
        self.shim("pr", "view", "3", env={"FLEET_SWITCHBOARD_BIN": "/nonexistent"})          # no identity and no role: unknown
        self.shim("pr", "view", "3", env={"FLEET_SWITCHBOARD_BIN": "/nonexistent", "FLEET_SWITCHBOARD_ROLE": "tester"})   # a caller's variable never names its lane
        self.assertEqual([json.loads(l).get("lane") for l in (self.state / "agent-calls.jsonl").read_text().splitlines()], ["worker", "worker", "unknown", "unknown"])
        sb.agent_calls.fold(str(self.state), time.time(), {"acme-a": {"issues/#": 2}})
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        lanes = {}
        for row in hours.values():
            for lane, accounts in row.get("lanes", {}).items():
                lanes[lane] = sum(sum(e.values()) for e in accounts.values())
        self.assertEqual(lanes, {"worker": 2, "unknown": 2, "daemon": 2})
        text = "\n".join(sb.agent_calls.status_lines(str(self.state), time.time()))
        self.assertIn("this hour per lane: daemon 2, unknown 2, worker 2", text)
        self.assertIn("lane worker: acme-a 2 (api issues/# 2)", text)

    def test_lane_an_old_spool_line_without_a_lane_is_read_as_unknown_and_a_hostile_lane_is_cleaned(self):
        (self.state / "agent-calls.jsonl").write_text(json.dumps({"ts": int(time.time()), "account": "acme-a", "endpoint": "pr view"}) + "\n"
                                                      + json.dumps({"ts": int(time.time()), "account": "acme-a", "endpoint": "pr view", "lane": "a b\nc"}) + "\n")
        text = "\n".join(sb.agent_calls.status_lines(str(self.state), time.time()))
        self.assertIn("unknown 1", text)
        self.assertIn("a?b?c 1", text)
        self.assertNotIn("a b", text)

    def test_lane_s7_no_value_reaches_the_record_beside_the_lane(self):
        self.shim("api", "repos/acme/app/issues/5?token=queryvalue", "-f", "body=secretbody", env={"GH_TOKEN": "secrettoken"})
        row = json.loads((self.state / "agent-calls.jsonl").read_text().splitlines()[0])
        self.assertEqual(sorted(row), ["account", "endpoint", "lane", "ts"])
        sb.agent_calls.fold(str(self.state), time.time())
        record = (self.state / "agent-calls-hours.json").read_text()
        for leak in ("secrettoken", "secretbody", "queryvalue", "acme/app"):
            self.assertNotIn(leak, record)

    def test_daemon_classes_keep_their_class_and_two_that_reduce_alike_add(self):
        now = time.time()
        sb.agent_calls.fold(str(self.state), now, {"acme-a": {"api issues/#": 2, "api pulls/#": 1, "api graphql": 1}})
        sb.agent_calls.fold(str(self.state), now, {"acme-b": {"api zz-one": 2, "api zz-two": 1, "browse x": 1}})   # three classes that all reduce to `other`/`api repo`/`browse`
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        row = list(hours.values())[-1]
        self.assertEqual(row["daemon"], {"acme-a": 4, "acme-b": 4})
        cells = row["lanes"]["daemon"]
        self.assertEqual(cells["acme-a"], {"api issues/#": 2, "api pulls/#": 1, "api graphql": 1})
        self.assertEqual(sum(cells["acme-b"].values()), 4)
        self.assertEqual(cells["acme-b"]["api repo"], 3)                                  # two classes reduced to one word: 2 + 1, not the last one
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {}}, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "{}", ""), time.time, environ={})
        engine = types.SimpleNamespace(accounts=accounts, state_dir=self.state, now=time.time, _note=lambda *a, **k: None)
        for path in ("repos/acme/app/issues/1", "repos/acme/app/pulls/2", "graphql"):
            accounts.read(accounts.runner, path, "acme/app", 5)
        sb.record_github_calls(engine)
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        mine = list(hours.values())[-1]["lanes"]["daemon"]["default"]
        self.assertEqual(mine, {"api issues/#": 1, "api pulls/#": 1, "api graphql": 1})   # the daemon's own classes survive the fold

    def test_lane_a_failed_write_leaves_no_temp_file(self):
        with mock.patch.object(sb.agent_calls.os, "replace", side_effect=OSError("no")):
            self.assertFalse(sb.agent_calls._write_json(str(self.state / "x.json"), {"a": 1}))
        self.assertEqual([f.name for f in self.state.iterdir() if f.name.startswith(".agent-calls-")], [])

    def test_lane_lanes_are_bounded_per_hour(self):
        lanes = "\n".join(json.dumps({"ts": int(time.time()), "account": "a", "endpoint": "pr view", "lane": "l%d" % i}) for i in range(60))
        (self.state / "agent-calls.jsonl").write_text(lanes + "\n")
        sb.agent_calls.fold(str(self.state), time.time())
        hours, _ = sb.agent_calls.load_hours(str(self.state))
        row = list(hours.values())[0]["lanes"]
        self.assertLessEqual(len(row), sb.agent_calls.LANES_PER_HOUR + 1)
        self.assertEqual(sum(sum(e.values()) for a in row.values() for e in a.values()), 60)

    def test_lane_s10_an_unreadable_spool_still_runs_the_command(self):
        (self.state / "agent-calls.jsonl").mkdir()
        done = self.shim("api", "repos/acme/app/issues/5")
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"))

    def test_usage_s2_the_daemon_records_real_usage_once_a_minute_without_counting_it(self):
        reset = int(time.time() // 3600) * 3600 + 3600   # the window then covers this whole hour bucket: no proration in the figure
        asked = []

        def runner(argv, **kw):
            asked.append((argv, kw.get("env", {}).get("FLEET_SWITCHBOARD_OWN_GH")))
            if argv[1:3] == ["auth", "token"]:
                return subprocess.CompletedProcess(argv, 0, "tok\n", "")
            return subprocess.CompletedProcess(argv, 0, json.dumps({"resources": {"core": {"limit": 5000, "used": 4000, "remaining": 1000, "reset": reset}}}), "")

        clock = [time.time()]
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {"acme/*": "acme-a"}}, runner, lambda: clock[0], environ={})
        engine = types.SimpleNamespace(accounts=accounts, state_dir=self.state, now=lambda: clock[0], _note=lambda *a, **k: None)
        sb.agent_calls.fold(str(self.state), clock[0], {"acme-a": 100})
        sb.record_github_calls(engine)
        sb.record_github_calls(engine)                                                       # inside the minute: no second read
        self.assertEqual(len([a for a in asked if a[0][1:] == ["api", "rate_limit"]]), 1)
        self.assertEqual([a[1] for a in asked if a[0][1:] == ["api", "rate_limit"]], [None])  # no marker any more: the daemon reaches the real gh by path (own_gh_path), so the shim never sees, hence never counts, it
        self.assertEqual([a[0][0] for a in asked if a[0][1:] == ["api", "rate_limit"]], [FAKE_GH])
        self.assertEqual(len(accounts.calls), 0, "the rate-limit read is not a counted call")
        accounts.read(runner, "repos/acme/app/issues/1", "acme/app", 5)
        self.assertEqual(len(accounts.calls), 1, "the daemon's OWN counter records a read it makes (the shim's per-account file never sees it)")
        self.assertEqual(asked[-1][0][0], FAKE_GH)
        body = json.loads((self.state / "github-usage.json").read_text())["accounts"]["acme-a"]
        self.assertEqual((body["used"], body["remaining"], body["reset"]), (4000, 1000, reset))
        text = "\n".join(sb.agent_calls.usage_lines(str(self.state), clock[0]))
        self.assertIn("real usage acme-a: used 4000, remaining 1000", text)
        self.assertIn("recorded 100, outside the shim and the daemon about 3900", text)
        clock[0] += 61
        sb.record_github_calls(engine)
        self.assertEqual(len([a for a in asked if a[0][1:] == ["api", "rate_limit"]]), 2)

    def test_usage_the_daemons_read_runs_the_real_gh_binary_and_its_own_counter_counts_it_not_the_shims_file(self):
        real = self.home / "realgh"
        real.write_text("#!/bin/sh\necho \"real $*\" >> %s\necho '{}'\n" % (self.home / "real.log"))
        real.chmod(0o755)
        accounts = sb.GhAccounts({"gh": str(real), "call_timeout_seconds": 5, "gh_users": {}}, subprocess.run, time.time, environ={})
        done = accounts.read(subprocess.run, "repos/acme/app/issues/1", "acme/app", 5)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.home / "real.log").read_text().splitlines(), ["real api repos/acme/app/issues/1"], "the REAL gh binary ran, by path")
        self.assertEqual(len(accounts.calls), 1, "the daemon's own counter counted it")
        self.assertFalse((self.state / "agent-calls.jsonl").exists(), "the shim's per-account call file does not record the daemon's own reads")

    def test_usage_a_garbled_read_is_left_out_and_never_raises(self):
        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {"acme/*": "acme-a"}}, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "not json", ""),
                                 time.time, environ={})
        self.assertEqual(accounts.read_usage(), {})
        self.assertIn("not recorded", "\n".join(sb.agent_calls.usage_lines(str(self.state), time.time())))

    def test_usage_the_default_token_the_account_cap_a_failed_runner_and_a_stale_reading(self):
        good = json.dumps({"resources": {"core": {"limit": 5000, "used": 10, "remaining": 4990, "reset": int(time.time()) + 600}}})
        users = {"acme/r%d" % i: "acct%d" % i for i in range(12)}

        def runner(argv, **kw):
            if argv[1:3] == ["auth", "token"]:
                return subprocess.CompletedProcess(argv, 0, "tok\n", "")
            if kw["env"].get("GH_TOKEN") == "tok" and "acct3" in str(self.__dict__.get("bad", "")):
                raise OSError("boom")
            return subprocess.CompletedProcess(argv, 0, good, "")

        accounts = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": users}, runner, time.time, environ={"GH_TOKEN": "inherited"})
        found = accounts.read_usage()
        self.assertEqual(len(found), sb.agent_calls.USAGE_ACCOUNTS + 1)    # the cap, plus the default token's reading
        self.assertIn("default", found)
        failing = sb.GhAccounts({"gh": FAKE_GH, "call_timeout_seconds": 5, "gh_users": {"acme/*": "a"}},
                                lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no"), time.time, environ={})
        self.assertEqual(failing.read_usage(), {})
        sb.agent_calls.record_usage(str(self.state), time.time() - 900, {"a": (1, 4999, 5000, int(time.time()) + 600)})
        self.assertIn("(read 15 min ago)", "\n".join(sb.agent_calls.usage_lines(str(self.state), time.time())))

    def test_lane_endpoint_classes_per_lane_and_account_are_bounded(self):
        rows = "\n".join(json.dumps({"ts": int(time.time()), "account": "a", "endpoint": "api " + sorted(sb.agent_calls.API_SUB)[i], "lane": "w"}) for i in range(40))
        (self.state / "agent-calls.jsonl").write_text(rows + "\n")
        sb.agent_calls.fold(str(self.state), time.time())
        cell = list(sb.agent_calls.load_hours(str(self.state))[0].values())[0]["lanes"]["w"]["a"]
        self.assertEqual((len(cell), sum(cell.values()), cell["other"]), (sb.agent_calls.ENDPOINTS_PER_CELL + 1, 40, 10))

    def shim_module(self):
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader("gh_shim_endpoint", str(SHIM))
        spec = importlib.util.spec_from_loader("gh_shim_endpoint", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_endpoint_class_s7_no_name_reaches_any_record_from_any_call_shape(self):
        """PC-1 as a class: an allow-list, so a name-bearing and a name-free variant of every gh shape records no name token (shim, spool, fold, status)."""
        shim = self.shim_module()
        name = "zzname"
        shapes = [["api", "https://api.github.com/repos/acme-%s/app-%s/pulls/5" % (name, name)], ["api", "repos/acme-%s/app-%s/issues/5?q=%s" % (name, name, name)],
                  ["api", "/repos/o-%s/n-%s/contents/dir-%s/file-%s.py" % (name, name, name, name)], ["api", "orgs/acme-%s/members" % name], ["api", "users/bob-%s/repos" % name],
                  ["api", "teams/t-%s" % name], ["api", "search/issues?q=repo:acme-%s/app" % name], ["api", "user/repos"], ["api", "graphql"], ["api", "rate_limit"],
                  ["api", "repos/o/n/git/ref/heads/feature-%s" % name], ["api", "http://host-%s.example/repos/o/n/issues/1" % name],
                  ["browse", "acme-%s/app" % name], ["repo", "clone", "acme-%s/app" % name], ["repo", "view", "acme-%s/app" % name], ["pr", "view", "5", "-R", "acme-%s/app" % name],
                  ["pr", "list"], ["issue", "list"], ["issue", "view", "acme-%s" % name], ["release", "view", "v-%s" % name], ["run", "view", "123"], ["workflow", "run", "ci-%s.yml" % name],
                  ["gist", "view", "g-%s" % name], ["search", "prs", name], ["auth", "status"], ["label", "list"], ["ruleset", "view", "r-%s" % name],
                  ["frobnicate-%s" % name, "x"], ["pr", "wibble-%s" % name]]
        for args in shapes:
            got = shim.call_endpoint(args)
            self.assertNotIn(name, got, args)
            self.assertNotIn("acme", got)
            self.assertEqual(got, sb.agent_calls.reduce_endpoint(got), "idempotent")                     # the daemon side reduces again to the same
            self.assertEqual(sb.agent_calls.reduce_endpoint(" ".join(shim.command_words(args)[:2])), got, "the shim and the daemon side are one rule")
            self.assertNotIn(name, sb.agent_calls._endpoint("api https://api.github.com/repos/acme/app-%s/x" % name))
        self.assertEqual(shim.call_endpoint(["api", "https://api.github.com/repos/acme/app/pulls/5"]), "api pulls/#")
        self.assertEqual((shim.call_endpoint(["api", "orgs/acme-org/members"]), shim.call_endpoint(["browse", "acme/app"])), ("api orgs", "browse"))
        self.assertEqual(shim.call_endpoint(["pr", "wibble"]), "pr")
        self.assertEqual(shim.call_endpoint(["frob", "x"]), "other")

    def test_endpoint_class_an_old_or_hostile_spool_line_cannot_carry_a_name_in(self):
        (self.state / "agent-calls.jsonl").write_text(json.dumps({"ts": int(time.time()), "account": "a", "lane": "w", "endpoint": "api https://api.github.com/repos/acme/app-zzname/pulls/#"}) + "\n"
                                                      + json.dumps({"ts": int(time.time()), "account": "a", "lane": "w", "endpoint": "browse acme/zzname"}) + "\n")
        sb.agent_calls.fold(str(self.state), time.time())
        self.assertNotIn("zzname", (self.state / "agent-calls-hours.json").read_text())
        self.assertNotIn("zzname", "\n".join(sb.agent_calls.status_lines(str(self.state), time.time())))

    def test_endpoint_class_the_daemons_own_record_is_reduced_too(self):
        self.assertEqual(sb._endpoint_of("orgs/acme-org/members?x=1"), "orgs")
        self.assertEqual(sb._endpoint_of("repos/acme/app/issues/12/parent"), "issues/#/parent")

    def test_the_fold_moves_the_spool_into_the_hours_and_loses_no_line(self):
        for _ in range(2):
            self.shim("api", "repos/acme/app/issues/5")
        now = time.time()
        self.assertTrue(sb.agent_calls.fold(str(self.state), now))
        self.assertFalse((self.state / "agent-calls.jsonl").exists())
        hours, since = sb.agent_calls.load_hours(str(self.state))
        self.assertEqual(sum(sum(r.get("agent", {}).values()) for r in hours.values()), 2)
        self.assertEqual(stat.S_IMODE(os.stat(str(self.state / "agent-calls-hours.json")).st_mode), 0o600)


class TestShimReadCache(Base):
    """#867 clauses 3 and 4: a repeated identical `gh api <path>` read is answered from a short cache and revalidated with the entity tag; a write by the same agent
    drops it; below 15 percent of the budget the shim says so once and serves a cached read instead of spending."""

    def setUp(self):
        TestAgentShellCalls.setUp(self)
        (self.bin / "gh").write_text(
            "#!/bin/sh\n[ \"$1\" = auth ] && { echo faketoken; exit 0; }\necho \"$@\" >> \"$FAKE_LOG\"\n"
            "case \"$*\" in\n *If-None-Match*) printf 'HTTP/2.0 304 Not Modified\\r\\n\\r\\n'; exit 0;;\n"
            " \"api -i \"*) printf 'HTTP/2.0 200 OK\\r\\nEtag: \"v1\"\\r\\n\\r\\n{\"n\":1}'; exit 0;;\n"
            " \"api \"*) printf '{\"n\":1}'; exit 0;;\nesac\necho wrote\nexit 0\n")
        self.log = self.home / "fake.log"
        self.other = self.home / "othersb"
        self.other.write_text(self.sb.read_text().replace("worker", "other"))
        self.other.chmod(0o755)

    def run_shim(self, *args, env=None):
        return TestAgentShellCalls.shim(self, *args, env=dict({"FAKE_LOG": str(self.log)}, **(env or {})))

    def network(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def age(self, seconds):
        cache = self.state / "gh-read-cache"
        for name in os.listdir(cache):
            if name.startswith(".w-"):
                mark = json.loads((cache / name).read_text())
                mark["ts"] -= seconds   # an agent's write mark ages with the entries
                (cache / name).write_text(json.dumps(mark))
                continue
            entry = json.loads((cache / name).read_text())
            entry["ts"] -= seconds
            (cache / name).write_text(json.dumps(entry))

    def usage(self, remaining, limit=5000, account="acme-a"):
        (self.state / "github-usage.json").write_text(json.dumps({"v": 1, "at": time.time(), "accounts": {account: {"used": limit - remaining, "remaining": remaining, "limit": limit, "reset": time.time() + 900}}}))

    users = TestAgentShellCalls.users
    shim_module = TestAgentShellCalls.shim_module
    PATH = "repos/acme/app/issues/5"

    def test_s3_a_repeated_read_is_served_from_the_cache_then_revalidated_and_a_304_is_not_counted(self):
        first = self.run_shim("api", self.PATH)
        second = self.run_shim("api", self.PATH)
        self.assertEqual((first.stdout, second.stdout, second.returncode), ('{"n":1}', '{"n":1}', 0))
        self.assertEqual(len(self.network()), 1, "2 reads, 1 network call")
        self.age(31)
        third = self.run_shim("api", self.PATH)
        self.assertEqual(third.stdout, '{"n":1}')
        self.assertEqual(len(self.network()), 2)
        self.assertIn("If-None-Match: \"v1\"", self.network()[1])
        self.assertEqual(len((self.state / "agent-calls.jsonl").read_text().splitlines()), 1, "the 304 is not counted as a call")

    def marker_mutant(self, func):
        """#1216: a copy of the shim directory in which `func` honours the retired marker again (returns at once)."""
        source = SHIM.read_text()
        start = source.index("def %s(" % func)
        head = source[start:]
        at = head.index(":\n", head.index(")")) + 2
        if head[at:].lstrip().startswith('"""'):
            at = head.index('"""', head.index('"""', at) + 3) + 4
        guard = '    if os.environ.get("FLEET_SWITCHBOARD_OWN_GH") == "1":\n        return None\n'
        target = self.home / ("mutant-" + func)
        shutil.copytree(str(SHIM.parent), str(target))
        (target / "gh").write_text(source[:start] + head[:at] + guard + head[at:])
        return target / "gh"

    def test_mutants_honouring_the_marker_in_count_call_and_the_cache_fail_these_cases(self):   # #1216
        marker = {"FLEET_SWITCHBOARD_OWN_GH": "1"}
        for func in ("count_call", "cached_read"):
            with self.subTest(func=func):
                (self.state / "agent-calls.jsonl").unlink() if (self.state / "agent-calls.jsonl").exists() else None
                shutil.rmtree(str(self.state / "gh-read-cache"), True)
                self.log.write_text("")
                self.run_shim("api", self.PATH, env=marker); self.run_shim("api", self.PATH, env=marker)
                spooled = (self.state / "agent-calls.jsonl").read_text().splitlines() if (self.state / "agent-calls.jsonl").exists() else []
                real = (len(spooled), len(self.network()))
                self.assertEqual(real, (1, 1), "the real shim, marker set: one call counted, the repeat served from the cache")
                (self.state / "agent-calls.jsonl").unlink()
                shutil.rmtree(str(self.state / "gh-read-cache"), True)
                self.log.write_text("")
                mutant = self.marker_mutant(func)
                with mock.patch.dict(globals(), {"SHIM": mutant}), rebound("SHIM", mutant):
                    self.run_shim("api", self.PATH, env=marker); self.run_shim("api", self.PATH, env=marker)
                spooled = (self.state / "agent-calls.jsonl").read_text().splitlines() if (self.state / "agent-calls.jsonl").exists() else []
                self.assertNotEqual((len(spooled), len(self.network())), real, "the mutant differs: the case above fails it")

    def test_s8_a_write_by_the_same_agent_drops_its_entries_and_another_agents_write_does_not(self):
        self.run_shim("api", self.PATH)
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), 1)
        self.run_shim("api", self.PATH, env={"FLEET_SWITCHBOARD_BIN": str(self.other)})   # the other agent has its own entry (key includes the agent)
        self.assertEqual(len(self.network()), 2)
        self.run_shim("issue", "edit", "5", "-t", "x", env={"FLEET_SWITCHBOARD_BIN": str(self.other)})   # a write by the other agent
        self.run_shim("api", self.PATH)
        self.assertEqual(len([l for l in self.network() if l.startswith("api")]), 2, "the first agent's entry survived the other agent's write")
        self.run_shim("issue", "edit", "5", "-t", "x")                                     # a write by the first agent
        self.run_shim("api", self.PATH)
        self.assertEqual(len([l for l in self.network() if l.startswith("api")]), 3, "its next read goes to GitHub")

    def test_s4_below_15_percent_one_warning_a_cached_read_is_served_and_a_write_is_never_refused(self):
        self.usage(500)
        first = self.run_shim("api", self.PATH)
        self.assertEqual(first.stderr.count("warning"), 1)
        write = self.run_shim("issue", "edit", "5", "-t", "x")
        self.assertEqual((write.returncode, write.stderr.count("warning")), (0, 0))      # one line a minute
        self.age(20)                                                                      # past the write's grace window
        self.run_shim("api", self.PATH)                                                   # the write dropped the entry: this read spends and stores
        before = len(self.network())
        self.age(100)                                                                     # older than the fresh bound, still in the stale bound
        again = self.run_shim("api", self.PATH)
        self.assertEqual((again.stdout, len(self.network())), ('{"n":1}', before), "served from the cache instead of spending")

    def test_s5_s9_far_from_the_limit_output_and_exit_status_are_those_of_the_plain_call(self):
        self.usage(3000)
        for args in (("api", self.PATH), ("issue", "edit", "5", "-t", "x"), ("pr", "view", "3")):
            plain = self.run_shim(*args, env={"FLEET_SHIM_CACHE": "off"})
            now = self.run_shim(*args)
            self.assertEqual((now.stdout, now.stderr, now.returncode), (plain.stdout, plain.stderr, plain.returncode), args)
            self.assertNotIn("warning", now.stderr)

    def test_s10_a_cache_that_cannot_be_written_still_reaches_gh(self):
        (self.state / "gh-read-cache").write_text("not a directory")
        (self.state / "agent-calls.jsonl").mkdir()
        done = self.run_shim("api", self.PATH)
        self.assertEqual((done.returncode, done.stdout, len(self.network())), (0, '{"n":1}', 1))

    def test_edges_an_error_answer_garbage_a_corrupt_entry_and_a_304_with_no_entry_pass_through_and_cache_nothing(self):
        gh = self.bin / "gh"
        good = gh.read_text()
        gh.write_text("#!/bin/sh\n[ \"$1\" = auth ] && { echo faketoken; exit 0; }\necho \"$@\" >> \"$FAKE_LOG\"\n"
                      "case \"$FAKE_MODE\" in\n 404) printf 'HTTP/2.0 404 Not Found\\r\\n\\r\\n{\"message\":\"Not Found\"}'; echo 'gh: Not Found (HTTP 404)' >&2; exit 1;;\n"
                      " garbage) printf 'not http at all'; exit 0;;\n 304) printf 'HTTP/2.0 304 Not Modified\\r\\n\\r\\n'; exit 0;;\nesac\nexit 0\n")
        for mode, code, out in (("404", 1, '{"message":"Not Found"}'), ("garbage", 0, "not http at all"), ("304", 0, "")):
            done = self.run_shim("api", self.PATH, env={"FAKE_MODE": mode})
            self.assertEqual((done.returncode, done.stdout), (code, out), mode)
            if mode == "404":
                self.assertIn("HTTP 404", done.stderr)
            cache = self.state / "gh-read-cache"
            self.assertEqual([n for n in (os.listdir(cache) if cache.exists() else []) if n.endswith(".json")], [], mode)
        gh.write_text(good)
        self.run_shim("api", self.PATH)
        cache = self.state / "gh-read-cache"
        for name in os.listdir(cache):
            (cache / name).write_text("{not json")
        before = len(self.network())
        self.assertEqual(self.run_shim("api", self.PATH).stdout, '{"n":1}')
        self.assertEqual(len(self.network()), before + 1, "a corrupt entry is a miss")
        self.age(700)
        before = len(self.network())
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), before + 1, "an entry older than the bound is a miss")

    def test_edges_low_budget_with_no_entry_spends_and_a_stale_reading_is_ignored(self):
        self.usage(100)
        first = self.run_shim("api", self.PATH)
        self.assertEqual((first.stdout, len(self.network())), ('{"n":1}', 1))          # nothing to serve: it spends, never refuses
        (self.state / "github-usage.json").write_text(json.dumps({"v": 1, "at": time.time() - 3600, "accounts": {"acme-a": {"used": 4900, "remaining": 100, "limit": 5000, "reset": time.time()}}}))
        shutil.rmtree(str(self.state / "gh-low-warned"), ignore_errors=True)
        self.age(100)
        again = self.run_shim("api", self.PATH)
        self.assertEqual((again.stderr, len(self.network())), ("", 2), "a reading an hour old is ignored: no warning, and the stale entry is revalidated")

    def test_edges_a_path_that_names_the_working_directory_or_a_host_is_not_cached(self):
        self.run_shim("api", "repos/{owner}/{repo}/issues")
        self.run_shim("api", self.PATH, env={"GH_HOST": "ghe.example"})
        cache = self.state / "gh-read-cache"
        self.assertEqual([n for n in (os.listdir(cache) if cache.exists() else []) if n.endswith(".json")], [])

    def test_edges_a_failed_output_after_the_request_never_makes_a_second_request(self):
        module = self.shim_module()
        calls = []
        real_run = module.subprocess.run

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b'HTTP/2.0 200 OK\r\nEtag: "v1"\r\n\r\n{"n":1}', b"")

        class Broken:
            def isatty(self):
                return False

            @property
            def buffer(self):
                raise BrokenPipeError("closed")

        os.environ["FLEET_SWITCHBOARD_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FLEET_SWITCHBOARD_STATE", None)
        with mock.patch.object(module.subprocess, "run", fake_run), mock.patch.object(module.sys, "stdout", Broken()):
            status = module.cached_read("gh", {}, ["api", self.PATH], "worker", "acme-a", None)
        self.assertEqual((status, len(calls)), (0, 1))

    def test_m2_paths_that_report_a_state_are_never_cached_in_any_spelling(self):
        forms = ["rate_limit", "/rate_limit", "rate_limit?x=1", "RATE_LIMIT", "rate_limit/", "https://api.github.com/rate_limit", "%72ate_limit", "%2572ate_limit", "repos/../rate_limit",
                 "api/v3/rate_limit", "user", "user/", "applications/abc/token", "installation/repositories", "notifications",
                 "repos/acme/app/commits/abc/check-runs", "repos/acme/app/commits/abc/status", "repos/acme/app/actions/runs/1", "repos/acme/app/actions/jobs/2"]
        for form in forms:
            self.log.write_text("") if self.log.exists() else None
            self.run_shim("api", form)
            self.run_shim("api", form)
            self.assertEqual(len(self.network()), 2, form)
            cache = self.state / "gh-read-cache"
            self.assertEqual([n for n in (os.listdir(cache) if cache.exists() else []) if n.endswith(".json")], [], form)

    def test_m2_the_table_is_what_the_shim_denies(self):
        module = self.shim_module()
        for word in module.NEVER_CACHED:
            self.assertTrue(module.never_cached("repos/o/n/%s" % word) or module.never_cached(word), word)
            self.assertIsNone(module.cacheable_read(["api", "/%s/" % word.upper()]), word)
        self.assertIsNotNone(module.cacheable_read(["api", "repos/acme/app/issues/5"]))
        self.assertIsNotNone(module.cacheable_read(["api", "repos/user/status/issues/1"]), "an owner or repository NAMED like a state word is still a name")

    def test_m1_a_read_in_flight_across_a_write_by_the_same_agent_never_stores_its_old_answer(self):
        module = self.shim_module()
        os.environ["FLEET_SWITCHBOARD_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FLEET_SWITCHBOARD_STATE", None)
        out = io.BytesIO()

        class Out:
            def isatty(self):
                return False
            buffer = out

        for write_finishes_first in (True, False):
            shutil.rmtree(str(self.state / "gh-read-cache"), ignore_errors=True)

            def slow_read(argv, **kw):
                module.cache_drop("worker")                      # the same agent's write begins while the request is in flight
                if write_finishes_first:
                    module.cache_mark("worker")                  # and ends before the read does
                return subprocess.CompletedProcess(argv, 0, b'HTTP/2.0 200 OK\r\nEtag: "v1"\r\n\r\n{"old":1}', b"")

            with mock.patch.object(module.subprocess, "run", slow_read), mock.patch.object(module.sys, "stdout", Out()):
                module.cache_directory(create=True)
                os.utime(str(self.state / "gh-read-cache"))
                status = module.cached_read("gh", {}, ["api", self.PATH], "worker", "acme-a", None)
            self.assertEqual(status, 0)
            cache = self.state / "gh-read-cache"
            self.assertEqual([n for n in os.listdir(cache) if n.endswith(".json")], [], "the pre-write answer was not stored (write finishing first: %s)" % write_finishes_first)

    def test_m1_a_read_made_inside_the_grace_window_after_a_write_goes_to_github_and_stores_nothing(self):
        self.run_shim("api", self.PATH)
        self.run_shim("issue", "edit", "5", "-t", "x")
        before = len(self.network())
        self.run_shim("api", self.PATH)
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), before + 2)
        self.age(20)
        self.run_shim("api", self.PATH)
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), before + 3, "after the window the cache works again")

    def test_cross_calls_two_accounts_do_not_share_and_a_paginated_read_is_not_served_from_the_cache(self):
        self.run_shim("api", self.PATH)
        self.users({"acme/app": "acme-b"})
        self.run_shim("api", self.PATH)
        self.assertEqual(len(self.network()), 2, "another account is another key")
        self.run_shim("api", self.PATH, "--paginate")
        self.assertEqual(len(self.network()), 3, "a call of another shape is never answered from the cache")

    def overlapped(self, module, entry_age, answer, write_between=True):
        """Run cached_read for a path with an entry of `entry_age` seconds, while a write by the same agent happens during the request. Returns (status, files left)."""
        os.environ["FLEET_SWITCHBOARD_STATE"] = str(self.state)
        self.addCleanup(os.environ.pop, "FLEET_SWITCHBOARD_STATE", None)
        shutil.rmtree(str(self.state / "gh-read-cache"), ignore_errors=True)
        directory = module.cache_directory(create=True)
        file = module.cache_file(directory, "worker", "acme-a", self.PATH)
        module.cache_store(directory, file, '"v1"', '{"old":1}', time.time() - entry_age)
        out = io.BytesIO()

        class Out:
            def isatty(self):
                return False
            buffer = out

        def request(argv, **kw):
            if write_between:
                module.cache_drop("worker")   # the same agent writes while the request is in flight
            return subprocess.CompletedProcess(argv, 0, answer, b"")

        with mock.patch.object(module.subprocess, "run", request), mock.patch.object(module.sys, "stdout", Out()):
            status = module.cached_read("gh", {}, ["api", self.PATH], "worker", "acme-a", None)
        return status, [n for n in os.listdir(directory) if n.endswith(".json")], out.getvalue()

    def test_every_store_of_the_cache_is_guarded_by_the_write_mark_and_each_is_pinned(self):
        """Completeness: the shim's cache is written in exactly these places; a new one must be added here with an overlapped test."""
        source = SHIM.read_text()
        self.assertEqual(source.count("cache_store("), 3, "one definition and two call sites (the 304 refresh and the 2xx store): pin any new one below")
        self.assertEqual(source.count("not raced"), 2, "both call sites are behind the write mark")
        module = self.shim_module()
        # the 304 refresh: an old entry is revalidated while the same agent writes: nothing is rewritten, and the next read goes to GitHub
        status, files, out = self.overlapped(module, 40, b"HTTP/2.0 304 Not Modified\r\n\r\n")
        self.assertEqual((status, files), (0, []))
        # the 2xx store: same overlap
        status, files, out = self.overlapped(module, 40, b'HTTP/2.0 200 OK\r\nEtag: "v2"\r\n\r\n{"new":1}')
        self.assertEqual((status, files, out), (0, [], b'{"new":1}'))
        # and with no write in between both are stored (the guard is not a blanket refusal)
        status, files, out = self.overlapped(module, 40, b"HTTP/2.0 304 Not Modified\r\n\r\n", write_between=False)
        self.assertEqual((status, len(files), out), (0, 1, b'{"old":1}'))
        # the drop itself removes only this agent's entries
        directory = module.cache_directory()
        other = module.cache_file(directory, "someone", "acme-a", self.PATH)
        module.cache_store(directory, other, None, "{}", time.time())
        module.cache_drop("worker")
        self.assertTrue(os.path.exists(other))

    def test_m2_double_percent_encoding_is_read_as_what_it_decodes_to(self):
        module = self.shim_module()
        self.assertEqual(module.normal_api_path("%2572ate_limit"), ["rate_limit"])
        self.assertTrue(module.never_cached("%2572ate_limit"))

    def test_the_cache_is_private_and_holds_no_token(self):
        self.run_shim("api", self.PATH, env={"GH_TOKEN": "secrettoken"})
        cache = self.state / "gh-read-cache"
        self.assertEqual(stat.S_IMODE(os.stat(str(cache)).st_mode), 0o700)
        for name in os.listdir(cache):
            self.assertEqual(stat.S_IMODE(os.stat(str(cache / name)).st_mode), 0o600)
            self.assertNotIn("acme", name)
            for leak in ("secrettoken", "faketoken", "acme-a"):
                self.assertNotIn(leak, (cache / name).read_text())

    def test_only_the_plain_get_shape_is_cached_and_the_cache_is_bounded(self):
        self.run_shim("api", self.PATH, "--jq", ".n")
        self.run_shim("api", self.PATH, "-X", "GET")
        self.run_shim("pr", "view", "3")
        self.assertFalse((self.state / "gh-read-cache").exists() and os.listdir(self.state / "gh-read-cache"))
        module = TestAgentShellCalls.shim_module(self)
        directory = self.state / "gh-read-cache"
        directory.mkdir(exist_ok=True)
        module.CACHE_FILES_MAX = 10
        for i in range(15):
            module.cache_store(str(directory), str(directory / ("a%02d.json" % i)), None, "{}", time.time())
        self.assertLessEqual(len([n for n in os.listdir(directory) if n.endswith(".json")]), 10)
        old = directory / "old.json"
        old.write_text("{}")
        os.utime(str(old), (time.time() - 3600, time.time() - 3600))
        module.cache_store(str(directory), str(directory / "new.json"), None, "{}", time.time())
        self.assertFalse(old.exists(), "an entry older than the age bound is removed")


class TestSameNameAccountsAdd(Base):
    """#879: accounts whose names clean to the same name are ADDED in the hourly fold, the load hours and the usage record, never overwritten."""

    SAME = ("acme a", "acme/a")     # both clean to `acme?a`
    WORD = "acme?a"

    def _fold(self, mod, delta):
        d = Path(tempfile.mkdtemp(dir=str(self.home)))
        mod.fold(str(d), 1000000.0, delta)
        return mod.load_hours(str(d))[0][str(mod._hour(1000000.0))]

    def _usage(self, mod, readings):
        d = Path(tempfile.mkdtemp(dir=str(self.home)))
        mod.record_usage(str(d), 1000000.0, readings)
        return json.loads((d / "github-usage.json").read_text())["accounts"]

    def _mutant(self, old, new):
        src = Path(sb.agent_calls.__file__).read_text()
        self.assertEqual(src.count(old), 1, "the mutant's target line moved")
        mod = types.ModuleType("agent_calls_mutant")
        mod.__file__ = sb.agent_calls.__file__
        exec(compile(src.replace(old, new), "agent_calls_mutant", "exec"), mod.__dict__)
        return mod

    def _checks(self, mod):
        """Each record's check, by name; returns the names that fail."""
        failed = []
        a, b = self.SAME
        if self._fold(mod, {a: 3, b: 4})["daemon"].get(self.WORD) != 7:
            failed.append("fold")
        d = Path(tempfile.mkdtemp(dir=str(self.home)))
        (d / "agent-calls-hours.json").write_text(json.dumps({"hours": {"5": {"agent": {a: 3, b: 4}}}}))
        if mod.load_hours(str(d))[0]["5"]["agent"].get(self.WORD) != 7:
            failed.append("load")
        if self._usage(mod, {a: (3, 10, 50, 100), b: (4, 20, 50, 200)}).get(self.WORD, {}).get("used") != 7:
            failed.append("usage")
        return failed

    def test_s2_the_fold_adds_two_accounts_that_clean_to_one_word(self):
        a, b = self.SAME
        self.assertEqual(self._fold(sb.agent_calls, {a: 3, b: 4})["daemon"], {self.WORD: 7})
        row = self._fold(sb.agent_calls, {a: {"api issues/#": 1}, b: {"api issues/#": 2, "api graphql": 1}})   # the per-endpoint form adds too
        self.assertEqual(sum(sum(c.values()) for c in row["lanes"]["daemon"].values()), 4)
        self.assertEqual(row["daemon"], {self.WORD: 4})

    def test_s3_the_load_hours_add_them(self):
        self.assertEqual(self._checks(sb.agent_calls), [])

    def test_s4_the_usage_record_adds_them(self):
        a, b = self.SAME
        got = self._usage(sb.agent_calls, {a: (3, 10, 50, 100), b: (4, 20, 50, 200)})
        self.assertEqual(got, {self.WORD: {"used": 7, "remaining": 30, "limit": 100, "reset": 200}})

    def test_s5_a_scan_of_the_code_finds_no_place_that_overwrites(self):
        tree = ast.parse(Path(sb.agent_calls.__file__).read_text())

        def keyed(node):
            return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_account" for n in ast.walk(node))

        places, overwriting = [], []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            gets = {ast.dump(c.func.value) for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "get" and c.args and keyed(c.args[0])}
            for n in ast.walk(fn):
                if isinstance(n, (ast.DictComp, ast.SetComp)) and keyed(n.key if isinstance(n, ast.DictComp) else n.elt):
                    places.append((fn.name, n.lineno))
                    overwriting.append((fn.name, n.lineno))        # a comprehension keyed by a cleaned name keeps only the last
                elif isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Subscript) and keyed(t.slice):
                            places.append((fn.name, n.lineno))
                            if ast.dump(t.value) not in gets:       # it must read the earlier count under the same cleaned key
                                overwriting.append((fn.name, n.lineno))
        self.assertGreaterEqual(len(places), 3, places)
        self.assertEqual(overwriting, [], "these places overwrite instead of adding")

    def test_s6_each_overwriting_mutant_fails_a_check(self):
        mutants = {
            "load": ("out[_account(a)] = out.get(_account(a), 0) + int(n)", "out[_account(a)] = int(n)"),
            "fold": ("delta[_account(a)] = delta.get(_account(a), 0) + int(n)", "delta[_account(a)] = int(n)"),
            "usage": ('"used": old["used"] + u,', '"used": u,'),
        }
        for name, (old, new) in mutants.items():
            with self.subTest(name):
                self.assertIn(name, self._checks(self._mutant(old, new)), "the mutant must fail its record's check")

    def test_u1_distinct_cleaned_names_are_recorded_as_before(self):
        self.assertEqual(self._fold(sb.agent_calls, {"acme-a": 3, "acme-b": 4})["daemon"], {"acme-a": 3, "acme-b": 4})
        d = Path(tempfile.mkdtemp(dir=str(self.home)))
        (d / "agent-calls-hours.json").write_text(json.dumps({"hours": {"5": {"agent": {"acme-a": 3, "acme-b": 4}}}}))
        self.assertEqual(sb.agent_calls.load_hours(str(d))[0]["5"]["agent"], {"acme-a": 3, "acme-b": 4})
        got = self._usage(sb.agent_calls, {"acme-a": (3, 10, 50, 100), "acme-b": (4, 20, 60, 200)})
        self.assertEqual(got, {"acme-a": {"used": 3, "remaining": 10, "limit": 50, "reset": 100}, "acme-b": {"used": 4, "remaining": 20, "limit": 60, "reset": 200}})

    def test_u2_a_negative_count_and_a_non_number_are_dropped(self):
        a, b = self.SAME
        for bad in (-5, "x", None, True):
            with self.subTest(bad=bad):
                d = Path(tempfile.mkdtemp(dir=str(self.home)))
                (d / "agent-calls-hours.json").write_text(json.dumps({"hours": {"5": {"agent": {a: 3, b: bad}}}}))
                self.assertEqual(sb.agent_calls.load_hours(str(d))[0]["5"]["agent"], {self.WORD: 3})
                self.assertEqual(self._fold(sb.agent_calls, {a: 3, b: bad})["daemon"].get(self.WORD), 3)


class TestSelfRegisteringCli(unittest.TestCase):
    """The front door (#179 follow-up): commands register and dispatch BY NAME, so a new command edits no shared list. What must not change: the set of
    subcommands and each one's help, the function each runs and what it is given, the daemon start, the error mapping; and a hostile name never runs
    an arbitrary function."""
    SURFACE_DIR = REPO / "scenarios" / "switchboard" / "cli-surface" / "commands"   # ONE file per command: two PRs adding different commands never touch the same file

    @classmethod
    def read_surface(cls):
        """{"commands": [sorted names], "help": {name: help}} read from the per-command files; a file whose name and `command` disagree is an error."""
        found = {}
        for path in sorted(cls.SURFACE_DIR.glob("*.json")):
            entry = json.loads(path.read_text())
            if entry.get("command") != path.stem:
                raise AssertionError("%s describes %r: a snapshot file is named for its command" % (path.name, entry.get("command")))
            found[path.stem] = entry["help"]
        return {"commands": sorted(found), "help": found}
    # Written from the OLD dispatch chain, not derived from the new code: action -> (function, the arguments it was called with)
    OLD = {
        "status": ("cmd_status", ("args", "runner", "now")), "poke": ("cmd_poke", ("args",)), "policy-report": ("cmd_policy_report", ("args", "runner", "now")),
        "agents": ("cmd_agents", ("args", "runner", "now")), "job": ("cmd_job", ("args", "runner", "now", "spawn")), "review-laws": ("cmd_review_laws", ("args", "runner", "now")), "review": ("cmd_review", ("args", "runner", "now")), "pr-explain": ("cmd_pr_explain", ("args", "runner", "now")), "disk": ("cmd_disk", ("args", "runner", "now")), "pr-state": ("cmd_pr_state", ("args", "runner", "now")),
        "reattach": ("cmd_reattach", ("args", "runner", "now", "socket_call")),
        "retire": ("cmd_retire", ("args", "runner", "now")),
        "impact": ("cmd_impact", ("args", "runner", "now")), "judge-tool": ("cmd_judge_tool", ("args", "runner", "now")),
        "ensure": ("cmd_ensure", ("args", "spawn")), "install": ("cmd_install", ("args", "runner", "now", "spawn")), "key": ("cmd_key", ("args", "runner", "now")),
        "restart": ("cmd_restart", ("args", "runner", "spawn")), "bootstrap": ("cmd_bootstrap", ("args", "runner", "now", "socket_call")),
        "launch": ("cmd_launch", ("args", "runner", "now", "socket_call")), "handoff": ("cmd_handoff", ("args", "runner", "now", "socket_call")),
        "run": ("cmd_run", ("args", "runner", "now", "wait")), "send": ("cmd_send", ("args", "runner", "now")), "report": ("cmd_report", ("args", "runner", "now")),
        "notice": ("cmd_notice", ("args", "runner", "now")), "prove": ("cmd_prove", ("args", "runner", "now")), "verdict": ("cmd_verdict", ("args", "runner", "now")),
        "waive": ("cmd_waive", ("args", "runner", "now")), "gates": ("cmd_gates", ("args", "runner", "now")), "gates-check": ("cmd_gates_check", ("args", "runner", "now")),
        "decisions": ("cmd_decisions", ("args", "runner", "now")), "comment": ("cmd_comment", ("args", "runner", "now")), "whoami": ("cmd_whoami", ("args", "runner", "now")),
        "pending": ("cmd_pending", ("args", "runner", "now")), "remind": ("cmd_remind", ("args", "runner", "now")), "intent": ("cmd_intent", ("args", "runner", "now")),
        "intents": ("cmd_intents", ("args", "runner", "now")), "import-v1": ("cmd_import_v1", ("args", "runner", "now")), "orders": ("cmd_orders", ("args", "runner", "now")),
        "load-replay": ("cmd_load_replay", ("args", "runner", "now")), "labels": ("cmd_labels", ("args", "runner", "now")), "plan": ("cmd_plan", ("args", "runner", "now", "socket_call")),
        "triage": ("cmd_triage", ("args", "runner", "now"))}
    OLD_ENSURE_ON_USE = ("send", "report", "notice", "launch", "handoff", "comment", "remind", "decisions", "intent", "bootstrap")
    OLD_DIRECT = ("status", "poke", "policy-report", "review-laws", "review", "pr-explain", "impact", "plan", "judge-tool")
    OLD_LEDGER = ("prove", "verdict", "waive", "gates", "gates-check")

    @staticmethod
    def plain(text):
        """Help text as words: the wrapping and the section name differ between Python versions and terminal widths, the content must not."""
        return " ".join(text.replace("optional arguments:", "options:").split())

    @staticmethod
    def commands():
        parser = sb.build_parser()
        (action,) = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
        return parser, action.choices

    def test_every_subcommand_of_the_snapshot_still_exists_with_the_same_help(self):
        _, choices = self.commands()
        if os.environ.get("CLI_SURFACE_UPDATE") == "1":
            self.SURFACE_DIR.mkdir(parents=True, exist_ok=True)
            for stale in self.SURFACE_DIR.glob("*.json"):
                stale.unlink()
            for name in sorted(choices):
                (self.SURFACE_DIR / ("%s.json" % name)).write_text(json.dumps({"command": name, "help": self.plain(choices[name].format_help())}, indent=1, sort_keys=True) + "\n")
        failure = self.snapshot_failure(self.SURFACE_DIR)
        self.assertIsNone(failure, failure)
        snap = self.read_surface()
        for name in snap["commands"]:
            with self.subTest(command=name):
                self.assertEqual(self.plain(choices[name].format_help()), snap["help"][name])

    def snapshot_failure(self, surface_dir):
        """The message of the REAL surface comparison (the body of test_every_subcommand_of_the_snapshot...) run against `surface_dir`, or None when it passes."""
        _, choices = self.commands()
        try:
            with mock.patch.object(self.__class__, "SURFACE_DIR", surface_dir):
                snap = self.read_surface()
            self.assertEqual(sorted(choices), snap["commands"], "the set of subcommands changed: only in the code %s, only in the snapshot %s" % (
                sorted(set(choices) - set(snap["commands"])), sorted(set(snap["commands"]) - set(choices))))
        except AssertionError as err:
            return str(err)
        return None

    def test_a_command_without_a_snapshot_file_and_a_file_without_a_command_each_fail_and_name_it(self):
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work)
            for source in self.SURFACE_DIR.glob("*.json"):
                (copy / source.name).write_text(source.read_text())
            self.assertIsNone(self.snapshot_failure(copy), "a complete copy passes")
            (copy / "status.json").unlink()                                              # a command with no snapshot file
            message = self.snapshot_failure(copy)
            self.assertIsNotNone(message)
            self.assertIn("only in the code ['status']", message)
            (copy / "status.json").write_text((self.SURFACE_DIR / "status.json").read_text())
            (copy / "ghost-command.json").write_text(json.dumps({"command": "ghost-command", "help": "x"}))    # a file with no command
            message = self.snapshot_failure(copy)
            self.assertIsNotNone(message)
            self.assertIn("only in the snapshot ['ghost-command']", message)
            (copy / "ghost-command.json").unlink()
            (copy / "alpha.json").write_text(json.dumps({"command": "beta", "help": "x"}))                     # a file named for one command, describing another
            message = self.snapshot_failure(copy)
            self.assertIn("alpha.json", message)

    def test_the_snapshot_is_one_file_per_command_with_a_full_help_text(self):
        files = sorted(self.SURFACE_DIR.glob("*.json"))
        self.assertGreater(len(files), 40)
        self.assertFalse((self.SURFACE_DIR.parent / "commands.json").exists(), "the old single shared file is gone")
        for path in files:
            entry = json.loads(path.read_text())
            self.assertEqual(sorted(entry), ["command", "help"], path.name)
            self.assertTrue(entry["help"].startswith("usage: fleet-switchboard " + path.stem), path.name)

    def test_a_new_command_edits_no_shared_table_it_registers_says_its_group_beside_itself_and_dispatches(self):
        calls, ensured = [], []

        def add_fixture_parsers(sub):
            sub.add_parser("fixture", help="a command added with no shared edit")

        def cmd_fixture(args, runner, now):
            calls.append("fixture")
            raise sb.LedgerCommandError("a ledger error is a usage error")

        cmd_fixture.ledger = True
        cmd_fixture.ensure_on_use = True
        names = {"add_fixture_parsers": add_fixture_parsers, "cmd_fixture": cmd_fixture}
        before = (sb.DIRECT_ACTIONS, sb.LEDGER_ACTIONS, sb.ENSURE_ON_USE_COMMANDS)
        with mock.patch.dict(sb.__dict__, names), mock.patch.object(sb, "ensure_on_use", lambda spawn=None: ensured.append("ensured")):
            parser = sb.build_parser()
            self.assertIn("fixture", parser.commands)
            code = sb.dispatch(types.SimpleNamespace(action="fixture"), parser.commands, runner="R", now="N")
            sb.derive_action_groups()
            self.assertIn("fixture", sb.LEDGER_ACTIONS)
            self.assertIn("fixture", sb.ENSURE_ON_USE_COMMANDS)
            self.assertNotIn("fixture", sb.DIRECT_ACTIONS)
        sb.derive_action_groups()
        self.assertEqual((sb.DIRECT_ACTIONS, sb.LEDGER_ACTIONS, sb.ENSURE_ON_USE_COMMANDS), before, "the groups return to what the module defines")
        self.assertEqual((calls, ensured, code), (["fixture"], ["ensured"], sb.EXIT_USAGE), "it ran, the daemon was ensured first, the ledger error mapped to a usage error")

    def test_two_branches_that_add_different_commands_touch_no_common_file_and_merge_cleanly(self):
        with tempfile.TemporaryDirectory() as work:
            env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.test",
                       GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")

            def git(*args):
                done = subprocess.run(["git", "-C", work] + list(args), capture_output=True, text=True, env=env)
                self.assertEqual(done.returncode, 0, "git %s: %s%s" % (args, done.stdout, done.stderr))
                return done.stdout

            git("init", "-q", "-b", "main")
            target = Path(work) / "scenarios" / "switchboard" / "cli-surface" / "commands"
            target.mkdir(parents=True)
            for existing in self.SURFACE_DIR.glob("*.json"):
                (target / existing.name).write_text(existing.read_text())
            git("add", ".")
            git("commit", "-qm", "base")
            touched = []
            for branch, name in (("one", "alpha-cmd"), ("two", "beta-cmd")):
                git("checkout", "-q", "-b", branch, "main")
                (target / ("%s.json" % name)).write_text(json.dumps({"command": name, "help": "usage: fleet-switchboard %s" % name}))
                git("add", ".")
                git("commit", "-qm", name)
                touched.append(set(git("diff", "--name-only", "main", branch).split()))
            self.assertEqual(touched[0] & touched[1], set(), "0 files changed by both branches")
            git("checkout", "-q", "one")
            git("merge", "--no-edit", "two")
            self.assertTrue((target / "alpha-cmd.json").exists() and (target / "beta-cmd.json").exists())

    def test_the_old_dispatch_table_is_still_registered_and_every_registered_command_has_a_handler(self):
        """OLD is the table of the ORIGINAL commands and never grows: a new command adds no row here. What must hold for every command, old or new, is
        derived: it is registered, and `resolve_command` finds its `cmd_<action>` function."""
        parser, choices = self.commands()
        self.assertTrue(set(self.OLD) <= set(choices), "an original command is gone: %s" % sorted(set(self.OLD) - set(choices)))
        self.assertEqual(parser.commands, frozenset(choices))
        for action in sorted(set(choices) - {"version"}):
            with self.subTest(action=action):
                handler = sb.resolve_command(action, parser.commands)
                self.assertTrue(callable(handler), "%s has no cmd_ function" % action)
                required = [n for n, p in inspect.signature(handler).parameters.items() if p.default is p.empty and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)]
                self.assertTrue(set(required) <= set(sb.COMMAND_ARGUMENTS), "%s asks for %s, which a command is never given" % (action, sorted(set(required) - set(sb.COMMAND_ARGUMENTS))))

    def run_dispatch(self, action, **namespace):
        """dispatch() for `action` with every function replaced by a recorder that keeps the real signature: (return value, calls, ensure calls)."""
        calls, ensured = [], []
        fakes = {}
        for name in (set(fn for fn, _ in self.OLD.values()) - {"cmd_plan"}) | {"cmd_plan_check", "cmd_plan_review"}:   # cmd_plan stays real: it chooses between the two
            real = getattr(sb, name)

            def make(real=real, name=name):
                signature = inspect.signature(real)

                def fake(*a, **kw):
                    calls.append((name, dict(signature.bind(*a, **kw).arguments)))   # by parameter name, whether it was called by position or by keyword
                    return 17
                fake.__signature__ = inspect.signature(real)
                fake.__name__ = name
                return fake
            fakes[name] = make()
        args = types.SimpleNamespace(action=action, plan_action=namespace.get("plan_action"))
        with mock.patch.multiple(sb, **fakes), mock.patch.object(sb, "ensure_on_use", lambda spawn=None: ensured.append(action)):
            code = sb.dispatch(args, self.commands()[0].commands, runner="R", now="N", wait="W", spawn="S", socket_call="C")
        return code, calls, ensured

    def test_every_command_runs_the_same_function_with_the_same_arguments_as_before(self):
        values = {"args": None, "runner": "R", "now": "N", "spawn": "S", "socket_call": "C", "wait": "W"}
        for action, (function, given) in self.OLD.items():
            with self.subTest(action=action):
                code, calls, ensured = self.run_dispatch(action)
                if action == "plan":   # `plan check` is the default and, as before, was given no socket
                    function, given = "cmd_plan_check", ("args", "runner", "now")
                self.assertEqual(code, 17)
                self.assertEqual([c[0] for c in calls], [function], "exactly the old function, once")
                self.assertEqual(sorted(calls[0][1]), sorted(given), "given exactly what it was given before")
                for name in given:
                    if name != "args":
                        self.assertEqual(calls[0][1][name], values[name])
                self.assertEqual(ensured, [action] if action in self.OLD_ENSURE_ON_USE else [], "the daemon is started for the same commands")

    def test_plan_review_is_the_one_subcommand_that_chooses_a_second_function(self):
        code, calls, _ = self.run_dispatch("plan", plan_action="review")
        self.assertEqual([c[0] for c in calls], ["cmd_plan_review"])
        self.assertEqual(sorted(calls[0][1]), ["args", "now", "runner", "socket_call"])

    def test_version_prints_and_runs_nothing(self):
        with captured() as (out, err):
            code, calls, ensured = self.run_dispatch("version")
        self.assertEqual((code, calls, ensured), (0, [], []))
        self.assertIn(sb.VERSION, out.getvalue())

    @staticmethod
    def declared_groups():
        """{group: set(actions)} read from the SOURCE: every module-level `cmd_x.<group> = True` of bin/fleet-switchboard, for a command the parser
        registers. This is what each command says beside itself: the expected sets are DERIVED from it, never kept as a list in a test."""
        declared = {"direct": set(), "ledger": set(), "ensure_on_use": set()}
        registered = TestSelfRegisteringCli.commands()[0].commands
        tree = ast.parse(switchboard_source())
        plain = {id(node) for node in tree.body}
        for node in ast.walk(tree):    # a declaration written any other way would be invisible to the scan AND to the views: refuse it
            if isinstance(node, ast.Assign) and any(isinstance(x, ast.Attribute) and x.attr in declared and isinstance(x.value, ast.Name)
                                                    and x.value.id.startswith("cmd_") for x in node.targets):
                if id(node) not in plain or not (isinstance(node.value, ast.Constant) and node.value.value is True):
                    raise AssertionError("line %d declares a command's dispatch group in a form the guard cannot read: write `cmd_x.<group> = True` at module level" % node.lineno)
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute) \
                    and isinstance(node.targets[0].value, ast.Name) and node.targets[0].attr in declared \
                    and isinstance(node.value, ast.Constant) and node.value.value is True and node.targets[0].value.id.startswith("cmd_"):
                action = node.targets[0].value.id[4:].replace("_", "-")
                if action in registered:
                    declared[node.targets[0].attr].add(action)
        return declared

    def group_problems(self):
        """One line per group whose effective set differs from what the commands declare, naming the commands."""
        declared, problems = self.declared_groups(), []
        for name, effective, key in (("DIRECT_ACTIONS", sb.DIRECT_ACTIONS, "direct"), ("LEDGER_ACTIONS", sb.LEDGER_ACTIONS, "ledger"),
                                     ("ENSURE_ON_USE_COMMANDS", sb.ENSURE_ON_USE_COMMANDS, "ensure_on_use")):
            if set(effective) != declared[key]:
                problems.append("%s differs from what the commands declare: missing %s, extra %s" % (name, sorted(declared[key] - set(effective)), sorted(set(effective) - declared[key])))
        return problems

    def test_the_error_mapping_and_the_special_groups_are_what_they_were(self):
        # The effective groups (the module's views) must equal what the commands DECLARE (read from the source), exactly: a command whose declared
        # group differs from its effective one fails loudly. And the pure-move guarantee stays: every original member is still in its group.
        self.assertEqual(self.group_problems(), [])
        with mock.patch.object(sb, "LEDGER_ACTIONS", frozenset(set(sb.LEDGER_ACTIONS) - {"gates"})):       # the guard fails loudly and names the command
            self.assertEqual(self.group_problems(), ["LEDGER_ACTIONS differs from what the commands declare: missing ['gates'], extra []"])
        with mock.patch.object(sb, "DIRECT_ACTIONS", frozenset(set(sb.DIRECT_ACTIONS) | {"send"})):
            self.assertEqual(self.group_problems(), ["DIRECT_ACTIONS differs from what the commands declare: missing [], extra ['send']"])
        self.assertTrue(set(self.OLD_DIRECT) <= set(sb.DIRECT_ACTIONS))
        self.assertTrue(set(self.OLD_LEDGER) <= set(sb.LEDGER_ACTIONS))
        self.assertTrue(set(self.OLD_ENSURE_ON_USE) <= set(sb.ENSURE_ON_USE_COMMANDS))
        self.assertNotIn("judge-tool", sb.ENSURE_ON_USE_COMMANDS, "judge-tool runs inside v2 on every tool call: it never starts the daemon")
        known = self.commands()[0].commands

        def raising(error):
            def fake(args, runner, now, **kw):
                raise error
            return fake
        args = lambda action: types.SimpleNamespace(action=action, plan_action=None)
        with mock.patch.object(sb, "ensure_on_use", lambda spawn=None: None):
            with mock.patch.object(sb, "cmd_send", raising(sb.V2Error("down"))), captured() as (out, err):   # a mapped command: its error is EXIT_ERROR with the message
                self.assertEqual(sb.dispatch(args("send"), known), sb.EXIT_ERROR)
                self.assertIn("down", err.getvalue())
            with mock.patch.object(sb, "cmd_status", raising(sb.V2Error("down"))):   # a direct command: the error is its own, not mapped
                with self.assertRaises(sb.V2Error):
                    sb.dispatch(args("status"), known)
            with mock.patch.object(sb, "cmd_gates", raising(sb.LedgerCommandError("bad ledger"))), captured() as (out, err):   # ledger commands: a usage error
                self.assertEqual(sb.dispatch(args("gates"), known), sb.EXIT_USAGE)
                self.assertIn("gates: bad ledger", err.getvalue())
            with mock.patch.object(sb, "cmd_send", raising(sb.LedgerCommandError("x"))):   # elsewhere that error is not mapped (as before)
                with self.assertRaises(sb.LedgerCommandError):
                    sb.dispatch(args("send"), known)
            with mock.patch.object(sb, "cmd_send", raising(sb.SimulatedCrash("boom"))), captured() as (out, err):
                self.assertEqual(sb.dispatch(args("send"), known), sb.EXIT_CRASH)

    def test_a_hostile_or_unknown_name_cannot_run_an_arbitrary_function(self):
        known = self.commands()[0].commands
        ran = []
        with mock.patch.object(sb, "cmd_gates_stats", lambda *a, **k: ran.append("gates_stats")), mock.patch.object(sb, "cmd_decision_write", lambda *a, **k: ran.append("write")), \
                mock.patch.object(sb, "ensure_on_use", lambda spawn=None: ran.append("ensure")):
            for name in (None, 5, "", "Status", "status ", " status", "status;x", "../status", "gates-stats", "gates_stats", "decision-write", "cmd-status", "cmd_status",
                         "__import__", "__class__", "build-parser", "build_parser", "dispatch", "main", "resolve-command", "os.system", "sb", "x" * 500, "status\x00", "STATUS"):
                with self.subTest(name=name):
                    self.assertIsNone(sb.resolve_command(name, known))
                    with captured() as (out, err):
                        code = sb.dispatch(types.SimpleNamespace(action=name, plan_action=None), known)
                    self.assertEqual(code, sb.EXIT_USAGE)
                    self.assertIn("unknown command", err.getvalue())
        self.assertEqual(ran, [], "nothing ran, not even the daemon start")
        for action in known:   # a registered command resolves to its OWN cmd_ function and to nothing else
            if action != "version":
                self.assertEqual(sb.resolve_command(action, known).__name__, "cmd_" + action.replace("-", "_"))
        with mock.patch.dict(sb.__dict__, {"cmd_zzz": 5}):   # a cmd_ name that is not callable is not a command
            self.assertIsNone(sb.resolve_command("zzz", frozenset({"zzz"})))

    def test_a_new_command_is_two_functions_and_no_shared_edit(self):
        seen = []

        def add_synthetic_parsers(sub):
            sub.add_parser("synthetic", help="a command added by name only").add_argument("--x")

        def cmd_synthetic(args, runner, now):
            seen.append((args.x, runner, now))
            return 7

        def add_not_a_registrar_parser(sub):   # the registrar name rule: `add_<name>_parsers` only
            raise AssertionError("must not be collected")
        with mock.patch.dict(sb.__dict__, {"add_synthetic_parsers": add_synthetic_parsers, "cmd_synthetic": cmd_synthetic, "add_not_a_registrar_parser": add_not_a_registrar_parser}):
            self.assertIn("add_synthetic_parsers", [r.__name__ for r in sb.command_registrars()])
            self.assertNotIn("add_not_a_registrar_parser", [r.__name__ for r in sb.command_registrars()])
            self.assertEqual(sb.main(["synthetic", "--x", "1"], runner="R", now="N"), 7)
        self.assertEqual(seen, [("1", "R", "N")])
        with captured() as (out, err):
            self.assertEqual(sb.main(["synthetic"], runner="R", now="N"), sb.EXIT_USAGE, "outside the patch the command does not exist")

    def test_a_new_command_can_say_it_is_direct_next_to_its_own_definition(self):
        def add_direct_demo_parsers(sub):
            sub.add_parser("direct-demo")

        def cmd_direct_demo(args, runner, now):
            raise sb.V2Error("its own error")
        cmd_direct_demo.direct = True
        with mock.patch.dict(sb.__dict__, {"add_direct_demo_parsers": add_direct_demo_parsers, "cmd_direct_demo": cmd_direct_demo}):
            with self.assertRaises(sb.V2Error):   # direct: the error is not mapped (a mapped command would return EXIT_ERROR)
                sb.main(["direct-demo"])

    def test_a_command_may_ask_only_for_the_arguments_a_command_is_given(self):
        _, choices = self.commands()
        for action in choices:
            if action == "version":
                continue
            handler = sb.resolve_command(action, frozenset(choices))
            with self.subTest(action=action):
                required = [n for n, p in inspect.signature(handler).parameters.items() if p.default is p.empty]
                self.assertTrue(set(required) <= set(sb.COMMAND_ARGUMENTS), required)

        def greedy(args, secrets):
            return 0
        with self.assertRaises(TypeError) as caught:
            sb.call_command(greedy, args=1, runner=2)
        self.assertIn("secrets", str(caught.exception))
        self.assertEqual(sb.call_command(lambda args, now=None: (args, now), args=1, runner=2, now=3), (1, 3), "only what it names, from what is available")

    def test_help_order_is_the_registration_order_and_the_docs_say_so(self):
        _, choices = self.commands()
        registered = [n for n in choices if n not in ("status", "run", "send", "report", "notice", "ensure", "poke", "launch", "pending", "remind", "whoami", "comment", "handoff", "version")]
        self.assertEqual(registered[0], "agents")
        self.assertIn("alphabetical by function name", (REPO / "docs" / "switchboard.md").read_text())


class TestGhShimGateProbe(Base):
    """The shim asks `gates-check` before `pr ready` and `pr merge`: a refusal (exit 10, only in an enforcing repo) stops the command."""

    def setUp(self):
        super().setUp()
        self.bin = self.home / "realbin"
        self.bin.mkdir()
        self.log = self.home / "gh-log.json"
        fake = self.bin / "gh"
        fake.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                        "if sys.argv[1:3] == ['repo', 'view']:\n    v = os.environ.get('FAKE_REPO')\n    print(v or ''); sys.exit(0 if v else 1)\n"
                        "if sys.argv[1:3] == ['pr', 'view'] and 'reviews,headRefOid,reviewDecision' in sys.argv:\n"
                        "    if os.environ.get('FAKE_REVIEWS_EXIT'): sys.exit(int(os.environ['FAKE_REVIEWS_EXIT']))\n"
                        "    print(os.environ.get('FAKE_REVIEWS') or '{\"reviews\": [], \"headRefOid\": \"' + 'a'*40 + '\"}'); sys.exit(0)\n"
                        "if sys.argv[1:3] == ['pr', 'view']:\n    v = os.environ.get('FAKE_PRURL')\n    print(v or ''); sys.exit(0 if v else 1)\n"
                        "json.dump({'argv': sys.argv[1:]}, open(%r,'w'))\n"
                        "print('real gh out'); sys.exit(7)\n" % str(self.log))
        fake.chmod(0o755)
        self.calls_log = self.home / "gates-calls.log"
        self.sb = self.home / "fakesb"
        self.sb.write_text("#!/bin/sh\n" + FAKE_SB_SOURCE +
                           "if [ \"$1\" = whoami ]; then\n  if [ \"$2\" = --json ]; then echo '{\"name\": \"worker\", \"repo\": \"acme/app\"}'; else echo worker; fi; exit 0; fi\n"
                           "if [ \"$1\" = review ] && [ \"$2\" = blockers ]; then\n  echo \"$@\" >> %r\n  [ -n \"$FAKE_BLOCKERS_EXIT\" ] && exit $FAKE_BLOCKERS_EXIT\n  [ -n \"$FAKE_BLOCKERS\" ] && echo \"$FAKE_BLOCKERS\"; exit 0; fi\n"
                           "if [ \"$1\" = gates-check ]; then\n  echo \"$@ own=$FLEET_SWITCHBOARD_OWN_GH\" >> %r\n"
                           "  [ -n \"$FAKE_GATE_SLEEP\" ] && sleep $FAKE_GATE_SLEEP\n  if [ -n \"$FAKE_GATE_JSON\" ]; then echo \"$FAKE_GATE_JSON\"; else echo '{\"verdict\": \"x\"}'; fi; exit ${FAKE_GATE_EXIT:-0}\nfi\nexit 2\n" % (str(self.calls_log) + ".review", str(self.calls_log)))
        self.sb.chmod(0o755)
        subprocess.run([str(self.sb), "whoami", "--json"], capture_output=True, timeout=60)   # the first start of a new file is the slow one
        self.state = self.home / "state"
        self.state.mkdir()
        self.write_gates({"acme/app": {"mode": "shadow"}})

    def write_gates(self, repos):
        config = self.home / ".config" / "fleet-switchboard" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"plan_gates": {"repos": repos}} if repos is not None else {}))

    def run_shim(self, *args, env=None, binary=True):
        environ = {"PATH": "%s:%s:%s" % (SHIM.parent, self.bin, SAFE_PATH), "HOME": str(self.home),
                   "FLEET_SWITCHBOARD_STATE": str(self.state), "FLEET_SHIM_LOOKUP_SECONDS": SHIM_LOOKUP_SECONDS,
                   "FLEET_SHIM_GATE_SECONDS": SHIM_LOOKUP_SECONDS}   # the gate question's bound too: the largest the shim allows
        if binary:
            environ["FLEET_SWITCHBOARD_BIN"] = str(self.sb)
        environ.update(env or {})
        write_fake_env(self.home, environ)
        if self.log.exists():
            self.log.unlink()
        started = time.time()
        done = subprocess.run([sys.executable, str(SHIM), *args], capture_output=True, text=True, env=environ, timeout=60)
        self.elapsed = time.time() - started
        seen = json.loads(self.log.read_text()) if self.log.exists() else None
        return done, seen

    def gate_calls(self):
        return self.calls_log.read_text().splitlines() if self.calls_log.exists() else []

    def spool(self):
        path = self.state / "gate-failopen.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_ready_and_merge_ask_gates_check_for_the_right_pr_and_the_real_gh_runs_unchanged(self):
        for args, kind in ((("pr", "ready", "12", "-R", "acme/app"), "ready"), (("pr", "merge", "12", "--repo", "acme/app", "--squash"), "merge"),
                           (("pr", "merge", "https://github.com/acme/app/pull/12", "--delete-branch"), "merge"),
                           (("pr", "ready", "12"), "ready")):   # a bare number: the repo is the caller's own (whoami)
            with self.subTest(args=args):
                if self.calls_log.exists():
                    self.calls_log.unlink()
                done, seen = self.run_shim(*args, env={"FAKE_REPO": "acme/app"})
                self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), "the real gh's exit code and output pass through")
                self.assertEqual(seen["argv"], list(args))
                (call,) = self.gate_calls()
                self.assertEqual(call, "gates-check acme/app#12 --for %s --json own=" % kind)

    def review(self, state, who="rev", at="2026-01-01T00:00:00Z"):
        return {"state": state, "author": {"login": who}, "submittedAt": at}

    def merge(self, *extra, reviews=(), env=None, args=("pr", "merge", "12", "-R", "acme/app", "--merge")):
        e = {"FAKE_REPO": "acme/app", "FAKE_REVIEWS": json.dumps({"reviews": list(reviews), "headRefOid": self.HEAD})}
        e.update(env or {})
        return self.run_shim(*args, *extra, env=e)

    def test_a_merge_over_a_changes_requested_review_is_refused_naming_the_reviewer_in_any_repo(self):
        done, seen = self.merge(reviews=[self.review("CHANGES_REQUESTED", "reviewer-a")])
        self.assertEqual(done.returncode, 10)
        self.assertIn("blocking review(s): reviewer-a: CHANGES_REQUESTED", done.stderr)
        self.assertIn("--over-review", done.stderr)
        self.assertIsNone(seen, "the real gh never ran")
        done, seen = self.merge("--admin", reviews=[self.review("CHANGES_REQUESTED", "reviewer-a")])
        self.assertEqual((done.returncode, seen), (10, None))                    # --admin changes nothing
        done, seen = self.merge(reviews=[self.review("CHANGES_REQUESTED", "reviewer-a")], env={"FAKE_REPO": "other/repo"},
                                args=("pr", "merge", "12", "-R", "other/repo", "--merge"))
        self.assertEqual(done.returncode, 10)                                    # a repo not listed under plan_gates is covered too

    OLD = "b" * 40

    def review_at(self, state, who, oid):
        return dict(self.review(state, who), commit={"oid": oid})

    def merge_decided(self, reviews, decision, *extra):
        e = {"FAKE_REPO": "acme/app", "FAKE_REVIEWS": json.dumps({"reviews": reviews, "headRefOid": self.HEAD, "reviewDecision": decision})}
        return self.run_shim("pr", "merge", "12", "-R", "acme/app", "--merge", *extra, env=e)

    def test_s1_a_changes_requested_review_at_the_current_head_blocks(self):
        done, seen = self.merge_decided([self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)], "CHANGES_REQUESTED")
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("blocking review(s): reviewer-a: CHANGES_REQUESTED", done.stderr)

    def test_s2_an_old_head_changes_requested_review_blocks_while_github_says_changes_requested(self):
        done, seen = self.merge_decided([self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD)], "CHANGES_REQUESTED")
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("reviewer-a: CHANGES_REQUESTED", done.stderr)

    def test_s3_a_newer_comment_by_the_same_reviewer_supersedes_an_old_head_changes_requested_review(self):
        reviews = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD), self.review_at("COMMENTED", "reviewer-a", self.HEAD)]
        done, seen = self.merge_decided(reviews, "APPROVED")
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        self.assertEqual([l for l in self.spool() if l["event"] == "over_review"], [], "no over-review line")

    def test_an_old_head_changes_requested_review_with_no_newer_review_does_not_block_unless_github_says_so(self):
        done, seen = self.merge_decided([self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD)], "APPROVED")
        self.assertEqual(done.returncode, 7, done.stderr)

    def test_a_review_without_a_commit_blocks_and_a_comment_before_it_or_another_changes_request_does_not_clear_it(self):
        for reviews in ([self.review("CHANGES_REQUESTED", "reviewer-a")],
                        [self.review_at("COMMENTED", "reviewer-a", self.HEAD), self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)],
                        [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.OLD), self.review_at("COMMENTED", "reviewer-a", self.OLD),
                         self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)]):
            with self.subTest(reviews=[r["state"] for r in reviews]):
                done, seen = self.merge_decided(reviews, None)
                self.assertEqual((done.returncode, seen), (10, None), done.stderr)

    def test_a_same_head_changes_requested_review_is_not_cleared_by_a_comment_from_the_same_shared_login(self):
        reviews = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD), self.review_at("COMMENTED", "reviewer-a", self.HEAD)]
        for decision in ("", None, "APPROVED"):
            with self.subTest(decision=decision):
                done, seen = self.merge_decided(reviews, decision)
                self.assertEqual((done.returncode, seen), (10, None), done.stderr)
        done, seen = self.merge_decided(reviews + [self.review_at("APPROVED", "reviewer-a", self.HEAD)], "APPROVED")   # an approval clears it
        self.assertEqual(done.returncode, 7, done.stderr)

    def test_s4_a_newer_comment_by_another_reviewer_does_not_supersede(self):
        reviews = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD), self.review_at("COMMENTED", "reviewer-b", self.HEAD)]
        done, seen = self.merge_decided(reviews, "")
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("reviewer-a: CHANGES_REQUESTED", done.stderr)
        self.assertNotIn("reviewer-b", done.stderr)

    def test_s5_an_unreadable_reviews_read_fails_open_with_one_warning_and_the_override_is_still_logged(self):
        done, seen = self.merge(env={"FAKE_REVIEWS_EXIT": "1"})
        self.assertEqual(done.returncode, 7)
        self.assertEqual(done.stderr.count("review check skipped"), 1, done.stderr)

    def test_s7_the_over_review_reason_rule_holds_with_the_head_rule(self):
        block = [self.review_at("CHANGES_REQUESTED", "reviewer-a", self.HEAD)]
        done, seen = self.merge("--over-review", "abcdefghi", reviews=block)
        self.assertEqual((done.returncode, seen), (10, None))
        done, seen = self.merge("--over-review", "abcdefghij", reviews=block)
        self.assertEqual(done.returncode, 7, done.stderr)
        self.assertEqual(len([l for l in self.spool() if l["event"] == "over_review"]), 1)

    def test_a_dismissed_or_superseded_review_and_plain_comments_do_not_block(self):
        for reviews in ([self.review("CHANGES_REQUESTED", "rev"), self.review("DISMISSED", "rev")],
                        [self.review("CHANGES_REQUESTED", "rev"), self.review("APPROVED", "rev")],
                        [self.review("CHANGES_REQUESTED", "rev"), self.review("COMMENTED", "other"), self.review("APPROVED", "rev")],
                        [self.review("APPROVED", "rev"), self.review("COMMENTED", "rev")]):
            with self.subTest(reviews=[r["state"] for r in reviews]):
                done, seen = self.merge(reviews=reviews)
                self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        done, seen = self.merge(reviews=[self.review("APPROVED", "a"), self.review("CHANGES_REQUESTED", "b")])   # one reviewer's block is enough
        self.assertEqual(done.returncode, 10)

    def test_a_recorded_blocking_verdict_at_the_current_head_blocks_and_is_named(self):
        done, seen = self.merge(env={"FAKE_BLOCKERS": "security recorded request-changes (2 blocking finding(s)) at aaaaaaaa"})
        self.assertEqual(done.returncode, 10)
        self.assertIn("security recorded request-changes", done.stderr)
        asked = (self.calls_log.parent / (self.calls_log.name + ".review")).read_text().split()
        self.assertEqual(asked, ["review", "blockers", "acme/app#12", "--head", self.HEAD])      # the shim binds the question to the head it read

    def test_over_review_with_a_reason_goes_ahead_is_stripped_and_logged_and_without_one_it_is_refused(self):
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        done, seen = self.merge("--over-review", "the owner accepted the risk in chat", reviews=block)
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        self.assertNotIn("--over-review", " ".join(seen["argv"]))
        self.assertNotIn("the owner accepted", " ".join(seen["argv"]))
        self.assertIn("on purpose", done.stderr)
        (line,) = [l for l in self.spool() if l["event"] == "over_review"]
        self.assertIn("worker", line["detail"])
        self.assertIn("the owner accepted the risk in chat", line["detail"])
        self.assertIn("reviewer-a: CHANGES_REQUESTED", line["detail"])
        self.assertEqual(line["pr"], "acme/app#12")
        done, seen = self.merge("--over-review=a reason of enough length", reviews=block)           # the = spelling
        self.assertEqual(done.returncode, 7)
        for extra in (("--over-review", "short"), ("--over-review",)):
            done, seen = self.merge(*extra, reviews=block)
            self.assertEqual((done.returncode, seen), (10, None))
            self.assertIn("at least 10 characters", done.stderr)
        done, seen = self.merge("--over-review", "a reason that nobody needs", reviews=[])           # nothing to override: just stripped
        self.assertEqual((done.returncode, "--over-review" in " ".join(seen["argv"])), (7, False))

    def test_a_reviews_api_or_switchboard_failure_lets_the_merge_through_with_a_visible_warning(self):
        done, seen = self.merge(env={"FAKE_REVIEWS_EXIT": "1"})
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"))
        self.assertIn("could not be read): not checked", done.stderr)
        done, seen = self.merge(env={"FAKE_BLOCKERS_EXIT": "1"})
        self.assertEqual(done.returncode, 7)
        self.assertIn("recorded verdicts could not be read", done.stderr)
        done, seen = self.merge(env={"FAKE_REVIEWS": "not json"})
        self.assertEqual(done.returncode, 7)
        self.assertIn("not checked", done.stderr)

    def tagged(self, first, who="shared", tag="rev", at="2026-01-01T00:00:00Z", state="COMMENTED"):
        body = "%s\n\nfindings...\n\n<!-- fleet-switchboard:from=%s -->" % (first, tag)
        return {"state": state, "author": {"login": who}, "submittedAt": at, "body": body}

    def test_a_commented_review_whose_first_line_says_changes_requested_at_the_current_head_blocks_and_a_later_approve_clears_it(self):
        cr = self.tagged("Review of %s: CHANGES REQUESTED" % self.HEAD)
        done, seen = self.merge(reviews=[cr])
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("shared/rev: CHANGES REQUESTED (Review of %s)" % self.HEAD[:8], done.stderr)
        for spelling in ("Review of %s: CHANGES REQUESTED" % self.HEAD[:7], "review of %s - changes requested" % self.HEAD, "Review of %s: REQUEST CHANGES" % self.HEAD):
            done, seen = self.merge(reviews=[self.tagged(spelling)])                  # a short sha prefix and the other spellings count
            self.assertEqual(done.returncode, 10, spelling)
        done, seen = self.merge(reviews=[cr, self.tagged("Review of %s: APPROVE" % self.HEAD)])
        self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        done, seen = self.merge(reviews=[cr, self.tagged("Review of %s: APPROVED" % self.HEAD, tag="other")])   # another reviewer's approve does not clear this one
        self.assertEqual(done.returncode, 10)
        done, seen = self.merge(reviews=[self.tagged("Review of %s: APPROVE" % self.HEAD), cr])                 # the latest one decides
        self.assertEqual(done.returncode, 10)

    def test_a_changes_requested_tag_at_an_old_head_and_untagged_comments_never_block(self):
        old = "b" * 40
        for reviews in ([self.tagged("Review of %s: CHANGES REQUESTED" % old)],
                        [{"state": "COMMENTED", "author": {"login": "shared"}, "body": "CHANGES REQUESTED, please fix this"}],
                        [{"state": "COMMENTED", "author": {"login": "shared"}, "body": "Looks fine.\nReview of %s: CHANGES REQUESTED" % self.HEAD}],   # not the FIRST line
                        [{"state": "COMMENTED", "author": {"login": "shared"}, "body": None}],
                        [self.tagged("Review of nothex: CHANGES REQUESTED")]):
            with self.subTest(body=str(reviews[0].get("body"))[:40]):
                done, seen = self.merge(reviews=reviews)
                self.assertEqual((done.returncode, done.stdout.strip()), (7, "real gh out"), done.stderr)
        done, seen = self.merge("--over-review", "the owner reviewed this head in person", reviews=[self.tagged("Review of %s: CHANGES REQUESTED" % self.HEAD)])
        self.assertEqual(done.returncode, 7)                                    # the explicit override still works

    def test_an_override_whose_log_cannot_be_written_is_refused_and_a_pr_number_after_the_flag_is_not_its_reason(self):
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        done, seen = self.merge("--over-review", "a fine long reason here", reviews=block, env={"FLEET_SWITCHBOARD_STATE": str(self.home / "no-such-state")})
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("could not be logged", done.stderr)
        done, seen = self.merge(reviews=block, args=("pr", "merge", "--over-review", "12", "-R", "acme/app", "--merge"))   # `12` is the PR
        self.assertEqual(done.returncode, 10)
        self.assertIn("at least 10 characters", done.stderr)
        done, seen = self.merge("--over-review", "x" * 300, reviews=block)       # a long reason: the head and the overridden review still fit the line
        self.assertEqual(done.returncode, 7)
        line = [l for l in self.spool() if l["event"] == "over_review"][-1]["detail"]
        self.assertIn(self.HEAD[:12], line)
        self.assertIn("reviewer-a: CHANGES_REQUESTED", line)
        done, seen = self.merge("--over-review", "a reason\x1b[31m with escapes", reviews=block)
        logged = [l for l in self.spool() if l["event"] == "over_review"][-1]["detail"]
        self.assertNotIn("\x1b", logged)
        self.assertIn("with escapes", logged)

    def test_only_merge_is_checked_and_the_marker_buys_nothing(self):
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        done, seen = self.merge(reviews=block, args=("pr", "ready", "12", "-R", "acme/app"))
        self.assertEqual(done.returncode, 7)

    def test_a_blocking_review_refuses_a_merge_with_the_marker_set_and_unset(self):   # #1216 S2: the review probe does not honour the marker
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        for env in ({"FLEET_SWITCHBOARD_OWN_GH": "1"}, {}):
            with self.subTest(env=env):
                done, seen = self.merge(reviews=block, env=env)
                self.assertEqual((done.returncode, seen), (10, None), done.stderr)
                self.assertIn("blocking review", done.stderr)

    HEAD = "a" * 40
    REFUSAL = ("fleet-switchboard: merge of acme/app#12 refused: missing G1: no passing CI record at this head. Way out: ask the chief of "
               "staff for a logged waiver (`fleet-switchboard waive acme/app#12 --gates G1 --reason ...`; never for G6 or G8), or the chief "
               "of staff can switch the repo to shadow (`fleet-switchboard gates mode acme/app shadow`).")

    def test_an_enforcing_refusal_stops_the_command_and_prints_the_reason(self):
        self.write_gates({"acme/app": {"mode": "enforce"}})
        for args in (("pr", "merge", "12", "-R", "acme/app"), ("pr", "ready", "12", "-R", "acme/app")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": self.REFUSAL})})
                self.assertEqual(done.returncode, 10)
                self.assertIsNone(seen, "the real gh did not run")
                self.assertEqual(done.stdout, "")
                self.assertEqual(done.stderr.strip(), self.REFUSAL)
                self.assertEqual(self.spool()[-1]["event"], "refused")

    def test_a_refusal_with_no_message_still_stops(self):
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"mode": "enforce"})})
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("refused", done.stderr)

    def test_everything_else_runs_the_real_gh(self):
        for code in ("3", "1", "127", "0"):   # fail-open, crash, missing, allow (a valid waiver, a shadow repo, a kill switch all answer 0)
            with self.subTest(code=code):
                done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": code})
                self.assertEqual((done.returncode, seen["argv"]), (7, ["pr", "merge", "12", "-R", "acme/app"]))
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "0", "FAKE_GATE_JSON": "not json"})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"])

    def test_an_enforcing_merge_is_bound_to_the_checked_head(self):
        self.write_gates({"acme/app": {"mode": "enforce"}})
        ok = {"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "enforce", "head": self.HEAD})}
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--squash", env=ok)
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--squash", "--match-head-commit", self.HEAD])
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit", self.HEAD, env=ok)
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--match-head-commit", self.HEAD], "the caller's own binding stands")
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit=" + "b" * 40, env=ok)
        self.assertEqual((done.returncode, seen), (10, None), "a binding to another head is refused")
        self.assertIn("not the head the gates were checked at", done.stderr)
        done, seen = self.run_shim("pr", "ready", "12", "-R", "acme/app", env=ok)
        self.assertEqual(seen["argv"], ["pr", "ready", "12", "-R", "acme/app"], "ready is not bound")
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_JSON": json.dumps({"mode": "enforce"})})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"], "no head known: fail-open, recorded")
        self.assertEqual(self.spool()[-1]["event"], "fail_open")

    def test_auto_merge_is_refused_only_when_enforcing(self):
        enforcing = {"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "enforce", "head": self.HEAD})}
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--auto", env=enforcing)
        self.assertEqual((done.returncode, seen), (10, None))
        self.assertIn("--auto", done.stderr)
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--auto",
                                   env={"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "shadow", "head": self.HEAD})})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--auto"], "shadow never refuses and never rewrites")

    ENFORCE = {"FAKE_GATE_JSON": json.dumps({"verdict": "allow", "mode": "enforce", "head": "a" * 40})}

    def test_the_head_binding_cannot_be_dodged_by_how_the_flag_is_spelled(self):
        head = "a" * 40
        for extra, want in ((["--match-head-commit", ""], []), (["--match-head-commit="], []), (["--match-head-commit"], []),
                            (["--match-head-commit", head.upper()], []), (["--match-head-commit=" + head, "--match-head-commit", ""], []),
                            (["--match-head-commit", "", "--match-head-commit", head], [])):
            with self.subTest(extra=extra):
                done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", *extra, env=self.ENFORCE)
                self.assertEqual(done.returncode, 7)
                self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app", "--match-head-commit", head])
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit", "a", env=self.ENFORCE)
        self.assertEqual((done.returncode, seen), (10, None), "a prefix is not the head")
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", "--match-head-commit", "b" * 40, "--match-head-commit=" + head, env=self.ENFORCE)
        self.assertEqual((done.returncode, seen), (10, None), "every given value must be the checked head")

    def test_a_bare_pr_number_uses_the_repo_the_real_gh_would(self):
        self.write_gates({"acme/app": {"mode": "enforce"}})
        done, seen = self.run_shim("pr", "merge", "5", env=dict(self.ENFORCE, FAKE_REPO="other/place"))
        self.assertEqual(self.gate_calls(), [], "the directory's repo is not listed: not asked, not rewritten")
        self.assertEqual(seen["argv"], ["pr", "merge", "5"])
        done, seen = self.run_shim("pr", "merge", "5", env=dict(self.ENFORCE, FAKE_REPO="acme/app"))
        self.assertEqual(self.gate_calls()[-1], "gates-check acme/app#5 --for merge --json own=")
        self.assertEqual(seen["argv"][-2:], ["--match-head-commit", "a" * 40])
        done, seen = self.run_shim("pr", "merge", "5", env=dict(self.ENFORCE, GH_REPO="acme/app"))
        self.assertEqual(self.gate_calls()[-1], "gates-check acme/app#5 --for merge --json own=", "GH_REPO is honoured")
        self.calls_log.unlink()
        done, seen = self.run_shim("pr", "merge", "5", env=self.ENFORCE)   # the real gh cannot say which repo: never guess the fleet repo
        self.assertEqual((done.returncode, self.gate_calls()), (7, []))
        self.assertIn("could not resolve the repo", self.spool()[-1]["detail"])

    def test_ordinary_spellings_are_understood(self):
        head = "a" * 40
        for args in (("pr", "merge", "-sd", "5", "-R", "acme/app"), ("pr", "merge", "5", "-R", "acme/app", "-sd"),
                     ("pr", "merge", "5", "--squash=true", "-R", "acme/app"), ("pr", "merge", "5", "-Racme/app"),
                     ("pr", "merge", "5", "--repo=acme/app", "--delete-branch=false"),
                     ("pr", "merge", "https://github.com/acme/app/pull/5/files")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env=self.ENFORCE)
                self.assertEqual(self.gate_calls()[-1], "gates-check acme/app#5 --for merge --json own=")
                self.assertEqual(seen["argv"], list(args) + ["--match-head-commit", head])
        for args in (("pr", "merge", "5", "-R", "acme/app", "--auto=true"), ("pr", "merge", "5", "-R", "acme/app", "-sd", "--auto")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env=self.ENFORCE)
                self.assertEqual((done.returncode, seen), (10, None))
        done, seen = self.run_shim("pr", "merge", "5", "-R", "acme/app", "--auto=false", env=self.ENFORCE)
        self.assertEqual(seen["argv"][-2:], ["--match-head-commit", head], "--auto=false is not auto")

    def test_no_argument_and_a_branch_name_are_resolved_by_the_real_gh_or_recorded(self):
        for args in (("pr", "merge"), ("pr", "merge", "my-branch", "-R", "acme/app"), ("pr", "ready")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args, env=dict(self.ENFORCE, FAKE_PRURL="https://github.com/acme/app/pull/8"))
                self.assertEqual(self.gate_calls()[-1].split()[1], "acme/app#8")
                self.assertEqual(seen["argv"][:len(args)], list(args))
        self.calls_log.unlink()
        done, seen = self.run_shim("pr", "merge", env=self.ENFORCE)
        self.assertEqual((done.returncode, self.gate_calls()), (7, []))
        self.assertIn("could not resolve the PR", self.spool()[-1]["detail"])
        done, seen = self.run_shim("pr", "ready", "12", "--undo", "-R", "acme/app", env=self.ENFORCE)
        self.assertIn("shape not understood", self.spool()[-1]["detail"])

    def test_a_refusal_is_printed_without_control_characters_and_only_for_an_enforce_reply(self):
        reply = json.dumps({"mode": "enforce", "message": "bad \x1b]0;title\x07 \x1b[2Jtext"})
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": reply})
        self.assertEqual((done.returncode, done.stderr), (10, "bad ]0;title [2Jtext\n"))
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app",
                                   env={"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"mode": "shadow", "message": "x"})})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"], "exit 10 from a shadow reply does not refuse")

    def test_a_check_that_hangs_costs_at_most_the_bound_and_the_command_still_runs(self):
        done, seen = self.run_shim("pr", "ready", "12", "-R", "acme/app", env={"FAKE_GATE_SLEEP": "30", "FLEET_SHIM_GATE_SECONDS": "1"})
        self.assertEqual(seen["argv"], ["pr", "ready", "12", "-R", "acme/app"])
        self.assertLess(self.elapsed, 6)
        (entry,) = self.spool()
        self.assertEqual(entry["event"], "fail_open")
        self.assertIn("no answer within 1s", entry["detail"])

    def test_the_bound_is_clamped_and_defaults_to_three_seconds(self):
        source = SHIM.read_text()
        self.assertIn("GATE_SECONDS = 3.0", source)
        done, seen = self.run_shim("pr", "ready", "12", "-R", "acme/app", env={"FAKE_GATE_SLEEP": "30", "FLEET_SHIM_GATE_SECONDS": "0.01"})
        self.assertLess(self.elapsed, 25, "a bound below 0.2 s is raised to 0.2 s, not 0")

    def test_with_no_switchboard_the_command_runs_and_the_gap_is_recorded(self):
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", binary=False, env={"HOME": str(self.home / "nohome")})
        self.assertEqual(seen["argv"], ["pr", "merge", "12", "-R", "acme/app"])

    def test_shapes_it_does_not_understand_pass_through_and_are_not_probed(self):
        for args in (("pr", "merge"), ("pr", "merge", "my-branch", "-R", "acme/app"), ("pr", "ready", "12", "--undo", "-R", "acme/app"),
                     ("pr", "merge", "12", "13", "-R", "acme/app"), ("pr", "merge", "12", "--mystery", "-R", "acme/app"),
                     ("pr", "merge", "12", "-R", "not a repo"), ("pr", "merge", "--", "12"), ("pr", "ready", "0", "-R", "acme/app"),
                     ("pr", "create", "--title", "t", "--body", "b"), ("pr", "view", "12", "-R", "acme/app"),
                     ("pr", "merge", "https://example.com/acme/app/pull/12")):
            with self.subTest(args=args):
                done, seen = self.run_shim(*args)
                if args[1] != "view":
                    self.assertEqual(seen["argv"][:2], list(args)[:2])
                self.assertEqual(self.gate_calls(), [])

    def test_a_repo_not_listed_is_not_probed_and_the_match_ignores_case(self):
        self.run_shim("pr", "merge", "12", "-R", "acme/other")
        self.assertEqual(self.gate_calls(), [])
        self.write_gates({"Acme/App": {"mode": "shadow"}})
        self.run_shim("pr", "merge", "12", "-R", "ACME/app")
        self.assertEqual(len(self.gate_calls()), 1)
        self.write_gates({})
        self.calls_log.unlink()
        self.run_shim("pr", "merge", "12", "-R", "acme/app")
        self.assertEqual(self.gate_calls(), [], "no plan_gates in the config: nothing is probed")
        self.write_gates(None)
        self.run_shim("pr", "merge", "12", "-R", "acme/app")
        self.assertEqual(self.gate_calls(), [])

    def test_a_fork_pr_url_is_asked_about_under_its_own_repo(self):
        self.write_gates({"acme/app": {"mode": "shadow"}})
        self.run_shim("pr", "merge", "https://github.com/someone/app/pull/9")
        self.assertEqual(self.gate_calls(), [], "a fork's PR is another repo: not listed, not probed")

    def mutant_shim(self, func):
        """A copy of the whole shim directory in which `func` honours the marker again (returns at once for it); SHIM points at it while the case runs."""
        source = SHIM.read_text()
        head = source[source.index("def %s(" % func):]
        at = head.index(":\n", head.index(")")) + 2
        guard = '    if os.environ.get("FLEET_SWITCHBOARD_OWN_GH") == "1":\n        return None\n'
        if source[source.index("def %s(" % func) + at - 2 - 1:][:0] == "":
            pass
        # a docstring comes first: put the guard after it
        body = head[at:]
        stripped = body.lstrip()
        if stripped.startswith('"""'):
            end = body.index('"""', body.index('"""') + 3) + 3
            at += end + 1
        mutant = source.replace(head, head[:at] + guard + head[at:], 1)
        ast.parse(mutant)
        target = self.home / "mutant-shims"
        shutil.copytree(SHIM.parent, target)
        (target / "gh").write_text(mutant)
        return target / "gh"

    def test_mutants_that_honour_the_marker_again_fail_the_cases(self):   # #1216 S6: review probe and gate probe
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        self.write_gates({"acme/app": {"mode": "enforce"}})
        gate = {"FLEET_SWITCHBOARD_OWN_GH": "1", "FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": self.REFUSAL})}
        for func, run in (("review_probe", lambda: self.merge(reviews=block, env={"FLEET_SWITCHBOARD_OWN_GH": "1"})),
                          ("gate_probe", lambda: self.run_shim("pr", "merge", "12", "-R", "acme/app", env=gate))):
            with self.subTest(func=func):
                real = self.run_shim_result_code(run)
                self.assertEqual(real, 10, "the real shim refuses")
                mutant = self.mutant_shim(func)
                try:
                    with mock.patch.dict(globals(), {"SHIM": mutant}), rebound("SHIM", mutant):
                        self.assertNotEqual(self.run_shim_result_code(run), 10, "the mutant lets the merge run")
                finally:
                    shutil.rmtree(self.home / "mutant-shims", ignore_errors=True)

    def run_shim_result_code(self, run):
        done, seen = run()
        return done.returncode

    def test_table_bad_inputs_behave_the_same_with_the_marker_set_and_unset(self):   # #1216: the marker changes nothing for any bad input
        block = [self.review("CHANGES_REQUESTED", "reviewer-a")]
        self.write_gates({"acme/app": {"mode": "enforce"}})
        table = (("no switchboard", dict(binary=False), {}),
                 ("reviews not json", {}, {"FAKE_REVIEWS": "not json"}),
                 ("reviews call error", {}, {"FAKE_REVIEWS_EXIT": "3"}),
                 ("blockers call error", {}, {"FAKE_BLOCKERS_EXIT": "5"}),
                 ("gate call error", {}, {"FAKE_GATE_EXIT": "9"}),
                 ("gate wrong type", {}, {"FAKE_GATE_JSON": "[1]"}),
                 ("gate not json", {}, {"FAKE_GATE_JSON": "nope", "FAKE_GATE_EXIT": "10"}),
                 ("gate refuses", {}, {"FAKE_GATE_EXIT": "10", "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": "no"})}))
        for name, kw, env in table:
            with self.subTest(case=name):
                got = []
                for extra in ({}, {"FLEET_SWITCHBOARD_OWN_GH": "1"}):
                    done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env=dict(env, **extra), **kw)
                    got.append((done.returncode, seen is None))
                self.assertEqual(got[0], got[1], "the marker changes nothing")
        for extra in ({}, {"FLEET_SWITCHBOARD_OWN_GH": "1"}):   # a blocking review is refused either way
            done, seen = self.merge(reviews=block, env=extra)
            self.assertEqual((done.returncode, seen), (10, None))

    def test_the_marker_does_not_skip_the_gate_probe(self):   # #1216 S3: the gate probe does not honour the marker
        self.write_gates({"acme/app": {"mode": "enforce"}})
        done, seen = self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FLEET_SWITCHBOARD_OWN_GH": "1", "FAKE_GATE_EXIT": "10",
            "FAKE_GATE_JSON": json.dumps({"verdict": "refuse", "mode": "enforce", "message": self.REFUSAL})})
        self.assertEqual((done.returncode, seen), (10, None), done.stderr)
        self.assertEqual(len(self.gate_calls()), 1)

    def test_the_spool_is_private_clipped_and_bounded(self):
        self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "3"})
        path = self.state / "gate-failopen.jsonl"
        self.assertEqual(stat.S_IMODE(os.stat(str(path)).st_mode), 0o600)
        path.write_text("x" * 300000)
        self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "3"})
        self.assertEqual(len(path.read_text()), 300000, "a full spool takes no more lines")
        path.unlink()
        self.state.rmdir()
        self.run_shim("pr", "merge", "12", "-R", "acme/app", env={"FAKE_GATE_EXIT": "3"})   # no state dir: nothing written, nothing fails

    def test_signing_and_the_account_environment_are_untouched(self):
        done, seen = self.run_shim("pr", "comment", "3", "--body", "hello")
        self.assertIn("fleet-switchboard:from=worker", seen["argv"][-1])
        self.assertEqual(self.gate_calls(), [])


class TestClosedUnmergedLastPr(unittest.TestCase):
    """#1347: an ask whose last other pull request is closed unmerged with a per-PR scenario claimed by no merged one is flagged (`plan_ledger.closed_unclaimed`). Table-derived."""

    SCEN = [{"id": "S1", "proof": "test", "evaluator": "ci", "claim": "pr"}, {"id": "S2", "proof": "test", "evaluator": "ci", "claim": "pr"}, {"id": "S3", "proof": "test", "evaluator": "ci", "claim": "pr"}]
    BODY = {"line": "PLAN: acme/app#7\nCLAIMS: S1 S2", "rest": "PLAN: acme/app#7\nCLAIMS: S3", "none": "PLAN: acme/app#7"}

    def judge(self, peers, closed, fn=None):
        return (fn or sb.plan_ledger.closed_unclaimed)(self.SCEN, [(s, self.BODY[b]) for s, b in peers], [(9, self.BODY[b]) for b in closed])

    def table(self):
        """Every state of the ask's other pull requests: one merged claim in (line, rest, none, absent) x one closed claim (line, none, absent) x an open one (yes, no)."""
        for merged in ("line", "rest", "none", None):
            for closed in ("line", "none", None):
                for opened in (False, True):
                    expect = bool(closed and not opened and merged != "none" and (merged == "line" or closed == "line") and merged != "rest")
                    if merged == "rest":
                        expect = bool(closed and not opened)   # S1 S2 left: unclaimed
                    if merged is None:
                        expect = bool(closed == "line" and not opened)
                    yield merged, closed, opened, expect

    def check(self, fn=None):
        rows = list(self.table())
        wrong = []
        for merged, closed, opened, expect in rows:
            peers = ([("merged", merged)] if merged else []) + ([("open", "line")] if opened else [])
            got = bool(self.judge(peers, [closed] if closed else [], fn)[0])
            if got != expect:
                wrong.append((merged, closed, opened))
        return len(rows), wrong

    def test_s7_every_combination_gives_the_table_outcome(self):
        count, wrong = self.check()
        self.assertEqual((count, wrong), (24, []))

    def test_s2_the_closed_last_pr_with_an_unclaimed_scenario_is_flagged_naming_it_and_the_closed_number(self):
        self.assertEqual(self.judge([("merged", "rest")], ["line"]), (["S1", "S2"], [9]))

    def test_s3_u4_the_same_with_full_coverage_or_no_claim_lines_is_not_flagged(self):
        self.assertEqual(self.judge([("merged", "line"), ("merged", "rest")], ["line"]), ([], []))
        self.assertEqual(self.judge([("merged", "none")], ["none"]), ([], []))

    def narrowing_by_closed_claim(self, fn=None):
        self.assertEqual(self.judge([], ["line"], fn), (["S1", "S2", "S3"], [9]))   # only the closed pull request's claim line narrows the plan

    def test_s13_a_closed_prs_claim_line_counts_as_narrowing(self):
        self.narrowing_by_closed_claim()

    def test_s13_a_mutant_that_ignores_the_closed_claim_line_for_narrowing_fails(self):
        real = sb.plan_ledger.closed_unclaimed
        ignores = lambda scen, peers, closed: real(scen, peers, [(n, "PLAN: acme/app#7") for n, _ in closed])
        with self.assertRaises(AssertionError):
            self.narrowing_by_closed_claim(ignores)

    def test_s8_the_mutant_that_ignores_closed_prs_fails(self):
        ignore = lambda scen, peers, closed: sb.plan_ledger.closed_unclaimed(scen, peers, [])
        self.assertNotEqual(self.check(ignore)[1], [])

    def test_s8_the_mutant_that_counts_a_closed_claim_as_covered_fails(self):
        def counts(scen, peers, closed):
            return sb.plan_ledger.closed_unclaimed(scen, peers + [("merged", b) for _, b in closed], closed)
        self.assertNotEqual(self.check(counts)[1], [])

    def test_s4_the_refusal_names_the_closed_pull_request_and_the_waiver(self):
        text = sb.plan_ledger.union_refusal("acme/app#5", ["S3"], [9])
        self.assertIn("#9", text)
        self.assertIn("S3", text)
        self.assertIn("--gates CU", text)


class StubIssueHub:
    """The hub as the closed-issue pass sees it: the events it holds, and a fresh read per issue (`reads`: number -> dict, or an exception)."""

    def __init__(self, entries, reads):
        self.entries, self.reads, self.read_log = entries, reads, []

    def closed_issue_snapshot(self):
        return {ref: dict(entry) for ref, entry in self.entries.items()}

    def forget_awaiting(self, repo, number):
        self.forgotten = getattr(self, "forgotten", []) + [(repo, number)]

    def _read_one(self, repo, path, now, fresh=False):
        number = int(path.rsplit("/", 1)[1])
        self.read_log.append((repo, number, fresh))
        found = self.reads[number]
        if isinstance(found, Exception):
            raise found
        return dict(found)
