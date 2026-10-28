"""Ask #1046: the standard brief header, and `launch` refusing an unregistered report-to name. Offline: the fakes of bin/test-cloud-worker
(a fake launcher, a fake cluster, a fake remote server); nothing real is called.

Scenarios S2-S7, S9, S11, U1-U3 of the plan. A test that names a mutant runs the same test body with the guarded step replaced and expects it to fail."""
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent.parent
loader = importlib.machinery.SourceFileLoader("cw_worker_tests_for_header", str(ROOT / "bin" / "test-cloud-worker"))
spec = importlib.util.spec_from_loader("cw_worker_tests_for_header", loader)
base = importlib.util.module_from_spec(spec)
sys.modules["cw_worker_tests_for_header"] = base
loader.exec_module(base)
sb, Base = base.sb, base.Base
cw = sb.load_module("cloud_worker").bind(sb.__dict__)
RULES = ROOT / "skills" / "fleet-coordination" / "operator-brief-header.rules"
PLANTED = "planted-secret-value-" + "9" * 12
HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b")
REGISTRY = {"whole": True, "version": 1, "problem": None, "agents": {"lane-a": {}, "lane-b": {}}, "retired": {"old-lane": {}}}


def rule_ids(path=RULES):
    return [line.split(": ", 1)[0] for line in Path(path).read_text().splitlines() if line.strip() and not line.startswith("#")]


class Header(Base):
    def header(self):
        return self.main("brief", "header", "--role", "operator")

    def test_s2_the_header_prints_once_with_every_rule_of_the_source_and_no_hash(self):
        code, out, err = self.header()
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("Standard header for a brief"), 1)
        ids = rule_ids()
        self.assertGreaterEqual(len(ids), 6)
        for ident in ids:
            self.assertEqual(out.count("[%s]" % ident), 1, ident)
        for line in RULES.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertIn(line.split(": ", 1)[1].strip(), out)
        self.assertEqual(HASH_RE.findall(out), [])

    def test_s2_a_rule_removed_from_the_source_is_absent_from_the_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cut = Path(tmp) / "rules"
            lines = RULES.read_text().splitlines()
            cut.write_text("\n".join(l for l in lines if not l.startswith("DELETE:")) + "\n")
            text = "\n".join(sb.brief_header_lines(cut))
        self.assertNotIn("[DELETE]", text)
        self.assertIn("[SURPRISE]", text)

    def test_s2_the_rules_cover_the_ask(self):
        text = RULES.read_text()
        for needle in ("CW_CLUSTER_ADMIN_CMD", "CW_CLUSTER_ADMIN_SECRET", "kubeconfig", "0700", "shred", "/dev/null", "exact name", "stop and report"):
            self.assertIn(needle, text)

    def test_s3_u2_the_header_shows_key_names_and_never_a_value(self):
        with mock.patch.dict(os.environ, {"CW_CLUSTER_ADMIN_CMD": PLANTED, "CW_CLUSTER_ADMIN_SECRET": PLANTED}):
            code, out, err = self.header()
        self.assertEqual(code, 0)
        self.assertIn("CW_CLUSTER_ADMIN_CMD", out)
        self.assertNotIn(PLANTED, out + err)

    def test_s7_the_documentation_names_the_command_in_both_places(self):
        command = "fleet-switchboard brief header"
        section = (ROOT / "docs" / "cloud-operator-quickstart.md").read_text().split("## Briefing a cloud operator", 1)
        self.assertEqual(len(section), 2, "the quickstart has no section `Briefing a cloud operator`")
        self.assertIn(command, section[1].split("\n## ", 1)[0])
        self.assertIn(command, (ROOT / "skills" / "fleet-coordination" / "SKILL.md").read_text())


