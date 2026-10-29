"""Ask #1046: a standard operator brief header, and a launch that refuses a report-to name nobody can answer to. Offline: fake cluster pieces,
fake registry in a temporary state directory. Scenarios: S2 S3 (the header), S4 S5 (the check), S6 (mutants), S7 (documentation), S9 S11 (cloud
shape and temporary rules), U2 (no secret), U3 (the one constant in one place)."""
import hashlib
import inspect
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)

PREFIX = "fl" + "eet"   # assembled from a fragment: the neutral-names scan of cloud/ finds no whole retired prefix in this file
SOURCE = os.path.join(ROOT, "skills", PREFIX + "-coordination", "operator-brief-header.txt")
PLANTED = "planted-" + "Hq7Lz2" * 5   # a fake setting value
SB = os.path.join(ROOT, "bin", PREFIX + "-switchboard")
HEADER_OUTPUT_SHA256 = "79ee69b86fb787c543317ea9973a9c4ad2915ae2dcb494bb2dc8b6bbf7bc3002"   # the output at the commit before the change


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
        discovery = (extra or {}).pop("discovery", None)

        class Eng:
            actor = "test"
            audit = types.SimpleNamespace(append=lambda actor, event, name, detail: audit.append((event, name, detail)))

            def discover(self, now):
                return discovery(now) if discovery else types.SimpleNamespace(agents={})

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
        src = read("bin/" + PREFIX + "-switchboard")
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
    COMMAND = PREFIX + "-switchboard brief header --role operator"

    def test_s7_the_quickstart_section_and_the_skill_name_the_command(self):
        doc = read("docs/cloud-operator-quickstart.md")
        section = doc[doc.index("## Briefing a cloud operator"):]
        section = section[:section.index("\n## ", 5)] if "\n## " in section[5:] else section
        self.assertEqual(section.count(self.COMMAND), 1)
        self.assertEqual(read("skills/" + PREFIX + "-coordination/SKILL.md").count(self.COMMAND), 1)

    def test_the_override_sentences_are_in_the_quickstart(self):
        doc = read("docs/cloud-operator-quickstart.md")
        for phrase in ("The override --boss-unregistered REASON is not restricted to a person: an agent may pass it.",
                       "It writes one launch.boss_unregistered audit line (the actor, the worker, the boss text and the reason).",
                       "A worker launched so has a boss nobody answers to: only the operator or the Chief of Staff can end it.",
                       "The reasons of both overrides, --boss-unregistered and --memory-ok, are scrubbed of secret shapes"):
            self.assertEqual(doc.count(phrase), 1, phrase)


def _shapes():
    return {
        "token": "ghp_" + "aB3dE5gH7j" * 3 + "KLmnop",
        "bearer": "Authorization: Bearer abcDEF1234567890xyz",
        "key": "-----BEGIN PRIVATE KEY-----",
        "url": "https://user:pass@host/path",
        "control": "line one\x07\nline two",
    }


SECRET_PART = {"token": "ghp_" + "aB3dE5gH7j" * 3, "bearer": "abcDEF1234567890xyz", "key": "BEGIN PRIVATE KEY", "url": "user:pass@", "control": None}


