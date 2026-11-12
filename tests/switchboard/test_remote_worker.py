"""Unit tests for the remote-worker package. No network: the Floor carrier is tested in test_floor_carrier.py."""
import sys
import unittest
from pathlib import Path

# The mutation tests of test_brief_integration copy bin/, docs/ and tests/ only: in such a copy the package is absent and these classes skip.
# In the repository the package is there, and a failing import is a failure, never a skip.
ROOT = Path(__file__).resolve().parents[2]
PRESENT = (ROOT / "remote_worker").is_dir()
only_with_package = unittest.skipUnless(PRESENT, "remote_worker is not in this copy of the tree")
sys.path.insert(0, str(ROOT))   # the repository, for the remote_worker package

from support.core import Base

if PRESENT:
    from remote_worker import (Config, ConfigError, SpecError, TransportError, WorkerHandle, launch_remote_worker, load_config,
                               register_transport, transport_from_config, validate_spec, wait_for_end, Transport)
    from remote_worker import launch as launch_mod
    from remote_worker.fake import FakeTransport

LAND = "acme-land --now"
GOOD = dict(name="widget-1", repo="acme/widget-api", branch="work/widget-1", task="Fix the parser.", land_command=LAND)
SHA = "0" * 39 + "1"


@only_with_package
class TestRemoteSpec(Base):
    def test_valid_spec_brief_carries_task_and_land_command(self):
        spec = validate_spec(**GOOD)
        self.assertIn("Fix the parser.", spec.brief)
        self.assertIn(LAND, spec.brief)
        self.assertEqual(spec.land_command, LAND)

    def test_each_bad_input_is_refused_before_any_call(self):
        bad = [dict(name="Bad Name"), dict(name=""), dict(name="a" * 64), dict(repo="noslash"), dict(repo="a/b.git"), dict(repo="a/../b"),
               dict(branch="x..y"), dict(branch="-x"), dict(branch="a/"), dict(task="  "), dict(task="x" * 100_001),
               dict(land_command=""), dict(land_command="a\nb"), dict(land_command="x" * 501), dict(idle_seconds=0), dict(hard_seconds=True),
               dict(hard_seconds=10 ** 9)]
        for change in bad:
            t = FakeTransport()
            with self.subTest(change=list(change)):
                with self.assertRaises(SpecError):
                    launch_remote_worker(t, **{**GOOD, **change})
                self.assertEqual(t.calls, [])


@only_with_package
class TestRemoteLaunch(Base):
    def test_launch_starts_and_returns_handle(self):
        t = FakeTransport()
        h = launch_remote_worker(t, **GOOD)
        self.assertEqual((h.name, h.transport), ("widget-1", "fake"))
        self.assertEqual(t.workers["widget-1"]["spec"].repo, "acme/widget-api")

    def test_repeat_launch_is_idempotent(self):
        t = FakeTransport()
        a = launch_remote_worker(t, **GOOD)
        b = launch_remote_worker(t, **GOOD)
        self.assertEqual(a, b)
        self.assertEqual(len(t.workers), 1)

    def test_transport_error_passes_through(self):
        t = FakeTransport()
        t.fail_next["start"] = TransportError("busy", retryable=True)
        with self.assertRaises(TransportError) as cm:
            launch_remote_worker(t, **GOOD)
        self.assertTrue(cm.exception.retryable)

    def test_wrong_handle_is_stopped_and_refused(self):
        class Wrong(FakeTransport):
            def start(self, spec):
                h = super().start(spec)
                return WorkerHandle(name="other", worker_id=h.worker_id, transport="fake")
        t = Wrong()
        with self.assertRaises(TransportError):
            launch_remote_worker(t, **GOOD)
        self.assertIn("stop", [c[0] for c in t.calls])

    def test_fake_satisfies_the_interface(self):
        self.assertIsInstance(FakeTransport(), Transport)


@only_with_package
class TestRemoteWait(Base):
    def setUp(self):
        super().setUp()
        self.t = FakeTransport()
        self.h = launch_remote_worker(self.t, **GOOD)
        self.now = 0.0
        self.sleeps = []

    def _sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def wait(self, timeout, **kw):
        return wait_for_end(self.t, self.h, timeout, poll=5, sleep=self._sleep, clock=lambda: self.now, **kw)

    def test_returns_terminal_state_with_sha(self):
        self.t.finish("widget-1", "done", "landed " + SHA, SHA)
        st = self.wait(60)
        self.assertEqual((st.state, st.landed_sha), ("done", SHA))
        self.assertEqual(self.sleeps, [])

    def test_times_out_with_non_terminal_status(self):
        st = self.wait(12)
        self.assertEqual(st.state, "queued")
        self.assertLessEqual(sum(self.sleeps), 12)

    def test_retryable_error_is_a_missed_poll_but_other_error_propagates(self):
        self.t.fail_next["status"] = TransportError("net", retryable=True)
        self.t.finish("widget-1", "failed")
        self.assertEqual(self.wait(60).state, "failed")
        self.t.fail_next["status"] = TransportError("no", retryable=False)
        with self.assertRaises(TransportError):
            self.wait(60)


@only_with_package
class TestRemoteFake(Base):
    def test_send_dedups_by_message_id_and_refuses_after_end(self):
        t = FakeTransport()
        h = launch_remote_worker(t, **GOOD)
        t.send(h, "hi", "m1"); t.send(h, "hi", "m1")
        self.assertEqual(t.workers["widget-1"]["messages"], ["hi"])
        t.stop(h, "x"); t.stop(h, "x")
        with self.assertRaises(TransportError):
            t.send(h, "late")


@only_with_package
class TestRemoteConfig(Base):
    def test_both_settings_required(self):
        with self.assertRaises(ConfigError):
            load_config({})
        with self.assertRaises(ConfigError):
            load_config({launch_mod.ENV_TRANSPORT: "fake"})
        with self.assertRaises(ConfigError):
            load_config({launch_mod.ENV_LAND: LAND})

    def test_land_command_is_taken_from_config_not_code(self):
        cfg = load_config({launch_mod.ENV_TRANSPORT: "fake", launch_mod.ENV_LAND: "my-land --x"})
        self.assertEqual(cfg, Config("fake", "my-land --x"))
        spec = validate_spec(**{**GOOD, "land_command": cfg.land_command})
        self.assertIn("my-land --x", spec.brief)
        self.assertNotIn(LAND, spec.brief)

    def test_bad_land_command_in_config(self):
        with self.assertRaises(ConfigError):
            load_config({launch_mod.ENV_TRANSPORT: "fake", launch_mod.ENV_LAND: "a\x00b"})

    def test_registry(self):
        register_transport("fake-for-test", FakeTransport)
        self.assertIsInstance(transport_from_config(Config("fake-for-test", LAND)), FakeTransport)
        with self.assertRaises(ConfigError):
            register_transport("fake-for-test", FakeTransport)
        with self.assertRaises(ConfigError):
            transport_from_config(Config("nope", LAND))


if __name__ == "__main__":
    unittest.main()
