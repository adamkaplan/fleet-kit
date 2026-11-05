"""#1380/#1429: the relay thread obeys the installed-tree guard. The fixtures (FakeBackend, FakeHub, Fixture) are those of bin/test-cloud-relay-daemon,
loaded from it (it is not a package, and that file is at its size limit)."""
import importlib.machinery
import importlib.util
import threading
import types
import unittest
from pathlib import Path

from support.paths import REPO

_loader = importlib.machinery.SourceFileLoader("relay_daemon_fixtures", str(REPO / "bin" / "test-cloud-relay-daemon"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
_fx = importlib.util.module_from_spec(_spec)
_loader.exec_module(_fx)
Fixture, relay, CODER, TESTER, PROVE, VERDICT = _fx.Fixture, _fx.relay, _fx.CODER, _fx.TESTER, _fx.PROVE, _fx.VERDICT


class RelayObeysTheTreeGuard(Fixture):
    """#1380/#1429 on the relay thread: the pass loop sets `blocked` (RelayLoop.set_blocked); while it is set the real RelayLoop thread answers
    every pulled request with the named refusal and decides nothing. A loop built by run_daemon (guarded=True) is blocked until the first guard check.
    Mutant: the flag ignored (Executor never asks it) -> the refusal tests fail."""

    def setUp(self):
        super().setUp()
        self.tok = Path(self.state) / "token"
        self.tok.write_text("t")
        self.tok.chmod(0o600)
        self.notes = []
        self.engine = types.SimpleNamespace(state_dir=self.state, config={}, _note=lambda *a: self.notes.append(a))
        self.env = {"FLEET_RELAY_HUB_URL": "http://127.0.0.1:9", "FLEET_RELAY_HUB_TOKEN_FILE": str(self.tok)}
        self.saved = (relay.http_transport, relay.SwitchboardBackend)
        relay.http_transport = lambda url, token: (self.hub.fetch, self.hub.post)
        relay.SwitchboardBackend = lambda *a, **k: self.backend
        self.addCleanup(lambda: (setattr(relay, "http_transport", self.saved[0]), setattr(relay, "SwitchboardBackend", self.saved[1])))

    def loop(self, guarded=True):
        return relay.RelayLoop(None, object(), None, lambda: 1000, environ=self.env, cadence=0.0, engine_factory=lambda: self.engine, guarded=guarded)

    def pull(self, loop, command="prove", pod=CODER, **args):
        """One real pass on the loop's own thread, via RelayLoop.run (one tick, then stop)."""
        rid = self.hub.submit(pod, command, **(args or PROVE))
        loop.sleep = lambda s: loop.stop_flag.set()
        t = threading.Thread(target=loop.run)
        t.start()
        t.join(30)
        self.assertFalse(t.is_alive())
        loop.stop_flag.clear()
        return self.hub.results[rid]

    def assert_refused_and_nothing_made(self, got):
        self.assertEqual(got["status"], 503)
        self.assertEqual(got["reason"], relay.TREE_REFUSAL)
        self.assertIsNone(got["record"])
        self.assertEqual(self.raw(), b"")
        self.assertEqual([k for k, d in self.backend.audits], ["relay.refused"])
        self.assertEqual(self.backend.audits[0][1]["why"], relay.TREE_REFUSAL)
        self.assertEqual(self.backend.reported, [])

    def test_the_first_call_before_any_guard_check_is_refused(self):
        self.assert_refused_and_nothing_made(self.pull(self.loop()))

    def test_a_bad_tree_refuses_prove_verdict_gates_and_report_by_name(self):
        loop = self.loop()
        loop.set_blocked(False)
        loop.set_blocked(True)
        for command, args in (("prove", PROVE), ("verdict", VERDICT), ("gates", {"x": 1}), ("report", {"state": "working", "text": "hi"})):
            got = self.pull(loop, command, **args)
            self.assertEqual((got["status"], got["reason"], got["record"]), (503, relay.TREE_REFUSAL, None), command)
        self.assertEqual(self.raw(), b"")
        self.assertEqual(self.backend.reported, [])
        self.assertEqual({k for k, d in self.backend.audits}, {"relay.refused"})

    def test_a_clear_tree_serves(self):
        loop = self.loop()
        loop.set_blocked(False)
        got = self.pull(loop)
        self.assertEqual((got["status"], got["reason"]), (200, "recorded"))
        self.assertEqual(len(self.entries()), 1)

    def test_it_goes_quiet_when_the_tree_goes_bad_and_resumes_when_clear(self):
        loop = self.loop()
        loop.set_blocked(False)
        self.assertEqual(self.pull(loop)["status"], 200)
        loop.set_blocked(True)
        self.assertEqual(self.pull(loop, "verdict", TESTER, **VERDICT)["status"], 503)
        self.assertEqual(len(self.entries()), 1)
        loop.set_blocked(False)
        self.assertEqual(self.pull(loop, "verdict", TESTER, **VERDICT)["status"], 200)
        self.assertEqual(len(self.entries()), 2)

    def test_a_loop_without_a_guard_is_never_blocked(self):
        self.assertEqual(self.pull(self.loop(guarded=False))["status"], 200)

    def test_the_relay_thread_never_reads_the_tree_itself(self):
        self.assertNotIn("installed_tree_problem", Path(relay.__file__).read_text())

    def test_mutant_the_flag_ignored_is_caught(self):
        src = Path(relay.__file__).read_text()
        self.assertIn("if self.blocked is not None and self.blocked():", src)
        mutant = types.ModuleType("relay_mutant")
        mutant.__file__ = relay.__file__
        exec(compile(src.replace("if self.blocked is not None and self.blocked():", "if False:"), "relay_mutant", "exec"), mutant.__dict__)
        ex = mutant.Executor(self.state, self.backend, lambda: 1000, lambda: True)
        rid = self.hub.submit(CODER, "prove", **PROVE)
        mutant.pass_once(ex, self.hub.fetch, self.hub.post)
        self.assertNotEqual(self.hub.results[rid]["reason"], relay.TREE_REFUSAL)   # the guard test would see a record: it fails
        self.assertEqual(len(self.entries()), 1)
