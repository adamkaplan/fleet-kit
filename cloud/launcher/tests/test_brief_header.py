"""Ask #1046: a standard operator brief header, and a launch that refuses a report-to name nobody can answer to. Offline: fake cluster pieces,
fake registry in a temporary state directory. Scenarios: S2 S3 (the header), S4 S5 (the check), S6 (mutants), S7 (documentation), S9 S11 (cloud
shape and temporary rules), U2 (no secret), U3 (the one constant in one place)."""
import io
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)

SOURCE = os.path.join(ROOT, "skills", "fleet-coordination", "operator-brief-header.txt")
PLANTED = "planted-" + "Hq7Lz2" * 5   # a fake setting value
SB = os.path.join(ROOT, "bin", "fleet-switchboard")


def read(path):
    with open(os.path.join(ROOT, path)) as f:
        return f.read()


def rules_of(path):
    found = []
    for line in open(path).read().splitlines():
        if line.strip() and not line.startswith("#"):
            ident, _, text = line.partition(" | ")
            found.append((ident.strip(), text.strip()))
    return found


def load_sb():
    import importlib.machinery
    import importlib.util
    os.environ.setdefault("FL" + "EET_SWITCHBOARD_UNDER_TEST", "1")
    loader = importlib.machinery.SourceFileLoader("cw_sb_1046", SB)
    spec = importlib.util.spec_from_loader("cw_sb_1046", loader)
    sb = importlib.util.module_from_spec(spec)
    sys.modules["cw_sb_1046"] = sb
    loader.exec_module(sb)
    return sb


def run_header(*extra, env=None):
    home = tempfile.mkdtemp()
    e = dict(os.environ, HOME=home, CW_CLUSTER_ADMIN_CMD=PLANTED, CW_CLUSTER_ADMIN_SECRET=PLANTED)
    e.update(env or {})
    done = subprocess.run([sys.executable, SB, "brief", "header", "--role", "operator", *extra], capture_output=True, text=True, env=e, timeout=60)
    return done.returncode, done.stdout, done.stderr


class Header(unittest.TestCase):
    def test_s2_every_rule_of_the_source_is_printed_once_and_no_hash_is_pinned(self):
        code, out, err = run_header()
        self.assertEqual(code, 0, err)
        rules = rules_of(SOURCE)
        self.assertGreaterEqual(len(rules), 8)
        for ident, text in rules:
            self.assertEqual(out.count("[%s]" % ident), 1, ident)
            self.assertEqual(out.count(text), 1, ident)
        self.assertIsNone(re.search(r"\b[0-9a-f]{7,40}\b", out), "no checkout hash")

    def test_s2_the_text_is_derived_from_the_source_a_removed_rule_is_gone_from_the_output(self):
        sb = load_sb()
        with tempfile.TemporaryDirectory() as work:
            path = os.path.join(work, "header.txt")
            lines = open(SOURCE).read().splitlines(True)
            kept = [l for l in lines if not l.startswith("delete-exact |")]
            self.assertEqual(len(kept), len(lines) - 1)
            with open(path, "w") as f:
                f.write("".join(kept))
            text = sb.brief_header_text(path)
        self.assertNotIn("[delete-exact]", text)
        self.assertIn("[stop-surprise]", text)

    def test_s3_the_route_is_named_by_key_and_no_value_is_printed(self):
        code, out, err = run_header()
        self.assertIn("CW_CLUSTER_ADMIN_CMD", out)
        self.assertIn("CW_CLUSTER_ADMIN_SECRET", out)
        self.assertNotIn(PLANTED, out + err)   # U2 too: the environment value never reaches the output

    def test_the_rules_say_what_the_ask_names(self):
        ids = [i for i, _ in rules_of(SOURCE)]
        for ident in ("route", "no-pin", "secret-print", "secret-dir", "secret-shred", "mint-null", "delete-exact", "stop-surprise"):
            self.assertIn(ident, ids)
        text = " ".join(t for _, t in rules_of(SOURCE))
        for word in ("/dev/null", "mode 700", "exact name", "stop and report"):
            self.assertIn(word, text)


NAME_ALIAS = "chief-of-staff"


def make_state(agents=(), retired=()):
    state = tempfile.mkdtemp()
    sb = load_sb()
    registry = sb.load_module("registry")
    for name in agents:
        registry.upsert(state, name, {"session": "ses_example_" + name, "pane": None, "role": "coder", "repo": None, "workspace": None, "cwd": None, "charter": None})
    for name in retired:
        registry.upsert(state, name, {"session": "ses_example_" + name, "pane": None, "role": "coder", "repo": None, "workspace": None, "cwd": None, "charter": None})
        registry.retire(state, name, "test", "done")
    return sb, state


class Sentinel(Exception):
    pass