class Boss(Base):
    def registered(self, **extra):
        return mock.patch.object(sb, "registry_read", return_value=dict(REGISTRY, **extra))

    def refused_cloud(self):
        with self.registered():
            code, out, err = self.launch_with("--cloud", "--reports-to", "lane-x")
        self.assertEqual(code, 1, err)
        self.assertIn("not a registered identity", err)
        self.assertEqual(self.stub_calls(), [])   # no pod, no create call
        self.assertEqual(self.jsonl("kubectl.log"), [])   # no vault, no cluster call
        self.assertFalse((self.state / "cloud" / "coder7.json").exists())
        return err

    def test_s4_a_cloud_launch_of_an_unregistered_name_is_refused_before_anything_is_made(self):
        err = self.refused_cloud()
        self.assertIn("lane-a", err)   # how to fix: the nearest registered names
        self.assertIn("--boss-unregistered", err)

    def test_s4_a_local_launch_of_an_unregistered_name_is_refused_before_any_session(self):
        engine = mock.Mock(config={}, audit=None)
        engine.discover.return_value = types.SimpleNamespace(agents={}, problems=[])
        engine.now.return_value = 0
        with mock.patch.object(sb, "build_engine", return_value=(engine, self.state, None)), self.registered(), \
                mock.patch.object(sb, "launch_headless", side_effect=AssertionError("created")), mock.patch.object(sb, "launch_agent", side_effect=AssertionError("created")):
            code, out, err = self.main("launch", "lane-new", "--agent", "coder", "--dir", str(self.home), "--role", "tester", "--reports-to", "lane-x")
        self.assertEqual(code, 1, err)
        self.assertIn("not a registered identity", err)
        self.assertLessEqual({c[0] for c in engine.method_calls}, {"discover", "now"})   # only reads: no workspace, no session, no reminder

    def test_s5_a_registered_retired_cos_or_cloud_name_is_accepted(self):
        for boss in ("lane-a", "old-lane", "chief-of-staff"):
            with self.subTest(boss=boss), self.registered():
                code, out, err = self.launch_with("--cloud", "--reports-to", boss, name="coder-" + boss.replace("-", ""))
                self.assertEqual(code, 0, err)
                self.assertNotIn("not a registered identity", err)

    def test_s5_a_name_with_a_cloud_record_is_known(self):
        self.assertEqual(self.launch()[0], 0)   # makes the record of coder7 (its boss orch is the first name: nothing registered yet)
        names, whole = sb.known_boss_names(mock.Mock(discover=mock.Mock(side_effect=OSError("x")), now=mock.Mock(return_value=0)), self.state)
        self.assertIn("coder7", names)
        self.assertIsNone(sb.boss_name_problem("coder7", names, True))

    def test_s5_the_override_launches_and_writes_one_audit_line(self):
        with self.registered():
            code, out, err = self.launch_with("--cloud", "--reports-to", "lane-x", "--boss-unregistered", "its lane starts after this one")
        self.assertEqual(code, 0, err)
        written = "\n".join(self.everything_written().values())
        self.assertEqual(written.count("launch.boss_unregistered"), 1)
        with self.registered():
            code, out, err = self.launch_with("--cloud", "--reports-to", "lane-x", "--boss-unregistered", " ", name="coder8")
        self.assertEqual(code, 1)
        self.assertIn("needs a reason", err)

    def test_s5_an_unreadable_registry_or_a_fresh_state_does_not_refuse(self):
        names, whole = {"lane-a"}, False
        self.assertIsNone(sb.boss_name_problem("lane-x", names, whole))
        self.assertIsNone(sb.boss_name_problem("lane-x", set(), True))
        self.assertIsNotNone(sb.boss_name_problem("lane-x", {"lane-a"}, True))

    def test_u1_a_launch_that_names_no_boss_behaves_as_before(self):
        with self.registered():
            code, out, err = self.main("launch", "coder7", "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main")
        self.assertEqual(code, 0, err)

    def test_s6_the_mutant_that_skips_the_check_fails_the_refusal_test(self):
        self.refused_cloud()
        self.setUp()
        with mock.patch.object(sb, "check_boss_name", lambda engine, state, args: None):
            with self.assertRaises(AssertionError):
                self.refused_cloud()

    def test_u2_a_refusal_carries_no_setting_value(self):
        with mock.patch.dict(os.environ, {"CW_CLUSTER_ADMIN_CMD": PLANTED}):
            err = self.refused_cloud()
        self.assertNotIn(PLANTED, err)