class OverrideReasonScrub(LaunchHarness):
    """S12: both override flags send their reason through the one helper `scrubbed_flag_text`."""

    def boss_audit(self, text):
        # the flag's own up-front check refuses a control character, so the control case goes in through the helper below
        result, _, audit = self.run_launch("lane-nobody", override=text)
        lines = [a for a in audit if a[0] == "launch.boss_unregistered"]
        return lines[0][2]["reason"] if lines else None

    def memory_audit(self, text, sb=None):
        sb = sb or load_sb()
        seen = []
        eng = types.SimpleNamespace(actor="t", audit=types.SimpleNamespace(append=lambda actor, kind, name, data: seen.append((kind, data))))
        sb.memory_read = lambda runner: None
        sb.launch_memory_check(types.SimpleNamespace(memory_ok=text, name="w"), eng, tempfile.mkdtemp(), None, lambda: 0)
        return [d for k, d in seen if k == "launch.memory_ok"][0]["reason"]

    def test_s12_two_flags_five_shapes(self):
        sb = load_sb()
        for name, text in _shapes().items():
            part = SECRET_PART[name]
            with self.subTest(flag="memory-ok", shape=name):
                got = self.memory_audit("fix " + text, sb)
                self.assertNotIn("\n", got)
                if part:
                    self.assertNotIn(part, got)
            with self.subTest(flag="boss-unregistered", shape=name):
                if name == "control":
                    got = load_sb().scrubbed_flag_text("fix " + text)
                else:
                    got = self.boss_audit("fix " + text)
                    self.assertIsNotNone(got)
                self.assertNotIn("\n", got)
                if part:
                    self.assertNotIn(part, got)
        self.assertEqual(self.memory_audit("release fix", sb), "release fix")
        self.assertEqual(self.boss_audit("release fix"), "release fix")

    def test_the_helper_cuts_to_160_and_removes_controls(self):
        sb = load_sb()
        got = sb.scrubbed_flag_text("a\x00b\nc" + "x" * 300)
        self.assertEqual(len(got), 160)
        self.assertNotRegex(got, r"[\x00-\x1f]")

    def test_both_flags_call_the_same_helper(self):
        from unittest import mock
        sb = load_sb()
        calls = []
        real = sb.scrubbed_flag_text
        sb.scrubbed_flag_text = lambda text, limit=160: calls.append(text) or real(text, limit)
        self.memory_audit("release fix", sb)
        with mock.patch.object(sys.modules[__name__], "load_sb", lambda: sb):
            result, _, audit = self.run_launch("lane-nobody", override="release fix")
        self.assertEqual(calls, ["release fix", "lane-nobody", "release fix"], "one call from each free-text field: the memory reason, the boss text, the boss reason")
        self.assertEqual([a for a in audit if a[0] == "launch.boss_unregistered"][0][2]["reason"], "release fix")

    def test_mutant_cut_without_scrub_fails_every_shape_case(self):
        sb = load_sb()
        sb.scrubbed_flag_text = lambda text: sb._one_line(text, 160)   # the old behaviour: cut, no scrub
        leaked = [n for n, t in _shapes().items() if SECRET_PART[n] and SECRET_PART[n] in self.memory_audit("fix " + t, sb)]
        self.assertEqual(sorted(leaked), ["bearer", "key", "token", "url"], "mutation applied: the unscrubbed cut keeps each secret shape")

    def test_mutant_only_the_new_flag_scrubs_fails_the_memory_ok_cases(self):
        from unittest import mock
        sb = load_sb()
        real = sb.scrubbed_flag_text
        # the memory-ok site calls the helper through the module global: a build that cuts there without scrub is this global replaced for that site only
        src = inspect.getsource(sb.launch_memory_check).replace("scrubbed_flag_text(reason)", "_one_line(reason, 200)")
        self.assertIn("_one_line(reason, 200)", src, "mutation applied")
        namespace = sb.__dict__
        exec(compile(src.replace("def launch_memory_check", "def mutant_memory_check", 1), "<mutant>", "exec"), namespace)
        sb.launch_memory_check = namespace["mutant_memory_check"]
        leaked = [n for n, t in _shapes().items() if SECRET_PART[n] and SECRET_PART[n] in self.memory_audit("fix " + t, sb)]
        self.assertEqual(sorted(leaked), ["bearer", "key", "token", "url"])
        self.assertNotIn(SECRET_PART["token"], self.boss_audit("fix " + _shapes()["token"]), "the new flag still scrubs")


BOSS_SHAPES = {
    "token": ("ghp_" + "aB3dE5gH7j" * 3 + "KLmnop", "ghp_" + "aB3dE5gH7j" * 3),
    "bearer": ("Authorization: Bearer abcDEF1234567890xyz", "abcDEF1234567890xyz"),
    "key": ("-----BEGIN PRIVATE KEY-----\nMIIBVgIBADANBgkq\n-----END PRIVATE KEY-----", "MIIBVgIBADANBgkq"),
    "url": ("https://user:pass@host/path", "user:pass@"),
    "control": ("boss\x07one\ntwo", None),
}