class LaunchHarness(unittest.TestCase):
    """The real cloud `_launch`, stopped at the launcher lookup (a Sentinel there means: everything before it passed, nothing was created)."""

    def run_launch(self, boss, agents=(), retired=(), brief="plain brief", hard=None, stack=False, shape=None, override=None, extra=None, settings=None):
        sb, state = make_state(agents, retired)
        b = sb.load_module("cloud_worker")
        audit = []

        class Eng:
            actor = "test"
            audit = types.SimpleNamespace(append=lambda actor, event, name, detail: audit.append((event, name, detail)))

            def discover(self, now):
                return types.SimpleNamespace(agents={})

            def now(self):
                return 0

        class LaunchError(Exception):
            pass

        lib = types.SimpleNamespace(check_repo_preflight=lambda repo, env: "ok")

        def stop():
            raise Sentinel()
        h = dict(sb.__dict__, LaunchError=LaunchError, PROG="sb", launch_repo=lambda a: a.repo, build_engine=lambda r, n, c: (Eng(), state, None),
                 read_brief=lambda f, i: brief)
        saved = {k: getattr(b, k) for k in ("launcher_argv", "use_settings", "check_allowed", "_lib", "load_record", "H", "stack_model_secret")}
        args = types.SimpleNamespace(name="tester7", resume=False, headless=False, agent="worker", reports_to=boss, dir=None, repo="acme-org/widget-service",
                                     branch="work-branch", shape=shape, idle=None, hard=hard, brief_file=None, stack=stack, boss_unregistered=override,
                                     role=None, issue=None, charter=None)
        for k, v in (extra or {}).items():
            setattr(args, k, v)
        err = io.StringIO()
        result = "reached"
        try:
            b.launcher_argv, b.use_settings, b.check_allowed = stop, (settings or (lambda: "test")), (lambda repo: None)
            b._lib, b.load_record, b.H, b.stack_model_secret = (lambda: lib), (lambda st, n: None), h, (lambda env=None: None)
            try:
                with redirect_stderr(err), redirect_stdout(io.StringIO()):
                    b._launch(args, None, lambda: 0, lambda st: None)
            except Sentinel:
                result = "reached"
            except LaunchError as problem:
                result = "refused: %s" % problem
        finally:
            for k, v in saved.items():
                setattr(b, k, v)
        return result, err.getvalue(), audit


class Check(LaunchHarness):
    def test_s4_a_name_no_registry_entry_knows_is_refused_before_anything_is_created(self):
        result, err, audit = self.run_launch("lane-nobody", agents=("lane-a",))
        self.assertTrue(result.startswith("refused:"), result)
        self.assertIn("not a registered identity", result)
        self.assertIn("nothing was created", result)
        self.assertIn("lane-a", result)               # how to register or which name to use: the nearest registered names
        self.assertIn("--boss-unregistered", result)

    def test_s5_a_registered_a_retired_name_and_the_role_alias_proceed(self):
        for boss, kw in (("lane-a", {"agents": ("lane-a",)}), ("lane-old", {"retired": ("lane-old",)}), (NAME_ALIAS, {}), (None, {})):
            with self.subTest(boss=boss):
                self.assertEqual(self.run_launch(boss, **kw)[0], "reached")

    def test_s5_the_override_with_a_reason_proceeds_and_writes_one_audit_line(self):
        result, err, audit = self.run_launch("lane-nobody", override="the lane starts after its boss")
        self.assertEqual(result, "reached")
        lines = [a for a in audit if a[0] == "launch.boss_unregistered"]
        self.assertEqual(len(lines), 1, audit)
        self.assertEqual(lines[0][2]["reports_to"], "lane-nobody")

    def test_s6_mutant_the_check_replaced_by_a_no_op_is_caught_by_the_refusal_test(self):
        sb, _ = make_state()
        b = sb.load_module("cloud_worker")
        original = b.unknown_report_to
        b.unknown_report_to = lambda *a, **k: (None, None)
        try:
            result, _, _ = self.run_launch("lane-nobody")
        finally:
            b.unknown_report_to = original
        self.assertEqual(result, "reached", "with the check gone the launch goes on: the refusal test above fails under this mutant")

    def test_a_registry_that_cannot_be_read_never_refuses_it_warns(self):
        sb, state = make_state()
        b = sb.load_module("cloud_worker")
        with open(os.path.join(state, "registry.json"), "w") as f:
            f.write("{not json")
        saved = dict(b.H)
        b.bind(sb.__dict__)
        try:
            refusal, warning = b.unknown_report_to(state, "lane-nobody", {}, NAME_ALIAS)
        finally:
            b.H.clear()
            b.H.update(saved)
        self.assertIsNone(refusal)
        self.assertIn("could not be read", warning)

    def test_the_local_route_checks_before_it_creates_anything(self):
        src = read("bin/fleet-switchboard")
        start = src.index("def cmd_launch(")
        body = src[start:src.index("cmd_launch.ensure_on_use", start)]
        check = body.index("check_report_to")
        self.assertLess(check, body.index("launch_agent(engine, args.name"))
        self.assertLess(check, body.index("herdr.workspace_create"))
        self.assertLess(check, body.index("launch_headless(engine"))


