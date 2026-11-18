"""#2553: `remind` and `send` do not run a full pane discovery for a repo-qualified issue or a registered sender, bound their external
calls with one retry, and write one `cli.timing` audit line when they take over CLI_SLOW_SECONDS."""
import argparse
import subprocess
import threading
from unittest import mock

from support.loader import sb

REPO_NAME = "acme-org/widget-service"


def build(Base, World, FAKE_OPENCODE, FAKE_HERDR, captured):
    class TestRemindSendFastPath(Base):
        def setUp(self):
            super().setUp()
            self.sb = sb
            self.world = World(self, agents=("cos", "platform", "widget-lead"))
            self.engine = self.world.engine
            self.cfg = dict(self.world.config, send_timeout_seconds=0.05)
            self.calls = 0
            real = self.engine.discover

            def counted(*a, **k):
                self.calls += 1
                return real(*a, **k)
            self.engine.discover = counted

        def register(self, name="widget-lead", **row):
            self.sb.load_module("registry").upsert(self.world.state, name, dict({"role": "coder", "repo": REPO_NAME}, **row))

        def resolve(self, issue, sender="widget-lead"):
            self.calls = 0
            return self.sb.resolve_for_sender(self.engine, self.cfg, self.sb.parse_issue_ref(issue), sender)

        # ---- S1 / U1
        def check_fast_path(self):
            self.assertEqual(self.resolve("%s#10" % REPO_NAME), (REPO_NAME, 10))
            self.assertEqual(self.calls, 0, "discover ran for a repo-qualified issue")
            self.register()
            self.assertEqual(self.resolve("10"), (REPO_NAME, 10))
            self.assertEqual(self.calls, 0, "discover ran for a registered sender")

        def test_s1_no_discovery_for_a_qualified_issue_or_a_registered_sender(self):
            self.check_fast_path()

        def test_s1_an_unregistered_or_retired_sender_falls_back_to_discovery(self):
            self.resolve("10", "ghost")
            self.assertEqual(self.calls, 1)
            self.register()
            self.sb.load_module("registry").retire(self.world.state, "widget-lead", "cos", "done")
            self.resolve("10")
            self.assertEqual(self.calls, 1)

        def test_u1_an_unreadable_registry_falls_back_to_discovery(self):
            self.register()
            with mock.patch.object(self.sb.load_module("registry"), "read", side_effect=ValueError("corrupt")):
                self.resolve("10")
            self.assertEqual(self.calls, 1)
            (self.world.state / "registry.json").write_text("{not json")
            self.resolve("10")
            self.assertEqual(self.calls, 1)

        # ---- the two commands
        def remind(self, ref_side_effect, advance=0):
            def read(*a, **k):
                if advance:
                    self.world.clock.t += advance
                value = next(effects)
                if isinstance(value, BaseException):
                    raise value
                return value
            effects = iter(ref_side_effect)
            args = argparse.Namespace(name="platform", when="10m", text=["x"], issue=self.sb.parse_issue_ref("%s#10" % REPO_NAME), until_closed=None, until_merged=None,
                                      keep_if_closed=False, key=None, sender="coder", list_mine=False, cancel=None)
            self.reads = 0
            self.fresh()
            with captured() as (out, err), \
                    mock.patch.object(self.sb, "build_engine", return_value=(self.engine, self.world.state, self.cfg)), \
                    mock.patch.object(self.sb, "read_ref_state", side_effect=lambda *a, **k: self.count(read, *a, **k)):
                code = self.sb.cmd_remind(args, None, self.world.clock.now)
            return code, err.getvalue()

        def count(self, read, *a, **k):
            self.reads += 1
            return read(*a, **k)

        def send(self, hang_first=0, advance=0):
            release = threading.Event()
            self.addCleanup(release.set)
            self.sends = 0
            self.fresh()

            def deliver(*a, **k):
                self.sends += 1
                if self.sends <= hang_first:
                    release.wait(30)
                self.world.clock.t += advance
                return {"sent": True, "held": False, "action": "queued", "reason": "", "message_id": "msg_ses_example"}
            with captured() as (out, err), \
                    mock.patch.object(self.sb, "build_engine", return_value=(self.engine, self.world.state, self.cfg)), \
                    mock.patch.object(self.sb, "load_config", return_value=self.world.config), \
                    mock.patch.object(self.engine, "send", side_effect=deliver):
                code = self.sb.main(["send", "--from", "cos", "--issue", "%s#10" % REPO_NAME, "platform", "hi"], runner=self.world.route,
                                    now=self.world.clock.now, wait=lambda sock, seconds: False, socket_call=self.world.herdr.socket)
            return code, err.getvalue()

        def fresh(self):
            self.sb._CLI_TIMING.update(discover_ms=0, gh_ms=0, lock_ms=0, v2_ms=0)   # each command builds its own engine

        def timings(self):
            return [e for e in self.world.audit() if e["event"] == "cli.timing"]

        # ---- S2
        def check_retry(self):
            code, err = self.remind([TimeoutError("gh"), "open"])
            self.assertEqual((code, self.reads), (0, 2), err)
            code, err = self.remind([TimeoutError("gh"), TimeoutError("gh")])
            self.assertEqual((code, self.reads), (1, 2), err)
            self.assertIn("step gh_ref_state", err)
            code, err = self.send(hang_first=1)
            self.assertEqual((code, self.sends), (0, 2), err)
            code, err = self.send(hang_first=2)
            self.assertEqual((code, self.sends), (1, 2), err)
            self.assertIn("step v2_send timed out", err)
            with mock.patch.object(self.engine, "gh_runner", side_effect=subprocess.TimeoutExpired(["gh"], 8)), self.assertRaises(TimeoutError):
                self.sb.read_ref_state(self.engine, REPO_NAME, 10, "issue")

        def test_s2_one_retry_then_a_named_failure(self):
            self.check_retry()

        # ---- S3
        def check_timing(self):
            self.world.audit_path.write_text("")
            self.assertEqual(self.remind(["open"], advance=1)[0], 0)
            self.assertEqual(self.send(advance=1)[0], 0)
            self.assertEqual(self.timings(), [], "a fast call wrote a timing line")
            self.assertEqual(self.remind(["open"], advance=6)[0], 0)
            self.assertEqual(len(self.timings()), 1, "a slow call wrote no timing line")
            row = self.timings()[0]
            self.assertEqual(sorted(k for k in row["detail"] if k.endswith("_ms")), ["discover_ms", "gh_ms", "lock_ms", "total_ms", "v2_ms"])
            self.assertTrue(all(isinstance(row["detail"][k], int) for k in ("discover_ms", "gh_ms", "lock_ms", "v2_ms")))
            self.assertGreaterEqual(row["detail"]["gh_ms"], 6000)
            self.assertEqual(self.send(advance=6)[0], 0)
            self.assertEqual(len(self.timings()), 2)
            self.assertGreaterEqual(self.timings()[1]["detail"]["v2_ms"], 6000)

        def test_s3_a_slow_call_writes_one_timing_line_a_fast_one_none(self):
            self.check_timing()

        # ---- S4: each check holds on the real code, and fails on its mutant
        def mutant(self, name, replacement):
            return mock.patch.object(self.sb, name, replacement)

        def test_s4_discovery_bypass_mutant(self):
            self.check_fast_path()
            real = self.sb.resolve_for_sender

            def always_discover(engine, config, issue, sender):
                engine.discover(engine.now())
                return real(engine, config, issue, sender)
            with self.mutant("resolve_for_sender", always_discover):
                with self.assertRaisesRegex(AssertionError, r"^1 != 0 : discover ran for a repo-qualified issue"):
                    self.check_fast_path()

        def test_s4_retry_mutant(self):
            self.check_retry()
            real = self.sb.run_bounded
            depth = []

            def never_delivers(work, seconds):   # the outer bound is real; the delivery inside it always times out, so a retry cannot help
                if depth:
                    return False, None
                depth.append(1)
                try:
                    return real(work, seconds)
                finally:
                    depth.pop()
            with mock.patch.object(self.sb, "run_bounded", never_delivers):
                with self.assertRaisesRegex(AssertionError, r"^Tuples differ: \(1, 0\) != \(0, 2\)"):
                    self.check_retry()

        def test_s4_timing_mutant(self):
            self.check_timing()
            with mock.patch.object(self.sb, "CLI_SLOW_SECONDS", 1000.0):
                with self.assertRaisesRegex(AssertionError, r"^0 != 1 : a slow call wrote no timing line"):
                    self.check_timing()

    return TestRemindSendFastPath