class PodShapeAndTemporaryRules(Base):
    def go(self, brief, *extra, name="coder7"):
        path = self.home / "brief2.txt"
        path.write_text(brief)
        return self.main("launch", name, "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main", "--brief-file", str(path), *extra)

    def warnings(self, err):
        return [l for l in err.splitlines() if "warning:" in l]

    def test_s9_the_one_line_output_shows_the_shape(self):
        for extra, shape in (((), "XS"), (("--stack",), "S"), (("--shape", "M"), "M")):
            with self.subTest(extra=extra):
                self.setUp()
                code, out, err = self.go("Build.\n", *extra)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)["shape"], shape)
                self.assertEqual(len(out.strip().splitlines()), 1)

    def first_create_stderr(self, brief, *extra):
        seen = []
        real = cw.run_launcher

        def watching(runner, command, *args, **kwargs):
            if command == "create":
                seen.append(sys.stderr.getvalue())
            return real(runner, command, *args, **kwargs)
        with mock.patch.object(cw, "run_launcher", watching):
            code, out, err = self.go(brief, *extra)
        return code, err, seen

    def test_s9_one_warning_before_the_first_create_when_the_brief_names_docker_without_stack(self):
        code, err, seen = self.first_create_stderr("Run docker compose and buildx for the lane stack tooling.\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err)), 1)
        self.assertEqual(len(self.warnings(seen[0])), 1)   # printed before the launcher's create was called
        self.setUp()
        code, err, seen = self.first_create_stderr("Run docker.\n", "--stack")
        self.assertEqual(self.warnings(err), [])

    def test_s11_a_hard_limit_above_55_minutes_is_refused_with_the_reason_and_the_rule(self):
        code, out, err = self.go("Build.\n", "--hard", "56m")
        self.assertEqual(code, 1)
        self.assertIn("not renewed", err)
        self.assertIn("#1034", err)
        self.assertEqual(self.stub_calls(), [])   # nothing was made
        self.assertEqual(self.jsonl("kubectl.log"), [])
        for ok in ("55m", "30m", "3300s"):
            self.setUp()
            self.assertEqual(self.go("Build.\n", "--hard", ok)[0], 0)
        self.setUp()
        self.assertEqual(self.go("Build.\n", "--hard", "1h")[0], 1)

    def test_s11_the_mutant_that_accepts_a_longer_hard_limit_fails(self):
        with mock.patch.object(cw, "hard_limit_problem", lambda hard: None):
            code, out, err = self.go("Build.\n", "--hard", "56m")
        self.assertEqual(code, 0)   # the mutant lets it through ...
        self.assertNotEqual(self.stub_calls(), [])   # ... and the pod is made, which the test above forbids

    def test_s11_one_warning_for_a_linked_issue_of_the_repository_then_it_continues(self):
        code, out, err = self.go("See https://github.com/acme-org/widget-service/issues/12 and acme-org/widget-service#13.\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.warnings(err)), 1)
        self.setUp()
        code, out, err = self.go("See https://github.com/other-org/thing/issues/12.\n")
        self.assertEqual(self.warnings(err), [])

    def test_u3_the_two_temporary_rules_are_named_constants_in_one_module_with_their_asks(self):
        text = (ROOT / "bin" / "cloud_worker.py").read_text()
        for name, ask in (("TEMP_HARD_LIMIT_MINUTES", "#1034"), ("TEMP_ISSUE_LINK_WARNING", "#1019")):
            lines = [l for l in text.splitlines() if l.startswith(name + " =")]
            self.assertEqual(len(lines), 1, name)
            self.assertIn(ask, lines[0])
        self.assertEqual(cw.TEMP_HARD_LIMIT_MINUTES, 55)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:] if a != "--"], verbosity=1)
