"""The macOS legs of a pull request (#1235): the planning job's decision, the gate pass's leg check, G4's reading, the concurrency group, the platform-path list.

The planning job is the REAL script of .github/workflows/test.yml, run in a temporary git repository (one case per event). A mutant is the real source with one
line replaced; each must fail the scenario written for it. Run with `python3 bin/test-workflow-split` (it loads these classes) or
`python3 -m unittest tests/switchboard/test_mac_legs.py`. Standard library only.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "bin"))
sys.path.insert(0, str(REPO / "tests" / "switchboard"))
from support.service_and_merge import FakeMergeGitHub  # noqa: E402

WORKFLOW = REPO / ".github" / "workflows" / "test.yml"
LIST = REPO / ".github" / "macos-platform-paths.txt"
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE, READING = load("merge_gate_t", REPO / "bin" / "merge_gate.py"), load("pr_reading_t", REPO / "bin" / "pr_reading.py")


def git(root, *argv):
    done = subprocess.run(["git", "-C", str(root), *argv], capture_output=True, text=True, env=ENV)
    assert done.returncode == 0, done.stderr
    return done.stdout


def plan_script(text):
    start = text.index("          python3 - <<'PY'\n") + len("          python3 - <<'PY'\n")
    end = text.index("\n          PY\n", start)
    return "python3 - <<'PY'\n" + "\n".join(l[10:] for l in text[start:end + len("\n          PY")].splitlines()) + "\n"


def planned(text, event, base="", draft=False, action="", labels=(), files=("docs/x.md",), refs=False):
    """The names of the `test` job's checks the real plan script gives for one event (a temporary repository whose origin/main holds the real list)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        git(root, "init", "-q", "-b", "main")
        (root / ".github").mkdir()
        (root / ".github" / "macos-platform-paths.txt").write_text(LIST.read_text())
        git(root, "add", "-A")
        git(root, "commit", "-qm", "base")
        git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
        if refs:
            git(root, "update-ref", "refs/remotes/origin/integration", "HEAD")
        git(root, "checkout", "-qb", "topic")
        for f in files:
            (root / f).parent.mkdir(parents=True, exist_ok=True)
            (root / f).write_text("x\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "change")
        out = root / "out.txt"
        out.write_text("")
        env = dict(ENV, EVENT=event, BASE=base, DRAFT="true" if draft else "false", ACTION=action, LABELS=json.dumps(list(labels)), GITHUB_OUTPUT=str(out))
        subprocess.run(["bash", "-c", plan_script(text)], cwd=root, env=env, check=True, capture_output=True)
        line = [l for l in out.read_text().splitlines() if l.startswith("matrix=")][-1]
    return sorted(r["name"] for r in json.loads(line[len("matrix="):]))


UBUNTU, MAC = "test (ubuntu-latest)", "test (macos-latest)"
# case name -> (arguments of `planned`, whether the macOS leg is in)
PLAN_CASES = {
    "a platform path": (dict(event="pull_request", base="main", files=("shims/gh",)), True),
    "a docs-only change": (dict(event="pull_request", base="main"), False),
    "the label needs-mac": (dict(event="pull_request", base="main", action="synchronize", labels=("needs-mac",)), True),
    "a reopened run": (dict(event="pull_request", base="main", action="reopened"), True),
    "a draft": (dict(event="pull_request", base="main", draft=True, files=("shims/gh",)), False),
    "a push to the default branch": (dict(event="push"), True),
    "the nightly": (dict(event="schedule"), True),
}


def decisions(text):
    return {name: (MAC in planned(text, **args) or any("macos-latest" in n for n in planned(text, **args))) for name, (args, _) in PLAN_CASES.items()}


class TestPlanningDecision(unittest.TestCase):
    def test_s2_each_case_gives_the_expected_matrix(self):
        got = decisions(WORKFLOW.read_text())
        self.assertEqual(got, {name: want for name, (_, want) in PLAN_CASES.items()})
        self.assertEqual((sum(1 for v in got.values() if v), len(got)), (5, 7))   # the plan's "4 in, 3 out" does not add up to 7: platform path, label, reopened, push, nightly are in; docs-only and draft are out

    def test_s2_ubuntu_is_in_every_case_and_a_docs_only_run_has_no_macos_name(self):
        text = WORKFLOW.read_text()
        for name, (args, _) in PLAN_CASES.items():
            self.assertTrue(any("ubuntu-latest" in n for n in planned(text, **args)), name)
        self.assertEqual(planned(text, **PLAN_CASES["a docs-only change"][0]), [UBUNTU])

    def test_s10_the_nightly_and_the_push_keep_both_macos_legs(self):
        text = WORKFLOW.read_text()
        self.assertEqual(planned(text, "push"), sorted([UBUNTU, MAC]))
        night = planned(text, "schedule", refs=True)
        self.assertEqual(len([n for n in night if "macos-latest" in n]), 2)

    def test_u1_a_pull_request_into_integration_keeps_its_rule(self):
        text = WORKFLOW.read_text()
        self.assertEqual(planned(text, "pull_request", base="integration", refs=True), [UBUNTU])
        self.assertEqual(planned(text, "pull_request", base="integration", files=("plugins/a.js",), refs=True), sorted([UBUNTU, MAC]))
        self.assertEqual(planned(text, "pull_request", base="integration", action="reopened", refs=True), [UBUNTU])

    def test_s2_the_stub_that_returns_both_macos_legs_for_every_case_fails(self):
        mutant = WORKFLOW.read_text().replace('oses = ["ubuntu-latest"] + (["macos-latest"] if macos else [])', 'oses = ["ubuntu-latest", "macos-latest"]', 1)
        self.assertNotEqual(mutant, WORKFLOW.read_text())
        self.assertNotEqual(decisions(mutant), {name: want for name, (_, want) in PLAN_CASES.items()})

    def test_s2_a_mutant_that_ignores_the_label_or_the_reopened_action_fails(self):
        text = WORKFLOW.read_text()
        for old, new in (('"needs-mac" in json.loads', '"needs-mac-x" in json.loads'), ('os.environ.get("ACTION") == "reopened"', 'os.environ.get("ACTION") == "nope"')):
            mutant = text.replace(old, new, 1)
            self.assertNotEqual(mutant, text)
            self.assertNotEqual(decisions(mutant), {name: want for name, (_, want) in PLAN_CASES.items()}, old)


    def test_pc5_a_pull_request_into_main_changing_a_newly_listed_os_path_runs_macos(self):
        text = WORKFLOW.read_text()
        for path in ("bin/switchboard/installer.py", "bin/github_census.py", "bin/guard_alert.py", "bin/spotlight_guard.py"):
            self.assertEqual(planned(text, "pull_request", base="main", files=(path,)), sorted([UBUNTU, MAC]), path)


class TestConcurrencyGroup(unittest.TestCase):
    """S5: the group model. A run's group and whether it cancels are read from the real expressions by evaluating the two the workflow writes."""

    @staticmethod
    def expressions(text):
        block = text[text.index("\nconcurrency:\n"):text.index("\njobs:\n")]
        lines = [l.strip() for l in block.splitlines() if l.strip().startswith(("group:", "cancel-in-progress:"))]
        return lines

    @staticmethod
    def model(lines, runs):
        """Apply the workflow's own rule in Python: the group suffix and the cancel flag come from the action, as the expressions say; returns the runs left in progress."""
        group_line, cancel_line = lines
        suffix = "-reopened" in group_line and "github.event.action == 'reopened'" in group_line
        guarded = "github.event.action != 'reopened'" in cancel_line
        groups = {}
        for run in runs:
            key = ("pr", run["action"] == "reopened" if suffix else False)
            cancels = run["event"] == "pull_request" and (run["action"] != "reopened" if guarded else True)
            for old in groups.get(key, []):
                if cancels:
                    old["cancelled"] = True
            groups.setdefault(key, []).append(dict(run, cancelled=False))
        return [r for rs in groups.values() for r in rs]

    RUNS = [{"id": 1, "event": "pull_request", "action": "synchronize"}, {"id": 2, "event": "pull_request", "action": "reopened"},
            {"id": 3, "event": "pull_request", "action": "synchronize"}]

    def test_s5_a_later_push_cancels_the_superseded_push_run_and_not_the_reopened_run(self):
        out = {r["id"]: r["cancelled"] for r in self.model(self.expressions(WORKFLOW.read_text()), self.RUNS)}
        self.assertEqual(out, {1: True, 2: False, 3: False})

    def test_s5_a_group_that_cancels_every_older_run_fails(self):
        mutant = WORKFLOW.read_text().replace("${{ github.event.action == 'reopened' && '-reopened' || '' }}", "", 1).replace(" && github.event.action != 'reopened'", "", 1)
        out = {r["id"]: r["cancelled"] for r in self.model(self.expressions(mutant), self.RUNS)}
        self.assertEqual(out[2], True)

    def test_s5_a_later_reopen_does_not_cancel_the_first_reopened_run(self):
        runs = [{"id": 1, "event": "pull_request", "action": "reopened"}, {"id": 2, "event": "pull_request", "action": "reopened"}]
        self.assertEqual([r["cancelled"] for r in self.model(self.expressions(WORKFLOW.read_text()), runs)], [False, False])


def rollup(entries):
    """A statusCheckRollup: entries are (check name, conclusion or None, run id)."""
    out = []
    for i, (name, conclusion, run) in enumerate(entries):
        out.append({"name": name, "workflowName": "test", "status": "COMPLETED" if conclusion else "IN_PROGRESS", "conclusion": (conclusion or "").upper(),
                    "startedAt": "2026-10-30T10:%02d:00Z" % (10 + run % 40), "completedAt": "2026-10-30T10:%02d:30Z" % (10 + run % 40),
                    "detailsUrl": "https://github.com/acme-org/widget-service/actions/runs/%d/job/%d" % (run, 100 + i)})
    return out


import ci_runs  # noqa: E402

ci_runs.READER = lambda repo, run: (1, "pull_request")   # the identity of a run: one workflow, so the newest check of each name decides


def legs(entries):
    return GATE.rollup_legs(ci_runs.newest(entries), entries)


ALL4 = (GATE.UBUNTU_LEGS + GATE.MAC_LEGS)


class TestGateLegs(unittest.TestCase):
    def test_s4_g4_reads_the_head_when_macos_ran_in_the_one_run(self):
        applies, ok, said = legs(rollup([(n, "success", 5) for n in ALL4]))
        self.assertEqual((applies, ok, "at the head" in said), (True, True, True))

    def test_s3_g4_reads_the_merge_ref_run_when_macos_ran_only_there(self):
        entries = [(n, "success", 5) for n in GATE.UBUNTU_LEGS] + [(n, "success", 9) for n in ALL4]
        applies, ok, said = legs(rollup(entries))
        self.assertEqual((applies, ok, "merge ref" in said), (True, True, True))

    def test_s3_g4_refuses_never_ran_skipped_and_cancelled(self):
        cases = {"never ran": [(n, "success", 5) for n in GATE.UBUNTU_LEGS],
                 "skipped leg": [(n, "success", 5) for n in ALL4[:3]] + [(ALL4[3], "skipped", 5)],
                 "cancelled leg": [(n, "success", 5) for n in ALL4[:3]] + [(ALL4[3], "cancelled", 5)],
                 "running leg": [(n, "success", 5) for n in ALL4[:3]] + [(ALL4[3], None, 5)]}
        for name, entries in cases.items():
            applies, ok, said = legs(rollup(entries))
            self.assertEqual((applies, ok), (True, False), name)
            self.assertIn("macos-latest", said)

    def test_g4_a_repository_without_these_legs_is_not_asked(self):
        self.assertEqual(legs(rollup([("build", "success", 5)]))[:2], (False, True))

    def test_g4_the_ledger_gate_says_which_it_found_and_refuses_an_absent_leg(self):
        plan_ledger = load("plan_ledger_t", REPO / "bin" / "plan_ledger.py")
        head = "a" * 40
        plan = {"result": "pass", "scenarios_parsed": [], "text": "", "plan_hash": "h"}
        def row(legs):
            report = plan_ledger.evaluate_gates(plan, [], None, {"head": head, "ci": "green", "legs": legs, "body": ""}, "acme-org/widget-service#1")
            return next(r for r in report["gates"] if r["gate"] == "G4")
        good = row(legs(rollup([(n, "success", 5) for n in ALL4])))
        self.assertEqual(good["status"], "ok")
        self.assertIn("macOS legs green at the head", good["detail"])
        bad = row(legs(rollup([(n, "success", 5) for n in GATE.UBUNTU_LEGS])))
        self.assertEqual(bad["status"], "missing")
        self.assertIn("absent", bad["missing"][0])

    def test_s6_the_mutant_that_reads_an_absent_leg_as_green_is_caught(self):
        source = (REPO / "bin" / "merge_gate.py").read_text()
        mutant = source.replace('bad = ["%s %s" % (n, states.get(n) or "absent") for n in MAC_LEGS if states.get(n) != "success"]',
                                'bad = ["%s %s" % (n, states.get(n) or "absent") for n in MAC_LEGS if n in states and states.get(n) != "success"]', 1)
        self.assertNotEqual(mutant, source)
        module = type(sys)("merge_gate_mutant")
        module.__dict__["__file__"] = str(REPO / "bin" / "merge_gate.py")
        exec(compile(mutant, "merge_gate_mutant", "exec"), module.__dict__)
        states = {n: "success" for n in GATE.UBUNTU_LEGS}
        self.assertEqual(GATE.judge_legs(states, False)[1], False)
        self.assertEqual(module.judge_legs(states, False)[1], True, "the mutant reads the absent legs as green: the real one does not")


class LegsWorld(FakeMergeGitHub):
    """The fake merge GitHub whose runs carry jobs: run 1 (the push run) has the Ubuntu legs, the reopened run 2 has `second`."""

    second = {n: "success" for n in ALL4}

    def __init__(self, first=None, **more):
        super().__init__(**more)
        self.jobs = {1: [{"name": n, "conclusion": "success"} for n in (first or GATE.UBUNTU_LEGS)]}

    def __call__(self, argv, own=True):
        before = list(self.runs)
        out = super().__call__(argv, own)
        if argv[0] == "api" and argv[1] == "-X" and argv[-1] == "state=open" and out[0] == 0:
            self.jobs[2] = [{"name": n, "conclusion": c} for n, c in self.second.items()]
            self.runs = before + self.runs
        return out


class TestGatePass(unittest.TestCase):
    def run_pass(self, fake):
        conf = GATE.settings({"merge_gate": {"repos": {fake.repo: {}}}})
        state = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        return GATE.gate_pass(READING, fake, conf, state, fake.repo, fake.number, "coder-a", os.getpid(), fake.clock, fake.sleep, alive=lambda pid: True)

    def test_s3_a_head_with_no_macos_run_is_reopened_and_passes_on_the_merge_ref_run(self):
        fake = LegsWorld()
        ok, lines = self.run_pass(fake)
        self.assertTrue(ok, lines)
        self.assertEqual(len(fake.writes), 2, "closed and reopened once")
        self.assertIn("merge ref", " ".join(lines))

    def test_s8_an_absent_skipped_or_cancelled_macos_leg_on_the_merge_ref_refuses_and_names_the_leg(self):
        for label, second in (("absent", {n: "success" for n in GATE.UBUNTU_LEGS}),
                              ("skipped", dict({n: "success" for n in ALL4}, **{GATE.MAC_LEGS[1]: "skipped"})),
                              ("cancelled", dict({n: "success" for n in ALL4}, **{GATE.MAC_LEGS[0]: "cancelled"}))):
            fake = LegsWorld()
            fake.second = second
            ok, lines = self.run_pass(fake)
            self.assertFalse(ok, label)
            self.assertIn("macos-latest) %s" % label, lines[-1], label)

    def test_s4_a_head_whose_run_had_the_macos_legs_passes_without_a_reopen(self):
        fake = LegsWorld(first=ALL4)
        ok, lines = self.run_pass(fake)
        self.assertTrue(ok, lines)
        self.assertEqual(fake.writes, [])
        self.assertIn("at the head", " ".join(lines))

    def test_s8_a_gate_pass_that_ignores_a_skipped_leg_is_caught(self):
        source = (REPO / "bin" / "merge_gate.py").read_text()
        mutant = source.replace('if applies and not legs_ok:', 'if False:', 1)
        self.assertNotEqual(mutant, source)
        module = type(sys)("merge_gate_mutant")
        module.__dict__["__file__"] = str(REPO / "bin" / "merge_gate.py")
        exec(compile(mutant, "merge_gate_mutant", "exec"), module.__dict__)
        fake = LegsWorld()
        fake.second = {n: "success" for n in GATE.UBUNTU_LEGS}
        conf = module.settings({"merge_gate": {"repos": {fake.repo: {}}}})
        state = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(state, ignore_errors=True))
        ok, _ = module.gate_pass(READING, fake, conf, state, fake.repo, fake.number, "coder-a", os.getpid(), fake.clock, fake.sleep, alive=lambda pid: True)
        self.assertTrue(ok, "the mutant passes an absent leg")