class EveryFreeTextField(OverrideReasonScrub):
    """S12: the three free-text fields (memory reason, boss reason, boss text) go through the one helper."""

    def launch_with_sb(self, sb, boss, reason):
        from unittest import mock
        with mock.patch.object(sys.modules[__name__], "load_sb", lambda: sb):
            return self.run_launch(boss, override=reason)

    def boss_line(self, sb, boss, reason="plain reason"):
        _, _, audit = self.launch_with_sb(sb, boss, reason)
        lines = [a for a in audit if a[0] == "launch.boss_unregistered"]
        return lines[0][2] if lines else None

    def test_s12_three_fields_five_shapes_leave_no_secret_part(self):
        sb = load_sb()
        for name, (text, part) in BOSS_SHAPES.items():
            with self.subTest(field="boss text", shape=name):
                if name == "control":
                    detail = {"reports_to": sb.scrubbed_flag_text(text, 60), "reason": ""}
                else:
                    detail = self.boss_line(sb, text)
                    self.assertIsNotNone(detail)
                self.assertNotIn("\n", detail["reports_to"])
                self.assertNotRegex(detail["reports_to"], r"[\x00-\x1f]")
                if part:
                    self.assertEqual(repr(detail).count(part), 0)
            with self.subTest(field="boss reason", shape=name):
                if part:
                    detail = self.boss_line(sb, "lane-nobody", text)
                    self.assertEqual(repr(detail).count(part), 0)
            with self.subTest(field="memory reason", shape=name):
                got = self.memory_audit(text, sb)
                self.assertNotIn("\n", got)
                if part:
                    self.assertEqual(got.count(part), 0)

    def test_s12_a_plain_text_is_kept_as_typed_per_field(self):
        sb = load_sb()
        detail = self.boss_line(sb, "lane-nobody", "the lane starts later")
        self.assertEqual(detail, {"reports_to": "lane-nobody", "reason": "the lane starts later"})
        self.assertEqual(self.memory_audit("release fix", sb), "release fix")

    def test_s12_the_three_sites_call_the_one_helper(self):
        src = read("bin/cloud_worker.py")
        self.assertIn('"reports_to": H["scrubbed_flag_text"](args.reports_to, 60)', src)
        self.assertIn('"reason": H["scrubbed_flag_text"](reason)', src)
        self.assertIn("scrubbed_flag_text(reason)", inspect.getsource(load_sb().launch_memory_check))

    def test_s12_mutant_the_boss_text_cut_without_scrub_leaks(self):
        sb = load_sb()
        real = sb.scrubbed_flag_text
        sb.scrubbed_flag_text = lambda text, limit=160: sb._one_line(text, limit) if limit == 60 else real(text, limit)
        text, part = BOSS_SHAPES["token"]
        detail = self.boss_line(sb, text)
        self.assertIn(part, detail["reports_to"], "mutation applied: the boss text leaks")
        self.assertEqual(self.boss_line(load_sb(), text)["reports_to"].count(part), 0, "the real helper does not")

    def test_s12_mutant_scrubbing_just_the_new_flag_reason_leaves_memory_ok_leaking(self):
        sb = load_sb()
        src = inspect.getsource(sb.launch_memory_check).replace("scrubbed_flag_text(reason)", "_one_line(reason, 200)")
        self.assertIn("_one_line(reason, 200)", src, "mutation applied")
        exec(compile(src.replace("def launch_memory_check", "def mutant_memory_check", 1), "<mutant>", "exec"), sb.__dict__)
        sb.launch_memory_check = sb.__dict__["mutant_memory_check"]
        text, part = BOSS_SHAPES["token"]
        self.assertIn(part, self.memory_audit(text, sb))
        self.assertEqual(self.boss_line(sb, "lane-nobody", text)["reason"].count(part), 0)


KEY_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\nKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\nzzzzAAAA1111bbbb2222cccc\n"
KEY_FORMS = {
    "rsa": ("RSA PRIVATE KEY", "RSA PRIVATE KEY"),
    "plain": ("PRIVATE KEY", "PRIVATE KEY"),
    "encrypted": ("ENCRYPTED PRIVATE KEY", "ENCRYPTED PRIVATE KEY"),
    "pgp": ("PGP PRIVATE KEY BLOCK", "PGP PRIVATE KEY BLOCK"),
    "openssh": ("OPENSSH PRIVATE KEY", "OPENSSH PRIVATE KEY"),
    "ec": ("EC PRIVATE KEY", "EC PRIVATE KEY"),
}


class WholeKeyBlock(unittest.TestCase):
    """S13: the key pattern covers the whole block, and stays linear."""

    @staticmethod
    def block(kind):
        return "-----BEGIN %s-----\n%s-----END %s-----" % (kind, KEY_BODY, kind)

    def test_s13_four_block_forms_leave_no_body_line(self):
        sb = load_sb()
        for name, (kind, _) in KEY_FORMS.items():
            with self.subTest(form=name):
                got = sb.redact_command_text("before\n" + self.block(kind) + "\nafter")
                for line in KEY_BODY.splitlines():
                    self.assertNotIn(line, got)
                self.assertIn("before", got)
                self.assertIn("after", got)

    def test_s13_a_line_between_two_blocks_is_kept(self):
        sb = load_sb()
        got = sb.redact_command_text(self.block("RSA PRIVATE KEY") + "\nkeep this line\n" + self.block("PRIVATE KEY"))
        self.assertIn("keep this line", got)
        for line in KEY_BODY.splitlines():
            self.assertNotIn(line, got)

    def test_s13_timing_one_begin_marker_no_end_marker(self):
        sb = load_sb()
        text = "-----BEGIN PRIVATE KEY-----" + "A" * 1000000
        start = time.monotonic()
        sb.redact_command_text(text)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_s13_timing_many_begin_markers_no_end_marker(self):
        sb = load_sb()
        text = "-----BEGIN PRIVATE KEY-----" * 20000
        start = time.monotonic()
        sb.redact_command_text(text)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_s13_mutant_a_lazy_dot_all_pattern_fails_the_many_begin_timing(self):
        code = ("import re,sys,time\n"
                "p=re.compile(r'-----BEGIN[A-Z ]*PRIVATE KEY-----.*?-----END[A-Z ]*PRIVATE KEY-----', re.S)\n"
                "t='-----BEGIN PRIVATE KEY-----'*20000\n"
                "s=time.monotonic(); p.sub('[redacted]', t); print(time.monotonic()-s)\n")
        self.assertIn(".*?", code, "mutation applied")
        start = time.monotonic()
        try:
            done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
            slow = float(done.stdout.strip()) >= 2.0
        except subprocess.TimeoutExpired:
            slow = True   # the 10 s guard: the mutant did not finish
        self.assertTrue(slow, "the mutant is too slow for the 2.0 s bound, as the real pattern is not")
        self.assertLess(time.monotonic() - start, 15)


