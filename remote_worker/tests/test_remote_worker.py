"""Unit tests for the remote-worker package. No network: the Floor transport gets an in-memory HTTP function."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from remote_worker import (Config, ConfigError, SpecError, TransportError, WorkerHandle, launch_remote_worker, load_config,
                           register_transport, transport_from_config, validate_spec, wait_for_end, Transport)
from remote_worker import launch as launch_mod
from remote_worker.fake import FakeTransport
from remote_worker.floor import FloorTransport

LAND = "acme-land --now"
GOOD = dict(name="widget-1", repo="acme/widget-api", branch="work/widget-1", task="Fix the parser.", land_command=LAND)
SECRET = "tok_abcdef0123456789"
SHA = "0" * 39 + "1"


class SpecTests(unittest.TestCase):
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


class LaunchTests(unittest.TestCase):
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

    def test_fake_and_floor_satisfy_the_interface(self):
        self.assertIsInstance(FakeTransport(), Transport)
        self.assertIsInstance(floor_with(lambda *a: (200, b"{}")), Transport)


class WaitTests(unittest.TestCase):
    def setUp(self):
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


class FakeTests(unittest.TestCase):
    def test_send_dedups_by_message_id_and_refuses_after_end(self):
        t = FakeTransport()
        h = launch_remote_worker(t, **GOOD)
        t.send(h, "hi", "m1"); t.send(h, "hi", "m1")
        self.assertEqual(t.workers["widget-1"]["messages"], ["hi"])
        t.stop(h, "x"); t.stop(h, "x")
        with self.assertRaises(TransportError):
            t.send(h, "late")


class ConfigTests(unittest.TestCase):
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


class Recorder:
    """An in-memory HTTP function: records requests and answers from a queue."""
    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((method, url, dict(headers), json.loads(body) if body else None))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        status, obj = a
        return status, obj if isinstance(obj, bytes) else json.dumps(obj).encode()


def floor_with(http, **kw):
    args = dict(base_url="https://floor.example:8080", token_env="FLOOR_TEST_TOKEN", git_base="https://git.example/",
                environ={"FLOOR_TEST_TOKEN": SECRET}, http=http)
    args.update(kw)
    return FloorTransport(**args)


SID = "ses_0123456789abcdef"
H = WorkerHandle("widget-1", SID, "floor")


class FloorTests(unittest.TestCase):
    def test_start_request_shape_and_handle(self):
        http = Recorder((201, {"session_id": SID, "state": "queued", "created": True}))
        h = launch_remote_worker(floor_with(http), **GOOD)
        self.assertEqual(h, H)
        m, url, headers, body = http.requests[0]
        self.assertEqual((m, url), ("POST", "https://floor.example:8080/v1/sessions"))
        self.assertEqual(headers["Authorization"], "Bearer " + SECRET)
        self.assertEqual(body["client_key"], "widget-1")
        self.assertEqual(body["repo"], "https://git.example/acme/widget-api.git")
        self.assertEqual(body["ref"], "work/widget-1")
        self.assertIn(LAND, body["brief"])
        self.assertNotIn("land_command", body)   # the service holds the land command in its own configuration

    def test_repeat_start_200_returns_same_session(self):
        http = Recorder((200, {"session_id": SID, "state": "running", "created": False}))
        self.assertEqual(launch_remote_worker(floor_with(http), **GOOD), H)

    def test_send_status_last_message_stop_paths(self):
        http = Recorder((202, {"queued": True}), (200, {"state": "done", "busy": False, "last_update": "2026-11-11T09:30:12Z", "landed_sha": SHA}),
                        (200, {"text": "landed " + SHA}), (200, {"state": "retired"}), (404, {"error": {"code": "not_found", "message": "x"}}))
        t = floor_with(http)
        t.send(H, "more", "m-1")
        st = t.status(H)
        self.assertEqual((st.state, st.busy, st.landed_sha), ("done", False, SHA))
        self.assertEqual(t.last_message(H), "landed " + SHA)
        t.stop(H, "r")
        t.stop(H, "r")   # a 404 on retire is already stopped
        self.assertEqual([(r[0], r[1].split("/v1")[1]) for r in http.requests], [
            ("POST", "/sessions/%s/messages" % SID), ("GET", "/sessions/%s/activity" % SID), ("GET", "/sessions/%s/last-message" % SID),
            ("DELETE", "/sessions/" + SID), ("DELETE", "/sessions/" + SID)])
        self.assertEqual(http.requests[0][3], {"text": "more", "message_id": "m-1"})

    def test_errors_are_classified_and_never_carry_the_token_or_body(self):
        cases = [((429, {"error": {"code": "busy", "message": SECRET}}), True), ((401, {"error": {"code": "unauthorized", "message": SECRET}}), False),
                 ((503, b"<html>" + SECRET.encode()), True), ((409, {"error": {"code": "terminal", "message": "x"}}), False)]
        for answer, retry in cases:
            with self.subTest(status=answer[0]):
                t = floor_with(Recorder(answer))
                with self.assertRaises(TransportError) as cm:
                    t.send(H, "x")
                self.assertEqual(cm.exception.retryable, retry)
                self.assertNotIn(SECRET, str(cm.exception))

    def test_network_failure_is_retryable(self):
        with self.assertRaises(TransportError) as cm:
            floor_with(Recorder(ConnectionRefusedError(SECRET))).status(H)
        self.assertTrue(cm.exception.retryable)
        self.assertNotIn(SECRET, str(cm.exception))

    def test_bad_answers_are_refused(self):
        for answer in [(200, {"state": "weird"}), (200, {"state": "done", "landed_sha": "abc"}), (200, b"not json")]:
            with self.subTest(answer=answer):
                with self.assertRaises(TransportError):
                    floor_with(Recorder(answer)).status(H)
        with self.assertRaises(TransportError):
            floor_with(Recorder((201, {"session_id": "../x"}))).start(validate_spec(**GOOD))

    def test_foreign_handle_is_refused_without_a_call(self):
        http = Recorder()
        for bad in (WorkerHandle("w", SID, "fake"), WorkerHandle("w", "ses_../x", "floor")):
            with self.assertRaises(TransportError):
                floor_with(http).status(bad)
        self.assertEqual(http.requests, [])

    def test_construction_refuses_unsafe_settings(self):
        bad = [dict(base_url="http://floor.example"), dict(base_url="ftp://x"), dict(base_url="https://u:p@floor.example"),
               dict(base_url="https://floor.example/path"), dict(git_base="http://git.example"), dict(git_base="https://u:p@git.example"),
               dict(environ={}), dict(environ={"FLOOR_TEST_TOKEN": "a b"})]
        for change in bad:
            with self.subTest(change=list(change)):
                with self.assertRaises(TransportError):
                    floor_with(Recorder(), **change)
        floor_with(Recorder(), base_url="http://127.0.0.1:8080")   # loopback http is allowed

    def test_oversize_message_refused_without_a_call(self):
        http = Recorder()
        with self.assertRaises(TransportError):
            floor_with(http).send(H, "x" * (256 * 1024 + 1))
        self.assertEqual(http.requests, [])

    def test_token_is_not_in_repr_of_anything_returned(self):
        t = floor_with(Recorder((201, {"session_id": SID})))
        self.assertNotIn(SECRET, repr(launch_remote_worker(t, **GOOD)))


if __name__ == "__main__":
    unittest.main()