REQUIRED_GROUPS = {"shims/": lambda p: p.startswith("shims/"), "plugins/": lambda p: p.startswith("plugins/"),
                   "install and service files": lambda p: p in ("INSTALL.md", "bin/fleet-doctor", "bin/fleet-heartbeat")}


def list_problems(patterns):
    """The required groups with no entry in the list: one message each."""
    return ["no entry for " + g for g, test in REQUIRED_GROUPS.items() if not any(test(p) for p in patterns)]


def patterns_of(text):
    return [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


class TestPlatformPaths(unittest.TestCase):
    """The platform-path list. The OS-calling files are derived by ast (not a regular expression) and each must be listed or exempt with a reason.

    Known limits: a command whose first element is built at run time, and a tool called only through a variable, are not seen.
    """
    def test_s9_the_list_holds_shims_plugins_and_the_install_and_service_files(self):
        self.assertEqual(list_problems(patterns_of(LIST.read_text())), [])
        self.assertEqual(len(REQUIRED_GROUPS), 3)

    def test_s9_a_list_missing_a_group_fails_for_each_group(self):
        base = patterns_of(LIST.read_text())
        for group, test in REQUIRED_GROUPS.items():
            self.assertEqual(list_problems([p for p in base if not test(p)]), ["no entry for " + group], group)

    def test_s9_the_plan_script_reads_this_list_and_the_two_fixed_paths_stay_in_it(self):
        tree = ast.parse("\n".join(plan_script(WORKFLOW.read_text()).splitlines()[1:-1]))
        consts = {t.id: n.value.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) for t in n.targets if isinstance(t, ast.Name)}
        self.assertEqual(consts["LIST"], ".github/macos-platform-paths.txt")
        self.assertEqual(consts["WORKFLOWS"], ".github/workflows/")
        self.assertTrue(LIST.exists())


    # PC-5 of #1671. Known limits: a command whose first element is built at run time, and a tool called only through a variable, are not seen.
    OS_TOOLS = frozenset("launchctl osascript mdfind sysctl plutil notify-send pmset caffeinate diskutil scutil sw_vers vm_stat mdutil pgrep pkill defaults open ps lsof".split())
    REAL_ONLY = frozenset(("open", "defaults"))   # counted only as a real subprocess call (not as a word or an argv list)
    CALLS = frozenset(("subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "os.system", "os.popen"))
    PLATFORM = frozenset(("sys.platform", "os.name", "platform.system", "os.uname"))
    EXEMPT = {
        "bin/fleet-switchboard": "left out on purpose: almost every change touches it; the push to main and the nightly catch a regression",
        "bin/cloud_cluster.py": "portable ps -o ... -p PID call, which also exists on Linux",
        "bin/guard_record_head_move.py": "portable ps -o ... -p PID call, which also exists on Linux",
    }

    @staticmethod
    def dotted(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return ".".join(reversed(parts))
        return ""

    @classmethod
    def tool_of(cls, node):
        """The OS tool a literal names (a string, its first word) or ''."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.split():
            word = node.value.split()[0].rsplit("/", 1)[-1]
            return word if word in cls.OS_TOOLS else ""
        return ""

    @classmethod
    def os_calling(cls, source):
        """The (line, what) hits of one source text, by ast."""
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return []
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and cls.dotted(node.func) in cls.CALLS and node.args:
                first = node.args[0]
                tool = cls.tool_of(first)
                if not tool and isinstance(first, (ast.List, ast.Tuple)) and first.elts:
                    tool = cls.tool_of(first.elts[0])
                if tool:
                    hits.append((node.lineno, tool))
            elif isinstance(node, (ast.List, ast.Tuple)) and node.elts and isinstance(getattr(node, "ctx", None), ast.Load):
                tool = cls.tool_of(node.elts[0])
                if tool and tool not in cls.REAL_ONLY and len(node.elts) > 1:
                    hits.append((node.lineno, tool))
            elif isinstance(node, ast.Attribute) and cls.dotted(node) in cls.PLATFORM:
                hits.append((node.lineno, cls.dotted(node)))
        return hits

    @classmethod
    def derived(cls, root):
        """{repo-relative path: first hit} over the non-test files of bin/, cloud/, plugins/ and shims/ (python files and extensionless python scripts)."""
        found = {}
        for top in ("bin", "cloud", "plugins", "shims"):
            for path in sorted((root / top).rglob("*")) if (root / top).is_dir() else []:
                if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts or path.name.startswith(("test_", "test-")):
                    continue
                rel = path.relative_to(root).as_posix()
                if "/tests/" in "/" + rel:
                    continue
                try:
                    text = path.read_text()
                except (UnicodeDecodeError, OSError):
                    continue
                if not (path.suffix == ".py" or (not path.suffix and text.startswith("#!") and "python" in text.split("\n", 1)[0])):
                    continue
                hits = cls.os_calling(text)
                if hits:
                    found[rel] = min(hits)
        return found

    @classmethod
    def unlisted(cls, root, patterns):
        import fnmatch
        return {rel: hit for rel, hit in cls.derived(root).items()
                if not any(fnmatch.fnmatchcase(rel, p) for p in patterns) and len(cls.EXEMPT.get(rel, "")) < 20}

    def test_pc5_every_os_calling_file_is_listed_or_exempt_with_a_reason(self):
        found = self.unlisted(REPO, patterns_of(LIST.read_text()))
        self.assertEqual(found, {}, "OS-calling files neither on .github/macos-platform-paths.txt nor exempt: " + ", ".join("%s (line %d: %s)" % (f, h[0], h[1]) for f, h in found.items()))

    def test_pc5_every_exempt_entry_is_still_os_calling_and_unlisted(self):
        import fnmatch
        patterns, found = patterns_of(LIST.read_text()), self.derived(REPO)
        for rel, reason in self.EXEMPT.items():
            self.assertGreaterEqual(len(reason), 20, rel)
            self.assertIn(rel, found, rel + " no longer calls the OS: drop its exempt entry")
            self.assertFalse(any(fnmatch.fnmatchcase(rel, p) for p in patterns), rel + " is listed: drop its exempt entry")

    def test_pc5_the_derivation_counts_open_and_defaults_only_as_real_calls(self):
        self.assertEqual(self.os_calling('x = "open the door"\nprint(["open", "a"], "defaults")\n'), [])
        self.assertEqual(self.os_calling('import subprocess\nsubprocess.run(["open", "a"])\n'), [(2, "open")])
        self.assertEqual(self.os_calling('cmd = ["pmset", "-g"]\n'), [(1, "pmset")])
        self.assertEqual(self.os_calling('import os\nos.system("lsof -p 1")\n'), [(2, "lsof")])

    def test_pc5_mutant_a_listed_line_removed_is_caught_naming_the_file(self):
        text = LIST.read_text()
        for gone in ("bin/switchboard/installer.py", "bin/github_census.py", "bin/guard_alert.py", "bin/spotlight_guard.py", "bin/heavy_slots.py"):
            mutant = "\n".join(l for l in text.splitlines() if l.strip() != gone)
            self.assertEqual(sorted(self.unlisted(REPO, patterns_of(mutant))), [gone])

    def test_pc5_mutant_a_new_unlisted_file_calling_pmset_is_caught_naming_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            (root / "bin" / "new_tool.py").write_text('import subprocess\nsubprocess.run(["pmset", "-g"])\n')
            (root / "bin" / "plain").write_text("#!/usr/bin/env python3\nimport os\nprint(os.name)\n")
            (root / "bin" / "docs.py").write_text('print("open the door")\n')
            self.assertEqual(sorted(self.unlisted(root, patterns_of(LIST.read_text()))), ["bin/new_tool.py", "bin/plain"])


class TestDocs(unittest.TestCase):
    def test_s7_the_documentation_says_what_runs_when_what_g4_requires_and_the_label(self):
        text = (REPO / "docs" / "switchboard.md").read_text()
        section = text[text.index("**Which runs happen when (the test workflow).**"):]
        section = section[:section.index("\n\nA pull request retargeted")]
        statements = ["the label `needs-mac`" in section, "what G4 and the gate pass require" in section and "merge-ref run" in section,
                      "a `reopened` run" in section and "every leg" in section]
        self.assertEqual(sum(statements), 3)


if __name__ == "__main__":
    unittest.main()
