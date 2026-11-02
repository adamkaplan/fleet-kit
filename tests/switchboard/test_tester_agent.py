"""#1963: `launch --role tester` is refused for an agent whose definition does not declare the role. build() gives the tests the fixtures of bin/test-switchboard.

The checkout is PLANTED in a temp directory (a copy of bin/tester_agent.py and a few definitions; the entry's own root, found from its `__file__`, is recorded
and asserted to be this checkout, and the plant is what the module reads); the three routes (terminal, headless, cloud) are driven with the fakes of the launch tests, `run_cloud` faked."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

from support.loader import captured, sb

REPO = Path(__file__).resolve().parents[2]
TESTER_LINE = "fleet-roles: coder, tester"
REFUSAL_TEXT = "needs an agent that works a brief and records verdicts"


def tester_agent_definition(name, roles=TESTER_LINE, named=True):
    front = ["---"] + (["name: %s" % name] if named else []) + (([roles] if roles else [])) + ["description: planted", "mode: primary", "---", "body"]
    return "\n".join(front) + "\n"


def tester_agent_plant(base, definitions, module_source=None, agents_dir=True):
    """A checkout under `base`: bin/tester_agent.py (a copy, or the given source) and agents/opencode/<file>.md per `definitions` {file name: text}."""
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "tester_agent.py").write_text(module_source if module_source is not None else (REPO / "bin" / "tester_agent.py").read_text())
    if agents_dir:
        folder = base / "agents" / "opencode"
        folder.mkdir(parents=True)
        for file_name, text in definitions.items():
            (folder / (file_name + ".md")).write_text(text)
    return base


def tester_agent_standard():
    return {"alpha": tester_agent_definition("alpha"), "beta": tester_agent_definition("beta", "fleet-roles: tester"),
            "build": tester_agent_definition("build", roles=None)}


def build():
    host = HOST   # set by bin/test-switchboard: the fixtures of that file

    class TesterAgentFixture(host.LedgerBase):
        """The planted checkout and the three routes' fakes (not collected: only the two classes returned below are)."""

        def setUp(self):
            super().setUp()
            self.work = self.home / "work"
            self.work.mkdir()
            self.tmp = Path(tempfile.mkdtemp(prefix="tester-agent-"))
            self.addCleanup(shutil.rmtree, self.tmp, True)
            self.cloud_calls, self.loaded = [], []
            self.before = len(self.world.v2.sessions)
            real_load = sb.load_module
            self.planted = None
            self.roots = []

            def spy(name):
                self.loaded.append(name)
                if name != "tester_agent" or self.planted is None:
                    return real_load(name)
                spec = importlib.util.spec_from_file_location("tester_agent_planted", str(self.planted / "bin" / "tester_agent.py"))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)

                def line_for(agent, root):   # the entry's own root is recorded (it must be this checkout), the plant is what is read
                    self.roots.append(root)
                    return module.line_for(agent, str(self.planted))
                return types.SimpleNamespace(line_for=line_for)
            patch = mock.patch.object(sb, "load_module", spy)
            patch.start()
            self.addCleanup(patch.stop)
            patch = mock.patch.object(sb, "run_cloud", lambda function, *a: (self.cloud_calls.append(function) if function == "cmd_launch" else None) or 0)
            patch.start()
            self.addCleanup(patch.stop)

        def plant(self, definitions, **kwargs):
            base = tester_agent_plant(self.tmp / ("c%d" % len(list(self.tmp.iterdir()))), definitions, **kwargs)
            self.planted = base
            return base

        def launch(self, agent, role, route, name="newcoder"):
            host.os.environ["HERDR_WORKSPACE_ID"] = "w1"
            host.os.environ.pop("HERDR_PANE_ID", None)
            argv = ["launch", name, "--dir", str(self.work), "--brief-file", "-", route]
            if agent is not None:
                argv += ["--agent", agent]
            if role is not None:
                argv += ["--role", role]
            with captured() as (out, err), mock.patch.object(sys, "stdin", io.StringIO("Do it.\n")):
                code = sb.main(argv, runner=self.route, now=self.world.clock.now, socket_call=self.world.herdr.socket)
            return code, out.getvalue(), err.getvalue()

        def started(self):
            """What the launch left: sessions, headless records, cloud calls (0 of each for a refusal)."""
            headless = list((self.world.state / "headless").glob("*.json")) if (self.world.state / "headless").is_dir() else []
            return len(self.world.v2.sessions) - self.before, len(headless), len(self.cloud_calls)

        def assert_refused(self, err, agent, allowed, first):
            lines = [line for line in err.splitlines() if line.strip()]
            self.assertEqual(len(lines), 1, err)
            self.assertEqual(lines[0], "fleet-switchboard: launch: --role tester needs an agent that works a brief and records verdicts: %s is not one (allowed: %s); "
                                       "run: fleet-switchboard launch NAME --agent %s --role tester ...; nothing was started" % (agent, allowed, first))

    class TestTesterAgentLaunch(TesterAgentFixture):
        """S2 to S4, S6 (launch part), U1: the check at the top of the launch, on each of the three routes."""

        ROUTES = ("--terminal", "--headless", "--cloud")

        def test_s2_each_allowed_agent_is_accepted_on_each_route_and_build_is_refused_on_each(self):
            self.plant(tester_agent_standard())
            accepted = 0
            for route in self.ROUTES:
                for agent, name in (("alpha", "wa"), ("beta", "wb")):
                    with self.subTest(route=route, agent=agent):
                        code, out, err = self.launch(agent, "tester", route, name=name + route.strip("-"))
                        self.assertEqual(code, 0, err)
                        accepted += 1
            self.assertEqual(accepted, 6)
            self.assertEqual(len(self.cloud_calls), 2)
            sessions = len(self.world.v2.sessions)
            for route in self.ROUTES:
                with self.subTest(route=route, agent="build"):
                    calls = len(self.cloud_calls)
                    code, out, err = self.launch("build", "tester", route, name="refused" + route.strip("-"))
                    self.assertEqual(code, 2)
                    self.assert_refused(err, "build", "alpha, beta", "alpha")
                    self.assertEqual(out, "")
                    self.assertEqual((len(self.world.v2.sessions), len(self.cloud_calls)), (sessions, calls), "nothing was started")
            self.assertFalse([p for p in (self.world.state / "headless").glob("refused*")] if (self.world.state / "headless").is_dir() else [])

        def test_s3_a_new_definition_is_accepted_and_a_removed_line_refuses_without_a_code_change(self):
            base = self.plant(tester_agent_standard())
            self.assertEqual(self.launch("gamma", "tester", "--terminal")[0], 2)   # not yet a tester
            (base / "agents" / "opencode" / "gamma.md").write_text(tester_agent_definition("gamma"))
            code, out, err = self.launch("gamma", "tester", "--terminal", name="g1")
            self.assertEqual(code, 0, err)   # the third is accepted
            (base / "agents" / "opencode" / "alpha.md").write_text(tester_agent_definition("alpha", roles=None))
            code, out, err = self.launch("alpha", "tester", "--terminal", name="a1")
            self.assertEqual(code, 2)   # the first, its line removed, is refused
            self.assert_refused(err, "alpha", "beta, gamma", "beta")

        def test_s3_the_name_is_the_file_name_when_the_definition_has_no_name_line(self):
            self.plant({"delta": tester_agent_definition("x", named=False)})
            self.assertEqual(self.launch("delta", "tester", "--terminal")[0], 0)

        def test_s4_an_unreadable_directory_and_a_directory_with_no_tester_refuse_in_one_line(self):
            for case, kwargs in (("unreadable", {"agents_dir": False}), ("no tester", {})):
                with self.subTest(case=case):
                    base = self.plant({"build": tester_agent_definition("build", roles="fleet-roles: coder")} if not kwargs else {}, **kwargs)
                    code, out, err = self.launch("coder", "tester", "--terminal", name="x" + case[:2])
                    self.assertEqual(code, 2)
                    lines = [line for line in err.splitlines() if line.strip()]
                    self.assertEqual(len(lines), 1, err)
                    self.assertIn(str(base / "agents" / "opencode"), lines[0])
                    self.assertIn("from the switchboard's checkout", lines[0])
                    self.assertEqual(self.started(), (0, 0, 0))

        def test_u1_other_roles_and_no_role_are_not_checked_and_read_nothing(self):
            self.plant({})   # a checkout that would refuse every tester launch
            for role in ("coder", "reviewer", "orchestrator", None):
                with self.subTest(role=role):
                    code, out, err = self.launch("build", role, "--terminal", name="n%s" % (role or "none")[:3])
                    self.assertEqual(code, 0, err)
                    self.assertNotIn(REFUSAL_TEXT, err)
            self.assertNotIn("tester_agent", self.loaded, "the directory of definitions was not read")

        def test_s6_the_kit_definitions_name_coder_as_the_one_tester_and_the_launch_from_the_checkout_accepts_it(self):
            for path in (REPO / "agents" / "opencode" / "coder.md", REPO / "agents" / "copilot" / "coder.agent.md"):
                self.assertEqual(path.read_text().split("\n---", 1)[0].splitlines().count(TESTER_LINE), 1, str(path))
            others = [p for folder in ("opencode", "copilot", "reviewers") for p in (REPO / "agents" / folder).glob("*.md")
                      if p.name not in ("coder.md", "coder.agent.md") and "tester" in p.read_text().split("\n---", 1)[0]]
            self.assertEqual(others, [])
            code, out, err = self.launch("coder", "tester", "--terminal", name="realtester")   # the real checkout, no plant
            self.assertEqual(code, 0, err)

        def test_the_entry_hands_the_check_the_checkout_above_its_own_bin(self):
            self.plant(tester_agent_standard())
            self.assertEqual(self.launch("alpha", "tester", "--terminal")[0], 0)
            self.assertEqual(self.roots, [str(REPO)])

    class TestTesterAgentMutants(TesterAgentFixture):
        """S3, S5: the module's source copied into a plant with one change, applied exactly once, each failing anchored on the check's own text beside a control."""

        SOURCE = (REPO / "bin" / "tester_agent.py").read_text()

        def run_plant(self, source, agent):
            self.plant(tester_agent_standard() | {"gamma": tester_agent_definition("gamma")}, module_source=source)
            return self.launch(agent, "tester", "--terminal", name="m" + agent)

        def assert_the_check(self, source, agent, accepted):
            """The one measure both the control and the mutant are shown to: `agent` is accepted (accepted True) or refused with the check's text."""
            code, out, err = self.run_plant(source, agent)
            if accepted:
                self.assertEqual(code, 0, err)
                self.assertNotIn(REFUSAL_TEXT, err)
            else:
                self.assertEqual(code, 2)
                self.assertIn("launch: --role tester %s: %s is not one (allowed:" % (REFUSAL_TEXT, agent), err)

        def mutate(self, old, new):
            self.assertEqual(self.SOURCE.count(old), 1, "the mutant applies exactly once")
            return self.SOURCE.replace(old, new)

        def test_s5_control_the_unmutated_copy_refuses_build(self):
            self.assert_the_check(self.SOURCE, "build", accepted=False)

        def test_s5_mutant_the_check_removed_is_caught_by_the_text_of_the_refusal(self):
            source = self.mutate('    folder = os.path.join(root, "agents", "opencode")\n', '    return None\n    folder = os.path.join(root, "agents", "opencode")\n')
            with self.assertRaises(AssertionError) as caught:
                self.assert_the_check(source, "build", accepted=False)
            self.assertIn("2 != 0", str(caught.exception).replace("0 != 2", "2 != 0"))   # the launch proceeded (0) where the refusal (2) is expected

        def test_s3_control_the_unmutated_copy_accepts_the_third_agent(self):
            self.assert_the_check(self.SOURCE, "gamma", accepted=True)

        def test_s3_mutant_a_list_typed_in_the_code_refuses_the_third_agent(self):
            source = self.mutate("    if agent in allowed:\n", '    allowed = ["alpha", "beta"]\n    if agent in allowed:\n')
            with self.assertRaises(AssertionError) as caught:
                self.assert_the_check(source, "gamma", accepted=True)
            self.assertIn("gamma is not one (allowed: alpha, beta)", str(caught.exception))

    return TestTesterAgentLaunch, TestTesterAgentMutants