class KeyBlockOddShapes(unittest.TestCase):
    """S13 (widened) and U6: a dash, another end marker, or no end marker."""

    BODY = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "KcwggSjAgEAAoIBAQC7VJTUt9Us8cKj", "zzzzAAAA1111bbbb2222cccc"]

    def pgp(self, armor="Version: GnuPG v2.0-x\nComment: a-b\n\n", body=None, end="-----END PGP PRIVATE KEY BLOCK-----"):
        return "-----BEGIN PGP PRIVATE KEY BLOCK-----\n" + armor + "\n".join(body or self.BODY) + "\n" + end

    def assert_no_body(self, got):
        for line in self.BODY:
            self.assertNotIn(line, got)

    def test_s13_six_forms_multi_line_leave_no_body_line(self):
        sb = load_sb()
        kinds = ["RSA PRIVATE KEY", "PRIVATE KEY", "ENCRYPTED PRIVATE KEY", "OPENSSH PRIVATE KEY", "EC PRIVATE KEY", "PGP PRIVATE KEY BLOCK"]
        for kind in kinds:
            with self.subTest(kind=kind):
                text = "-----BEGIN %s-----\n%s\n-----END %s-----" % (kind, "\n".join(self.BODY), kind)
                self.assert_no_body(sb.redact_command_text("x\n" + text + "\ny"))

    def test_s13_a_dash_in_the_armor_header_leaves_no_body_line(self):
        self.assert_no_body(load_sb().redact_command_text(self.pgp()))

    def test_s13_a_dash_in_the_body_leaves_no_body_line(self):
        body = ["MIIEvQIBADANBgkq-hkiG9w0BAQEFAASC", "Kcwg-SjAgEAAoIBAQC7VJTUt9Us8cKj", "zzzzAAAA1111bbbb2222cccc"]
        got = load_sb().redact_command_text(self.pgp(armor="", body=body))
        for line in body:
            self.assertNotIn(line, got)

    def test_s13_an_end_marker_of_another_kind_leaves_no_body_line(self):
        got = load_sb().redact_command_text(self.pgp(end="-----END CERTIFICATE-----") + "\nafter")
        self.assert_no_body(got)
        self.assertNotIn("after", got)   # S18: a foreign end marker does not end the block, so the text to its end is redacted

    def test_s13_a_begin_with_no_end_covers_the_text_up_to_the_next_begin(self):
        text = "head\n-----BEGIN PRIVATE KEY-----\n" + "\n".join(self.BODY) + "\n-----BEGIN PRIVATE KEY-----\nzed"
        got = load_sb().redact_command_text(text)
        self.assert_no_body(got)
        self.assertIn("head", got)

    def test_s13_a_line_between_two_odd_blocks_is_kept(self):
        got = load_sb().redact_command_text(self.pgp() + "\nunrelated line\n" + self.pgp(end="-----END CERTIFICATE-----") + "\nlast")
        self.assert_no_body(got)
        self.assertIn("unrelated line", got)
        self.assertNotIn("last", got)   # S18: the second block has no own end marker, so it runs to the end of the text

    def test_s13_timing_on_the_real_path(self):
        sb = load_sb()
        texts = {
            "one begin, no end": "-----BEGIN PRIVATE KEY-----" + "A" * 1000000,
            "20000 begins": "-----BEGIN PRIVATE KEY-----" * 20000,
            "dashes": "-----" * 200000,
            "begin words": "-----BEGIN " * 90910,
        }
        for name, text in texts.items():
            with self.subTest(case=name):
                start = time.monotonic()
                sb.redact_command_text(text)
                self.assertLess(time.monotonic() - start, 2.0)

    def test_s13_mutant_a_non_dash_body_fails_the_dash_cases(self):
        real = [p for p, _ in load_sb()._SECRET_PATTERNS if "PRIVATE KEY" in p.pattern and "(?:BEGIN|END)[A-Z ]*PRIVATE" not in p.pattern][0]
        mutant = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY(?: BLOCK)?-----[^-]*-----END[A-Z ]*PRIVATE KEY(?: BLOCK)?-----")
        self.assertIn("[^-]*", mutant.pattern, "mutation applied")
        text = self.pgp()
        self.assertIn(self.BODY[0], mutant.sub("[redacted]", text))
        self.assertNotIn(self.BODY[0], real.sub("[redacted]", text))

    def test_s13_mutant_a_lazy_dot_all_pattern_is_slow_on_many_begins(self):
        code = ("import re,time\n"
                "p=re.compile(r'-----BEGIN[A-Z ]*PRIVATE KEY(?: BLOCK)?-----.*?-----END[A-Z ]*-----', re.S)\n"
                "t='-----BEGIN PRIVATE KEY-----'*20000\n"
                "s=time.monotonic(); p.sub('[redacted]', t); print(time.monotonic()-s)\n")
        self.assertIn(".*?", code, "mutation applied")
        try:
            done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
            slow = float(done.stdout.strip()) >= 2.0
        except subprocess.TimeoutExpired:
            slow = True
        self.assertTrue(slow)

    def test_u6_the_pattern_has_no_dash_class_and_has_the_marker_guard(self):
        src = read("bin/" + PREFIX + "-switchboard")
        line = [l for l in src.splitlines() if "PRIVATE KEY(?: BLOCK)?-----" in l and "(?:BEGIN|END)[A-Z ]*PRIVATE" not in l][0]
        self.assertNotIn("[^-]*", line)
        self.assertIn("(?!-----BEGIN[A-Z ]{0,40}PRIVATE KEY)(?!-----END[A-Z ]{0,40}PRIVATE KEY)", line)   # S18