class CloudLimits(LaunchHarness):
    def assert_long_limits_proceed(self, settings=None):
        for hard in ("56m", "90m", "55m"):
            result, err, _ = self.run_launch(None, hard=hard, settings=settings)
            self.assertEqual(result, "reached", hard)
            self.assertNotIn("refused", result + err, hard)

    def test_s11_a_long_hard_limit_proceeds_the_token_is_renewed_while_a_worker_runs(self):
        self.assert_long_limits_proceed()

    def test_s11_the_issue_link_warning_still_prints_once_with_a_long_limit(self):
        result, err, _ = self.run_launch(None, hard="90m", brief="see acme-org/widget-service#13")
        self.assertEqual(result, "reached")
        self.assertEqual(len([l for l in err.splitlines() if "cannot read" in l and "issue" in l]), 1, err)
        self.assertIn("#1019", err)

    def test_s11_mutant_a_restored_refusal_above_55_minutes_is_caught(self):
        sb, _ = make_state()
        b = sb.load_module("cloud_worker")
        applied = []
        hard_now = []

        def mutant_settings():   # the restored rule: a launch above 55 minutes is refused before the settings are used
            if hard_now[-1] != "55m":
                applied.append(hard_now[-1])
                raise b.H["LaunchError"]("--hard %s is above 55m: refused" % hard_now[-1])
            return "test"
        original = LaunchHarness.run_launch

        def tracking(self_, boss, **kw):
            hard_now.append(kw.get("hard"))
            return original(self_, boss, **kw)
        LaunchHarness.run_launch = tracking
        try:
            with self.assertRaises(AssertionError):
                self.assert_long_limits_proceed(settings=mutant_settings)
        finally:
            LaunchHarness.run_launch = original
        self.assertEqual(applied, ["56m"], "the mutation was applied")

    def test_s11_a_brief_that_links_an_issue_of_the_repository_warns_once_and_continues(self):
        brief = "see https://github.com/acme-org/widget-service/issues/12 and acme-org/widget-service#13"
        result, err, _ = self.run_launch(None, brief=brief)
        self.assertEqual(result, "reached")
        self.assertEqual(len([l for l in err.splitlines() if "cannot read" in l and "issue" in l]), 1, err)
        result, err, _ = self.run_launch(None, brief="no links; see acme-org/other-repo#4")
        self.assertEqual([l for l in err.splitlines() if "issue" in l and "cannot read" in l], [])

    def test_s9_a_brief_naming_docker_without_stack_warns_once_before_the_launcher_and_with_stack_never(self):
        result, err, _ = self.run_launch(None, brief="build the image with docker buildx, then docker compose up")
        self.assertEqual(result, "reached")   # reached the launcher lookup, which comes after the warning
        self.assertEqual(len([l for l in err.splitlines() if "no nested Docker" in l]), 1, err)
        result, err, _ = self.run_launch(None, brief="use docker", stack=True, shape="S")
        self.assertEqual([l for l in err.splitlines() if "no nested Docker" in l], [])

    def test_s9_the_pod_shape_is_in_the_one_line_output(self):
        src = read("bin/cloud_worker.py")
        self.assertIn("def pod_shape(", src)
        sb, _ = make_state()
        b = sb.load_module("cloud_worker")
        self.assertEqual(b.pod_shape(types.SimpleNamespace(shape=None, stack=False, agent="worker")), "XS")
        self.assertEqual(b.pod_shape(types.SimpleNamespace(shape=None, stack=True, agent="worker")), "S")
        self.assertEqual(b.pod_shape(types.SimpleNamespace(shape="M", stack=False, agent="worker")), "M")
        self.assertEqual(b.pod_shape(types.SimpleNamespace(shape=None, stack=False, agent="orchestrator")), "S")
        tail = src[src.index("def _launch("):]
        self.assertIn('"shape": pod_shape(args)', tail[:tail.index("def _registry_note")])

    def test_u3_the_one_temporary_rule_is_a_constant_in_one_module_followed_by_an_ask_number(self):
        src = read("bin/cloud_worker.py")
        names = re.findall(r"^(TEMP_[A-Z_]+) =", src, re.M)
        self.assertEqual(names, ["TEMP_ISSUE_LINK_WARNING"])
        line = [l for l in src.splitlines() if l.startswith("TEMP_ISSUE_LINK_WARNING =")]
        self.assertEqual(len(line), 1)
        self.assertIn("#1019", line[0])
        self.assertNotIn("TEMP_HARD_LIMIT_MINUTES", src)
        self.assertNotIn("hard_limit_problem", src)


class Docs(unittest.TestCase):
    COMMAND = "fleet-switchboard brief header --role operator"

    def test_s7_the_quickstart_section_and_the_skill_name_the_command(self):
        doc = read("docs/cloud-operator-quickstart.md")
        section = doc[doc.index("## Briefing a cloud operator"):]
        section = section[:section.index("\n## ", 5)] if "\n## " in section[5:] else section
        self.assertEqual(section.count(self.COMMAND), 1)
        self.assertEqual(read("skills/fleet-coordination/SKILL.md").count(self.COMMAND), 1)


if __name__ == "__main__":
    unittest.main()
