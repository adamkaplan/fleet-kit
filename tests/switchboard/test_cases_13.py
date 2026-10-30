"""Test classes moved out of bin/test-switchboard (text unchanged): cases_13."""
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
from support.core import Base
from support.loader import sb
from support.paths import BIN, REPO


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
        check("cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in top, "the concurrency rule is unchanged")
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
        check(key(run("pull_request", "main")) == both, "a pull request into main ends with both required results")
        check(key(run("pull_request", "main", files=("docs/other.md",))) == both, "a docs-only pull request into main still runs macOS")
        check(key(run("pull_request", "main", draft=True)) == ["test (ubuntu-latest)"], "a draft into main skips macOS")
        check(key(run("pull_request", "main", draft=False)) == both, "marked ready, macOS runs")
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
            ("macOS skipped for a pull request into main", swap("macos = not draft", "macos = False"), listtext),
            ("the draft rule dropped", swap("macos = not draft", "macos = True"), listtext),
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
            ("an unconditional cancel", swap("cancel-in-progress: ${{ github.event_name == 'pull_request' }}", "cancel-in-progress: true"), listtext),
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


class TestRegistryEndpoint(unittest.TestCase):
    """#1074 (slice 3 of #1059): an agent without a pane has an endpoint, a liveness rule and a route function (not yet used by send); the row projections come from one tuple."""

    LOCAL = {"session": "ses_l", "pane": "p_1", "role": "coder", "repo": "o/r", "workspace": "w", "cwd": "/x"}
    CLOUD = {"session": "ses_c", "role": "orchestrator", "kind": "orchestrator", "endpoint": "https://pod.example/agent"}

    def setUp(self):
        self.reg = sb.load_module("registry")
        self.mod = sb.load_module("agent_reconcile")
        self.dir = Path(tempfile.mkdtemp(prefix="ep"))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def audit(self):
        path = self.dir / "registry-audit.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def both(self):
        self.reg.upsert(self.dir, "local", self.LOCAL)
        self.reg.upsert(self.dir, "pod", self.CLOUD)

    def test_s2_a_registry_with_both_reads_whole(self):
        self.both()
        read = self.reg.read(self.dir)
        self.assertEqual((read["whole"], read["version"], sorted(read["agents"])), (True, 1, ["local", "pod"]))
        self.assertEqual(read["agents"]["pod"]["endpoint"], self.CLOUD["endpoint"])

    def test_s3_the_endpoint_reaches_every_projection_and_survives_retire_and_undo(self):
        self.both()
        row = self.reg.read(self.dir)["agents"]["pod"]
        self.assertIn("endpoint", self.reg.AGENT_FIELDS)
        self.assertIn("endpoint", self.reg.CHANGE_FIELDS)
        changed = dict(row, endpoint="https://pod2.example/agent")
        self.assertTrue(self.reg.upsert(self.dir, "pod", changed)["changed"])   # written when only the endpoint changes
        self.reg.retire(self.dir, "pod", "boss", "done")
        self.assertEqual(self.reg.read(self.dir)["retired"]["pod"]["record"]["endpoint"], "https://pod2.example/agent")
        self.reg.unretire(self.dir, "pod")
        self.assertEqual(self.reg.read(self.dir)["agents"]["pod"]["endpoint"], "https://pod2.example/agent")

    def test_s4_every_row_field_is_in_all_three_projections_or_exempt(self):
        names = {"discovery": self.reg.AGENT_FIELDS, "change": self.reg.CHANGE_FIELDS, "record": self.reg.RECORD_FIELDS}
        for field in self.reg.ROW_FIELDS:
            for projection, fields in names.items():
                self.assertTrue((field in fields) != ((field, projection) in self.reg.EXEMPT), "%s / %s" % (field, projection))
        for (field, projection), why in self.reg.EXEMPT.items():
            self.assertIn(field, self.reg.ROW_FIELDS)
            self.assertIn(projection, self.reg.PROJECTIONS)
            self.assertTrue(why)
        self.assertEqual(set(sb.RETIRED_RECORD_FIELDS), set(self.reg.RECORD_FIELDS) - set(self.reg.OPTIONAL_FIELDS))
        saved = self.reg.ROW_FIELDS
        self.addCleanup(setattr, self.reg, "ROW_FIELDS", saved)
        self.reg.ROW_FIELDS = saved + ("added",)   # a field added to the row and to no table is in all three (derivation) unless exempt: derive again
        self.assertIn("added", self.reg.fields_of("record"))

    def test_s5_u5_a_no_pane_row_is_removed_only_on_a_dead_verdict_of_a_whole_read(self):
        self.both()
        def run(check, whole=True):
            read = self.reg.read(self.dir)
            read["whole"] = whole
            return self.mod.reconcile_on_read(self.dir, read, lambda n, a: {"pane": True, "process": True}, self.reg, check)
        self.assertEqual(run(None), [])                                   # default: unknown
        self.assertEqual(run(lambda n, a: "unknown"), [])
        self.assertEqual(run(lambda n, a: "unreachable"), [])
        self.assertEqual(run(lambda n, a: 1 / 0), [])                     # a failed check
        self.assertEqual(run(lambda n, a: "dead", whole=False), [])       # a partial read
        self.assertEqual([l for l in self.audit() if l.get("event") == "reconcile_remove"], [])
        self.assertEqual(run(lambda n, a: "dead"), ["pod"])
        self.assertEqual(len([l for l in self.audit() if l.get("event") == "reconcile_remove"]), 1)
        self.assertEqual(sorted(self.reg.read(self.dir)["agents"]), ["local"])

    def test_s6_route_function(self):
        self.both()
        agents = self.reg.read(self.dir)["agents"]
        self.assertEqual(self.reg.route_for(agents["pod"]), {"via": "endpoint", "to": self.CLOUD["endpoint"]})
        self.assertEqual(self.reg.route_for(agents["local"]), {"via": "pane", "to": "p_1"})

    def test_s6_registry_route_function_and_local_baseline(self):
        self.both()
        self.assertEqual(sb.registry_route(self.dir, "pod"), {"via": "endpoint", "to": self.CLOUD["endpoint"]})
        self.assertEqual(sb.registry_route(self.dir, "local"), {"via": "pane", "to": self.LOCAL["pane"]})   # the stored baseline: byte for byte
        self.assertEqual(json.dumps(sb.registry_route(self.dir, "local"), sort_keys=True), '{"to": "%s", "via": "pane"}' % self.LOCAL["pane"])
        self.assertIsNone(sb.registry_route(self.dir, "nobody"))

    def test_u1_local_agents_only_keep_the_baseline(self):
        self.reg.upsert(self.dir, "local", self.LOCAL)
        row = self.reg.read(self.dir)["agents"]["local"]
        self.assertFalse(self.reg.upsert(self.dir, "local", row)["changed"])   # change detection: no churn
        self.reg.retire(self.dir, "local", "boss", "done")
        record = self.reg.read(self.dir)["retired"]["local"]["record"]
        self.assertNotIn("endpoint", record)
        self.reg.unretire(self.dir, "local")
        back = self.reg.read(self.dir)["agents"]["local"]
        self.assertNotIn("endpoint", back)
        self.assertEqual(sb.registry_route(self.dir, "local"), {"via": "pane", "to": self.LOCAL["pane"]})

    def _gaps(self, change_fields):
        """The (field, projection) pairs where a field is neither in the projection nor exempt (the completeness rule)."""
        names = {"discovery": self.reg.AGENT_FIELDS, "change": change_fields, "record": self.reg.RECORD_FIELDS}
        return [(f, p) for f in self.reg.ROW_FIELDS for p, fs in names.items() if f not in fs and (f, p) not in self.reg.EXEMPT]

    def test_s7_mutants_each_fail(self):
        import unittest.mock as mock
        self.both()
        self.assertEqual(self._gaps(self.reg.CHANGE_FIELDS), [])
        # mutant 1: a projection (the change test) left out of the tuple's derivation drops the endpoint
        left_out = tuple(f for f in self.reg.CHANGE_FIELDS if f != "endpoint")
        self.assertEqual(self._gaps(left_out), [("endpoint", "change")])
        # mutant 2: a no-pane row read as dead is removed with no evidence: the real code keeps it
        self.assertEqual(self.mod.reconcile_on_read(self.dir, self.reg.read(self.dir), lambda n, a: {"pane": True, "process": True}, self.reg, None), [])
        # mutant 3: a route ignoring the endpoint returns None for the endpoint row: the route test names the endpoint
        with mock.patch.object(self.reg, "route_for", lambda row: {"via": "pane", "to": row.get("pane")} if row.get("pane") else None):
            self.assertNotEqual(sb.registry_route(self.dir, "pod"), {"via": "endpoint", "to": self.CLOUD["endpoint"]})
        with mock.patch.object(self.mod, "unknown_endpoint_check", lambda n, a: "dead"):   # the mutant removes the row the real default keeps
            self.assertEqual(self.mod.reconcile_on_read(self.dir, self.reg.read(self.dir), lambda n, a: {"pane": True, "process": True}, self.reg, None), ["pod"])

    def test_s8_the_docs_state_the_endpoint_the_liveness_rule_and_the_route_rule(self):
        text = (Path(__file__).resolve().parent.parent / "docs" / "agent-registry.md").read_text()
        self.assertEqual(text.count("## Additive note (#1074)"), 1)
        for item in ("**Endpoint field.**", "**Liveness rule.**", "**Route rule.**"):
            self.assertIn(item, text)

    def test_u2_a_credential_shaped_endpoint_is_refused(self):
        for bad in ("https://user:pw@pod.example/a", "https://pod.example/a?token=abc"):
            got = self.reg.upsert(self.dir, "pod", dict(self.CLOUD, endpoint=bad))
            self.assertTrue(got["problem"], bad)
        self.assertEqual(self.reg.read(self.dir)["agents"], {})

    def test_u3_a_row_without_an_endpoint_gains_no_key(self):
        self.reg.upsert(self.dir, "local", self.LOCAL)
        self.assertNotIn("endpoint", self.reg.read(self.dir)["agents"]["local"])
        self.assertEqual(self.reg.read(self.dir)["version"], 1)

    def test_u7_no_pane_is_bound_to_an_endpoint_row(self):
        self.both()
        got = self.reg.upsert(self.dir, "pod", dict(self.CLOUD, pane="p_9"))
        self.assertTrue(got["problem"])
        self.assertIsNone(self.reg.read(self.dir)["agents"]["pod"]["pane"])


class TestApprovalFinderCost(Base):
    """#1463: the refusal finder reads a message ONCE (ranges built once, `_sightings` once per `check_approval`), counted, not timed; and it
    agrees with the old per-form algorithm (kept here as the reference) on a table of hostile messages."""
    H = "a" * 64
    G = "b" * 64

    def module(self):
        return sb.load_module("plan_approval")

    # the OLD algorithm, per form, kept as the reference
    def old_refusal(self, text, pos):
        module = self.module()
        def line_of(t, p):
            return t[t.rfind("\n", 0, p) + 1:(t.find("\n", p) if t.find("\n", p) >= 0 else len(t))]
        def code_span(t, p):
            return any(m.start() <= p < m.end() for m in re.finditer(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", t, re.DOTALL))
        def fenced(t, p):
            fence, offset = None, 0
            for line in t.splitlines(True):
                mark = module.FENCE_RE.match(line)
                inside = fence is not None
                if mark:
                    fence = None if fence == mark.group(1) else (fence or mark.group(1))
                if offset <= p < offset + len(line):
                    return inside and not mark
                offset += len(line)
            return False
        def between(opening, closing):
            def test(t, p):
                line = line_of(t, p)
                col = p - (t.rfind("\n", 0, p) + 1)
                before, after = line[:col], line[col:]
                if opening == closing:
                    return before.count(opening) % 2 == 1 and closing in after
                return before.rfind(opening) > before.rfind(closing) and opening in before and closing in after
            return test
        def negation(word):
            body = r"n['\u2019]t" if word == "n't" else r"\s+".join(re.escape(w) for w in word.split())
            pattern = re.compile((r"(?<![\w'\u2019])" if word != "n't" else r"(?<=\w)") + body + r"(?![\w'\u2019])", re.IGNORECASE)
            return lambda t, p: pattern.search(" ".join(t[t.rfind("\n", 0, p) + 1:p].split()[-5:])) is not None
        tests = [negation(w) for w in module.NEGATION_WORDS] + [lambda t, p: line_of(t, p).lstrip().startswith(">"), code_span, fenced,
                                                                 between('"', '"'), between("\u201c", "\u201d")]
        return any(t(text, pos) for t in tests)

    def old_sightings(self, text):
        module = self.module()
        spans = []
        lead = module.APPROVAL_RE.match(text)
        if lead is not None:
            spans.append((lead.start(1) - len("Approved at sha256:"), module.approval_value(text), None))
        spans += [(m.start(), m.group(1), None) for m in module.ANYWHERE_RE.finditer(text)]
        colon = module.APPROVED_COLON_RE.search(text)
        if colon:
            for m in module.LIST_RE.finditer(text, colon.end()):
                spans.append((m.start(), m.group(3), (m.group(1), int(m.group(2)))))
        clean, refused = [], []
        for span in sorted(spans, key=lambda x: x[0]):
            (refused if self.old_refusal(text, span[0]) else clean).append(span)
        return clean, refused

    def hostile(self):
        A, B = "Approved at sha256:" + self.H, "Approved at sha256:" + self.G
        words = ["not", "no", "never", "don't", "do not", "did not", "cannot", "without", "not yet", "nothing"]
        out = ["%s %s" % (w, A) for w in words]
        out += ["`%s`" % A, "``%s``" % A, "`stray\n%s\n` and %s" % (A, B), "```\n%s\n```\n%s" % (A, B), "~~~\n%s\n```\n%s\n~~~" % (A, B),
                "> %s\n%s" % (A, B), "\"%s\" %s" % (A, B), "\u201c%s\u201d %s" % (A, B), "\"%s %s" % (A, B), "x\" %s \"y" % A,
                "\u200bnot %s" % A, "n\u043ft %s" % A, "%s, not yet" % A, "%s %s" % (A, B), "Approved: #7 at sha256:%s; #8 at sha256:%s" % (self.H, self.G),
                "`Approved: #7 at sha256:%s`; #8 at sha256:%s" % (self.H, self.G), "no\n%s" % A, "a b c d e no f %s" % A, "no a b c d e %s" % A,
                "```\n```\n%s" % A, "  > %s" % A, "`a` %s `b`" % A, "``a` %s `b``" % A, "%s\n" % A * 3, "\u201cx\u201d \u201d%s" % A,
                "Approved at sha256:" + "c" * 10, "", "hello", "%s\n`\n%s\n`" % (A, B), "No issues found. %s" % A]
        return out

    def test_the_new_finder_equals_the_old_per_form_algorithm_on_40_hostile_messages(self):
        module = self.module()
        messages = self.hostile()
        self.assertGreaterEqual(len(messages), 40)
        for text in messages:
            self.assertEqual(module._sightings(text), self.old_sightings(text), repr(text))
            old_clean, old_refused = self.old_sightings(text)
            self.assertEqual(module.refused_forms(text), [v for _, v, _ in old_refused], repr(text))
            self.assertEqual([f["value"] for f in module.find_approvals(text)],
                             list(dict.fromkeys((v.lower() if module.HEX64_RE.match(v) else v) for _, v, _ in old_clean)), repr(text))

    def big_messages(self):
        form = "Approved at sha256:%064x"
        plain = " ".join(form % i for i in range(3000))
        fenced = "```\n" + "\n".join(form % i for i in range(3000)) + "\n```"
        spans = " ".join("`" + form % i + "`" for i in range(3000))
        return {"plain": plain, "fenced": fenced, "spans": spans}

    def count_one_check(self, text):
        module = self.module()
        calls = []
        real = module._sightings
        before = module.Ranges.built
        with mock.patch.object(module, "_sightings", lambda *a, **k: calls.append(1) or real(*a, **k)):
            sb.check_approval({}, None, None, text, "acme/fleet", None)
        return len(calls), module.Ranges.built - before

    def test_3000_forms_are_read_once_per_message_counted_and_a_per_form_rescan_is_caught(self):
        module = self.module()
        for kind, text in self.big_messages().items():
            self.assertEqual(self.count_one_check(text), (1, 1), kind)
            self.assertGreaterEqual(sum(map(len, module._sightings(text))), 3000, kind)

            real = module.refusal_of
            small = "\n".join(text.split("\n")[:30]) if kind == "fenced" else " ".join(text.split(" ")[:90])
            with mock.patch.object(module, "refusal_of", lambda t, pos, ranges=None: real(t, pos, module.Ranges(t))):   # MUTANT: a rescan per form
                self.assertGreater(self.count_one_check(small)[1], 3, kind)
            self.assertEqual(self.count_one_check(small)[1], 1, kind)

    def test_the_documentation_states_the_four_limits_of_the_refusals(self):
        text = (BIN.parent / "docs" / "switchboard.md").read_text()
        section = text[text.index("**An approval is checked when it is sent (#951)"):]
        section = section[:section.index("\n\n")]
        for words in ("zero-width character or a homoglyph", "odd number of quotes", "negation AFTER the form", "stray backtick pairs with a later one across lines"):
            self.assertIn(words, section)


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