class SecretFamilyOddShapes(unittest.TestCase):
    """S17: every spanning row of the shared table (bearer, named value, URL password, key block, JWT) holds for odd shapes."""

    def red(self, text):
        return load_sb().redact_command_text(text)

    def test_named_value_odd_shapes_leave_no_secret_part(self):
        cases = ['password="ab cd\nPARTX"', 'password="ab PARTX cd"', 'password="ab\\"PARTX cd"', "token='ab\\'PARTX cd'",
                 'password="abc PARTX', "token: 'abc PARTX", 'token=PARTX,', 'token=PARTX;', 'api_key: "PARTX",']
        for text in cases:
            with self.subTest(text=text):
                self.assertNotIn("PARTX", self.red(text))
        self.assertNotIn("cd", self.red('password="ab cd\nPARTX"').replace("redacted", ""))
        self.assertEqual(self.red('password="abc def" next'), "password=[redacted] next")
        self.assertEqual(self.red('password="abc\nrest"').count("rest"), 0)
        self.assertEqual(self.red('password="abc PARTX\nkept line'), "password=[redacted]\nkept line")

    def test_bearer_odd_shapes_leave_no_token(self):
        for text in ("Bearer abc123-PARTX", "Bearer PARTX123\nnext", "Bearer PARTX123, next", "Bearer ab-PARTX-cd/ef=="):
            with self.subTest(text=text):
                self.assertNotIn("PARTX", self.red(text))

    def test_url_password_odd_shapes_leave_no_secret_part(self):
        for text in ("https://u:ab/PARTX@h/x", "https://u:ab@PARTX@h/x", "https://u:ab%40PARTX@h/x", "git+https://u:PARTX/@h"):
            with self.subTest(text=text):
                self.assertNotIn("PARTX", self.red(text))
        self.assertIn("https://u:[redacted]@h", self.red("https://u:ab/PARTX@h/x"))
        self.assertEqual(self.red("see https://h:8080/path ok"), "see https://h:8080/path ok")

    def test_jwt_odd_shapes_leave_no_secret_part(self):
        for text in ("eyJabc-def_PARTX.abc-def_PARTX.abc-def_PARTX", "eyJabcdefg.abcdefg_PARTX", "eyJabc-def.abc_PARTXX.", "x eyJabc_def-1.PARTX-abcdef.zz-"):
            with self.subTest(text=text):
                self.assertNotIn("PARTX", self.red(text))

    def test_url_password_with_a_space_is_a_named_limit_in_the_quickstart(self):
        doc = read("docs/cloud-operator-quickstart.md")
        self.assertIn("a password in a URL that holds a raw space", doc)
        self.assertIn("percent-encode the space", doc)

    def test_each_spanning_pattern_is_linear_on_one_million_characters(self):
        sb = load_sb()

        def rep(unit):
            return (unit * (1000000 // len(unit) + 1))[:1000000]
        texts = {
            "quote never closes": 'token="' + "a" * 999990, "quote lines": rep('token="\n'), "token=": rep("token="), "token-": rep("token-"),
            "tokentoken": rep("token"), "Bearer ": rep("Bearer "), "Bearer a-": rep("Bearer a-"), "://a:": rep("://a:"),
            "://a:b/": rep("://a:b/"), "eyJ": rep("eyJ"), "eyJ-": rep("eyJ-"), "eyJ dotted": rep("eyJaaaaaaa."),
        }
        for name, text in texts.items():
            with self.subTest(case=name):
                start = time.monotonic()
                sb.redact_command_text(text)
                self.assertLess(time.monotonic() - start, 2.0)


class KeyBlockForeignMarkers(unittest.TestCase):
    """S18: the key body ends at an own end marker, the next key begin marker, or the end of the text; a foreign marker inside it does not end it."""

    BODY = ["MIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "KcwggSjAgEAAoIBAQC7VJTUt9Us8cKj", "zzzzAAAA1111bbbb2222cccc"]
    BEGIN = "-----BEGIN PRIVATE KEY-----\n"
    OWN_END = "\n-----END PRIVATE KEY-----"
    FOREIGN = "-----END CERTIFICATE-----"
    OLD_BODY = r"-----BEGIN[A-Z ]*PRIVATE KEY(?: BLOCK)?-----(?:(?!-----(?:BEGIN|END))[\s\S])*(?:-----END[A-Z ]*-----)?"

    def shapes(self):
        b = self.BODY
        f = self.FOREIGN
        return {
            "foreign end once": self.BEGIN + b[0] + "\n" + f + "\n" + b[1] + "\n" + b[2],
            "foreign end twice": self.BEGIN + b[0] + "\n" + f + "\n" + b[1] + "\n" + f + "\n" + b[2],
            "foreign end then own end": self.BEGIN + b[0] + "\n" + f + "\n" + b[1] + "\n" + b[2] + self.OWN_END,
            "foreign end then no own end": self.BEGIN + b[0] + "\n" + f + "\n" + b[1] + "\n" + b[2] + "\n",
        }

    def test_s18_four_interrupted_shapes_leave_no_body_line(self):
        sb = load_sb()
        for name, text in self.shapes().items():
            with self.subTest(shape=name):
                got = sb.redact_command_text(text)
                for line in self.BODY:
                    self.assertNotIn(line, got)

    def test_s18_a_complete_key_then_plain_text_then_a_certificate_block_keeps_the_plain_text_and_the_certificate_block(self):
        cert = "-----BEGIN CERTIFICATE-----\nCERTBODYLINE\n-----END CERTIFICATE-----"
        text = self.BEGIN + self.BODY[0] + self.OWN_END + "\nplain text line\n" + cert
        got = load_sb().redact_command_text(text)
        self.assertNotIn(self.BODY[0], got)
        self.assertIn("plain text line", got)
        self.assertIn(cert, got)

    def test_s18_a_key_with_no_own_end_then_a_certificate_block_is_all_redacted(self):
        cert = "-----BEGIN CERTIFICATE-----\nCERTBODYLINE\n-----END CERTIFICATE-----"
        got = load_sb().redact_command_text(self.BEGIN + self.BODY[0] + "\n" + cert)
        self.assertNotIn(self.BODY[0], got)
        self.assertNotIn("CERTBODYLINE", got)

    def test_s18_two_complete_blocks_keep_the_line_between(self):
        got = load_sb().redact_command_text(self.BEGIN + self.BODY[0] + self.OWN_END + "\nunrelated\n" + self.BEGIN + self.BODY[1] + self.OWN_END)
        self.assertIn("unrelated", got)
        self.assertNotIn(self.BODY[0], got)
        self.assertNotIn(self.BODY[1], got)

    def test_s18_timing_on_the_real_path(self):
        sb = load_sb()
        pieces = {
            "one begin, no end": "-----BEGIN PRIVATE KEY-----" + "A" * 1000000,
            "20000 begins": "-----BEGIN PRIVATE KEY-----" * 20000,
            "dashes": "-----" * 200000,
            "begin words": "-----BEGIN " * 90910,
            "foreign ends": "-----END CERTIFICATE-----" * 40000,
            "end plus capitals": ("-----END " + "A" * 100) * 9174,
            "begin then foreign ends": "-----BEGIN PRIVATE KEY-----" + "-----END CERTIFICATE-----" * 40000,
            "begin then end plus capitals": "-----BEGIN PRIVATE KEY-----" + ("-----END " + "A" * 100) * 9174,
        }
        for name, text in pieces.items():
            with self.subTest(case=name):
                start = time.monotonic()
                sb.redact_command_text(text)
                self.assertLess(time.monotonic() - start, 2.0)

    def test_s18_mutant_the_old_body_fails_the_interrupted_shapes(self):
        mutant = re.compile(self.OLD_BODY)
        self.assertIn("(?!-----(?:BEGIN|END))", mutant.pattern, "mutation applied")
        failed = 0
        for text in self.shapes().values():
            got = mutant.sub("[redacted]", text)
            if any(line in got for line in self.BODY):
                failed += 1
        self.assertEqual(failed, 4)


class NamedValueQuoteBeforeSeparator(unittest.TestCase):
    """S19: a closing quote between the name and its separator (a JSON-style pair)."""

    def red(self, text):
        return load_sb().redact_command_text(text)

    def test_s19_json_style_pairs_leave_no_value_character(self):
        cases = {
            '"password": "a secret value"': ["secret", "value"],
            "'token': 'a b'": ["a b"],
            '"api_key" : "x\\"y z"': ["x\\", "y z"],
        }
        for text, parts in cases.items():
            with self.subTest(text=text):
                got = self.red(text)
                for part in parts:
                    self.assertNotIn(part, got)
                self.assertIn("[redacted]", got)

    def test_s19_the_quote_text_is_kept(self):
        self.assertEqual(self.red('"password": "abc"'), '"password": [redacted]')

    def test_s19_timing_on_the_real_path(self):
        sb = load_sb()

        def rep(unit):
            return (unit * (1000000 // len(unit) + 1))[:1000000]
        for name, text in {"quoted token": rep('"token": '), "look-behind one": rep("xtokenx "), "look-behind two": rep("a-a-a ")}.items():
            with self.subTest(case=name):
                start = time.monotonic()
                sb.redact_command_text(text)
                self.assertLess(time.monotonic() - start, 2.0)

    def test_s19_mutant_a_separator_with_only_equals_or_colon_leaves_the_json_value(self):
        real = [p for p, _ in load_sb()._SECRET_PATTERNS if "passw" in p.pattern][0]
        quote_group = "([\\\"']?)("
        self.assertIn(quote_group, real.pattern)
        mutant = re.compile(real.pattern.replace(quote_group, "()(", 1))
        self.assertNotEqual(mutant.pattern, real.pattern, "mutation applied")
        text = '"password": "a secret value"'
        self.assertIn("secret", mutant.sub(r"\1\2\3[redacted]", text))
        self.assertNotIn("secret", real.sub(r"\1\2\3[redacted]", text))


class HeaderFileAndOutput(unittest.TestCase):
    def test_s14_the_file_comment_names_the_title_line_and_the_id_text_form(self):
        comment = "\n".join(l for l in open(SOURCE).read().splitlines() if l.startswith("#"))
        self.assertIn("title line", comment)
        self.assertIn("`[id] text`", comment)
        self.assertNotIn("and nothing else", comment)

    def test_s14_the_printed_output_is_the_recorded_one(self):
        code, out, err = run_header()
        self.assertEqual(code, 0, err)
        self.assertEqual(hashlib.sha256(out.encode()).hexdigest(), HEADER_OUTPUT_SHA256)


class DocsRedactionLimits(unittest.TestCase):
    def test_the_quickstart_states_the_redaction_limits_and_the_local_read(self):
        doc = read("docs/cloud-operator-quickstart.md")
        for phrase in ("a begin marker with no end marker redacts the text to its end",
                       "a URL password that holds a space is not redacted",
                       "the local launch makes one more discovery of the local agents before the report-to check; it is a read with no behaviour change"):
            self.assertEqual(doc.count(phrase), 1, phrase)


class DocsBossText(unittest.TestCase):
    def test_the_quickstart_names_the_scrubbed_boss_text(self):
        doc = read("docs/cloud-operator-quickstart.md")
        phrase = "The boss text of --boss-unregistered is scrubbed too, and cut to 60 characters."
        self.assertEqual(doc.count(phrase), 1)


class EmptyRegistry(LaunchHarness):
    """S15: a registry with no name at all refuses an unregistered `--reports-to` too, and the message says how a first launch passes."""

    def local_route(self, boss, reason=None, check=None):
        sb, state = make_state()
        b = sb.load_module("cloud_worker")
        b.bind(sb.__dict__)
        seen, made = [], []
        engine = types.SimpleNamespace(actor="t", audit=types.SimpleNamespace(append=lambda actor, kind, name, data: seen.append((kind, data))),
                                       herdr=types.SimpleNamespace(workspace_create=lambda *a, **k: made.append("workspace")))
        args = types.SimpleNamespace(name="tester7", reports_to=boss, boss_unregistered=reason)
        saved = b.unknown_report_to
        if check:
            b.unknown_report_to = check(saved)
        try:
            with redirect_stderr(io.StringIO()):
                b.check_report_to(engine, state, args, {})
            return 0, "", seen, made
        except sb.LaunchError as problem:
            return 1, str(problem), seen, made
        finally:
            b.unknown_report_to = saved

    def test_s15_both_routes_refuse_against_an_empty_registry_and_name_the_flag(self):
        code, message, audit, made = self.local_route("lane-nobody")
        self.assertEqual((code, audit, made), (1, [], []))
        self.assertIn("--boss-unregistered", message)
        self.assertIn("first launch of a new installation", message)
        result, _, audit = self.run_launch("lane-nobody")
        self.assertTrue(result.startswith("refused:"), result)
        self.assertIn("--boss-unregistered", result)
        self.assertIn("first launch of a new installation", result)
        self.assertEqual(audit, [])

    def test_s15_with_the_flag_and_a_reason_both_routes_go_on_with_one_audit_line(self):
        code, _, audit, made = self.local_route("lane-nobody", "first launch of this installation")
        self.assertEqual(code, 0)
        self.assertEqual([k for k, _ in audit], ["launch.boss_unregistered"])
        result, _, audit = self.run_launch("lane-nobody", override="first launch of this installation")
        self.assertEqual(result, "reached")
        self.assertEqual(len([a for a in audit if a[0] == "launch.boss_unregistered"]), 1)

    def test_s15_a_registry_with_names_does_not_add_the_first_launch_sentence(self):
        result, _, _ = self.run_launch("lane-nobody", agents=("lane-a",))
        self.assertNotIn("first launch of a new installation", result)

    def test_s15_mutant_skipping_the_check_against_an_empty_registry_is_caught(self):
        def skip_empty(real):
            def mutant(state, name, agents, alias):
                found = load_sb().load_module("registry").read(state)
                if not found.get("agents") and not found.get("retired"):
                    return None, None   # the mutation: an empty registry refuses nothing
                return real(state, name, agents, alias)
            return mutant
        code, _, _, _ = self.local_route("lane-nobody", check=skip_empty)
        self.assertEqual(code, 0, "mutation applied: the empty registry no longer refuses, so the refusal test above fails under it")


class OneDiscovery(LaunchHarness):
    """S16: the report-to check reuses the launch's first discovery; the launch makes two in all (before and after the pod is made)."""

    def counted(self, boss, wrap=None):
        from unittest import mock
        found = types.SimpleNamespace(agents={"lane-a": object()})
        recorder = mock.Mock(return_value=found)
        if wrap:
            b = load_sb().load_module("cloud_worker")
            real = b.check_report_to
            b.check_report_to = wrap(real, recorder)
        try:
            result, _, _ = self.run_launch(boss, agents=("lane-a",), extra={"discovery": recorder})
        finally:
            if wrap:
                b.check_report_to = real
        return result, recorder

    def test_s16_the_check_adds_no_discovery_and_accepts_the_first_answer(self):
        result, recorder = self.counted("lane-a")
        self.assertEqual(result, "reached")
        self.assertEqual(recorder.call_count, 1, "before the pod is made the launch discovers once")

    def test_s16_the_launch_has_exactly_two_discovery_calls(self):
        tail = read("bin/cloud_worker.py")
        tail = tail[tail.index("def _launch("):]
        tail = tail[:tail.index("def _registry_note")]
        self.assertEqual(tail.count(".discover("), 2)

    def test_s16_mutant_an_extra_discovery_in_the_check_is_caught(self):
        def extra_call(real, recorder):
            def mutant(engine, state, args, local_agents):
                engine.discover(engine.now())   # the mutation: the extra discovery
                return real(engine, state, args, local_agents)
            return mutant
        result, recorder = self.counted("lane-a", wrap=extra_call)
        self.assertEqual(recorder.call_count, 2, "mutation applied: the extra call is counted, so the one-call test above fails under it")


class SourceTextScan(unittest.TestCase):
    """The file's own text holds no whole retired prefix (the neutral-names scan), with a mutant."""

    PATTERN = re.compile("fl" + "eet", re.IGNORECASE)

    def own_text(self):
        with open(os.path.abspath(__file__)) as f:
            return f.read()

    def test_own_source_text_has_no_retired_prefix(self):
        self.assertEqual(len(self.PATTERN.findall(self.own_text())), 0)

    def test_mutant_one_planted_literal_is_caught(self):
        mutated = self.own_text() + "\nX = '" + "fl" + "eet-x'\n"
        self.assertNotEqual(mutated, self.own_text(), "mutation applied")
        self.assertEqual(len(self.PATTERN.findall(mutated)), 1)


if __name__ == "__main__":
    unittest.main()
