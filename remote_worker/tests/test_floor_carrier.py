"""Tests of the Floor carrier (queue for commands, blobs for state) against an in-memory HTTP layer. No network, no real credential."""
import base64
import json
import os
import re
import sys
import tempfile
import unittest
import urllib.parse
from xml.etree import ElementTree

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from remote_worker import Transport, TransportError, WorkerHandle, launch_remote_worker, wait_for_end
from remote_worker import floor as floor_mod
from remote_worker.floor import FloorTransport, load_sas_file

QSAS = "sv=2021-08-06&sp=a&sig=QUEUESECRET0123456789"
CSAS = "sv=2021-08-06&sp=rcw&sig=CONTAINERSECRET0123456789"
SECRETS = ("QUEUESECRET0123456789", "CONTAINERSECRET0123456789")
GOOD = dict(name="widget-1", repo="acme/widget-api", branch="work/widget-1", task="Fix the parser.", land_command="acme-land --now")
SHA = "0" * 39 + "1"
ACCOUNT, FLOOR, CALLER = "examplestore", "f1", "fleet"


class FakeStore:
    """The storage account: a queue that records messages, blobs that can be read back, and a switchable state.json with an ETag."""
    def __init__(self):
        self.messages = []    # decoded commands, in the order added
        self.raw = []         # (url, body) of each queue request
        self.blobs = {}
        self.requests = []
        self.fail = []        # answers consumed first: a status int or an exception
        self.etag = 0
        self.state = None

    def publish(self, sessions, acks=None, written_at="2026-11-12T09:30:12Z"):
        self.etag += 1
        self.state = {"v": 1, "floor": FLOOR, "seq": self.etag, "written_at": written_at, "sessions": sessions, "acks": acks or {}}

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((method, url, dict(headers), body))
        if self.fail:
            f = self.fail.pop(0)
            if isinstance(f, Exception):
                raise f
            return f, {}, b""
        u = urllib.parse.urlsplit(url)
        if method == "POST":
            self.raw.append((url, body))
            text = ElementTree.fromstring(body).find("MessageText").text
            self.messages.append(json.loads(base64.b64decode(text)))
            return 201, {}, b""
        name = u.path.split("/", 2)[2]
        if method == "PUT":
            self.blobs[name] = body
            return 201, {}, b""
        if name == "state.json":
            if self.state is None:
                return 404, {}, b""
            if headers.get("If-None-Match") == '"e%d"' % self.etag:
                return 304, {}, b""
            return 200, {"etag": '"e%d"' % self.etag}, json.dumps(self.state).encode()
        if name in self.blobs:
            return 200, {}, self.blobs[name]
        return 404, {}, b""


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def sas_file(d, name, text, mode=0o600):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.qf = sas_file(self.dir.name, "q", QSAS + "\n")
        self.cf = sas_file(self.dir.name, "c", "?" + CSAS)
        self.store = FakeStore()
        self.clock = Clock()

    def transport(self, **kw):
        args = dict(account=ACCOUNT, floor_id=FLOOR, caller_id=CALLER, queue_sas_file=self.qf, container_sas_file=self.cf,
                    git_base="https://git.example/", http=self.store, clock=self.clock, wall=self.clock, min_read_seconds=0)
        args.update(kw)
        return FloorTransport(**args)

    def launched(self, t=None):
        t = t or self.transport()
        return t, launch_remote_worker(t, **GOOD)


class CarrierTests(Base):
    def test_satisfies_the_interface(self):
        self.assertIsInstance(self.transport(), Transport)

    def test_launch_command_and_request_shape(self):
        t, h = self.launched()
        self.assertTrue(re.match(r"^ses_[0-9a-f]{16}$", h.worker_id))
        cmd = self.store.messages[0]
        self.assertEqual((cmd["v"], cmd["op"], cmd["seq"], cmd["session_id"]), (1, "launch", 1, h.worker_id))
        self.assertRegex(cmd["cmd_id"], r"^c-[0-9a-f]{16}$")
        self.assertEqual((cmd["repo"], cmd["ref"]), ("https://git.example/acme/widget-api.git", "work/widget-1"))
        self.assertIn("acme-land --now", cmd["brief"])
        self.assertNotIn("land_command", cmd)
        method, url, _, _ = self.store.requests[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.startswith("https://%s.queue.core.windows.net/cmd-f1-fleet/messages?messagettl=86400&" % ACCOUNT), url)
        self.assertTrue(url.endswith(QSAS))

    def test_every_message_carries_the_24_hour_ttl(self):
        t, h = self.launched()
        t.send(h, "more")
        t.stop(h, "done")
        for url, _ in self.store.raw:
            self.assertIn("messagettl=86400", url)
        self.assertEqual(len(self.store.raw), 3)

    def test_seq_numbers_count_up_per_session(self):
        t, h = self.launched()
        t.send(h, "one")
        t.send(h, "two")
        t.stop(h, "x")
        self.assertEqual([(c["op"], c["seq"]) for c in self.store.messages], [("launch", 1), ("send", 2), ("send", 3), ("retire", 4)])
        other = launch_remote_worker(t, **{**GOOD, "name": "widget-2"})
        t.send(other, "hello")
        self.assertEqual([c["seq"] for c in self.store.messages if c["session_id"] == other.worker_id], [1, 2])
        self.assertEqual(len({c["cmd_id"] for c in self.store.messages}), len(self.store.messages))

    def test_a_repeated_start_is_the_same_launch_even_from_a_new_process(self):
        _, h1 = self.launched()
        t2, h2 = self.launched(self.transport())
        self.assertEqual(h1, h2)
        a, b = self.store.messages
        self.assertEqual((a["cmd_id"], a["seq"]), (b["cmd_id"], b["seq"]))   # the service answers `duplicate`

    def test_failed_add_is_retried_with_the_same_cmd_id_and_seq(self):
        t = self.transport()
        self.store.fail = [503]
        with self.assertRaises(TransportError) as cm:
            launch_remote_worker(t, **GOOD)
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(self.store.messages, [])
        h = launch_remote_worker(t, **GOOD)   # the retry
        t.send(h, "next")
        self.assertEqual([(c["op"], c["seq"]) for c in self.store.messages], [("launch", 1), ("send", 2)])
        self.assertEqual(len(self.store.messages), 2)   # no seq skipped, nothing doubled

    def test_failed_send_is_added_again_before_the_next_command(self):
        t, h = self.launched()
        self.store.fail = [ConnectionResetError("x")]
        with self.assertRaises(TransportError):
            t.send(h, "lost", "m-1")
        t.send(h, "lost", "m-1")   # same message id: the held command goes out, nothing new is numbered
        t.send(h, "after")
        self.assertEqual([(c["op"], c["seq"], c.get("text")) for c in self.store.messages],
                         [("launch", 1, None), ("send", 2, "lost"), ("send", 3, "after")])

    def test_message_id_reuses_cmd_id_and_sends_once(self):
        t, h = self.launched()
        t.send(h, "same", "m-7")
        t.send(h, "same", "m-7")
        sends = [c for c in self.store.messages if c["op"] == "send"]
        self.assertEqual(len(sends), 1)

    def test_unacked_command_is_resent_unchanged_after_sixty_seconds(self):
        t, h = self.launched()
        self.store.publish({})
        t.status(h)
        self.assertEqual(len(self.store.messages), 1)
        self.clock.t += 61
        t.status(h)
        self.assertEqual(len(self.store.messages), 2)
        self.assertEqual(self.store.messages[0], self.store.messages[1])   # same cmd_id, same seq, same body

    def test_acked_command_is_not_resent_and_the_ack_is_readable(self):
        t, h = self.launched()
        cid = self.store.messages[0]["cmd_id"]
        self.assertEqual(t.command_result(cid), "pending")
        self.store.publish({}, acks={cid: "applied"})
        t.status(h)
        self.clock.t += 500
        t.status(h)
        self.assertEqual(len(self.store.messages), 1)
        self.assertEqual(t.command_result(cid), "applied")

    def test_rejected_launch_surfaces_as_an_error(self):
        t, h = self.launched()
        self.store.publish({}, acks={self.store.messages[0]["cmd_id"]: "rejected:bad_request"})
        with self.assertRaises(TransportError):
            t.status(h)

    def test_rejected_send_is_readable(self):
        t, h = self.launched()
        t.send(h, "late")
        cid = self.store.messages[1]["cmd_id"]
        self.store.publish({h.worker_id: {"state": "running", "busy": True}}, acks={cid: "rejected:terminal"})
        t.status(h)
        self.assertEqual(t.command_result(cid), "rejected:terminal")


class PayloadTests(Base):
    def test_a_large_brief_goes_to_a_blob_written_first(self):
        t = self.transport()
        big = "x" * (60 * 1024)
        h = launch_remote_worker(t, **{**GOOD, "task": big})
        cmd = self.store.messages[0]
        self.assertNotIn("brief", cmd)
        self.assertEqual(cmd["payload"], "payload/%s.json" % cmd["cmd_id"])
        moved = json.loads(self.store.blobs[cmd["payload"]])
        self.assertIn(big, moved["brief"])
        order = [r[0] for r in self.store.requests]
        self.assertEqual(order, ["PUT", "POST"])   # payload first, then the command
        self.assertTrue(self.store.requests[0][1].endswith(CSAS))
        self.assertLessEqual(len(self.store.raw[0][1]), 64 * 1024)
        self.assertEqual(h.transport, "floor")

    def test_a_large_send_text_goes_to_a_blob_and_a_small_one_does_not(self):
        t, h = self.launched()
        t.send(h, "small")
        t.send(h, "y" * (50 * 1024))
        small, large = self.store.messages[1], self.store.messages[2]
        self.assertEqual(small["text"], "small")
        self.assertNotIn("text", large)
        self.assertEqual(json.loads(self.store.blobs[large["payload"]])["text"], "y" * (50 * 1024))

    def test_payload_just_under_the_limit_stays_in_the_message(self):
        t, h = self.launched()
        t.send(h, "z" * (40 * 1024))
        self.assertEqual(self.store.messages[1]["text"], "z" * (40 * 1024))
        self.assertEqual([r[0] for r in self.store.requests], ["POST", "POST"])

    def test_failed_payload_write_leaves_the_command_unsent_and_it_goes_out_again(self):
        t = self.transport()
        self.store.fail = [500]
        with self.assertRaises(TransportError):
            launch_remote_worker(t, **{**GOOD, "task": "x" * (60 * 1024)})
        self.assertEqual(self.store.messages, [])
        launch_remote_worker(t, **{**GOOD, "task": "x" * (60 * 1024)})
        self.assertEqual(len(self.store.messages), 1)

    def test_over_256_kib_is_refused_without_a_call(self):
        t, h = self.launched()
        n = len(self.store.requests)
        with self.assertRaises(TransportError):
            t.send(h, "x" * (256 * 1024 + 1))
        self.assertEqual(len(self.store.requests), n)


class StateTests(Base):
    def test_status_reads_the_session_from_state_json(self):
        t, h = self.launched()
        self.store.publish({h.worker_id: {"state": "done", "busy": False, "last_update": "2026-11-12T09:30:12Z", "landed_sha": SHA}})
        st = t.status(h)
        self.assertEqual((st.state, st.busy, st.landed_sha, st.last_update), ("done", False, SHA, "2026-11-12T09:30:12Z"))
        url = [r for r in self.store.requests if r[0] == "GET"][0][1]
        self.assertTrue(url.startswith("https://%s.blob.core.windows.net/floor-f1-fleet/state.json?" % ACCOUNT))
        self.assertTrue(url.endswith(CSAS))

    def test_conditional_read_uses_the_etag_and_304_keeps_the_state(self):
        t, h = self.launched()
        self.store.publish({h.worker_id: {"state": "running", "busy": True}})
        self.assertEqual(t.status(h).state, "running")
        self.assertEqual(t.status(h).state, "running")
        gets = [r for r in self.store.requests if r[0] == "GET"]
        self.assertNotIn("If-None-Match", gets[0][2])
        self.assertEqual(gets[1][2]["If-None-Match"], '"e1"')
        self.store.publish({h.worker_id: {"state": "idle", "busy": False}})
        self.assertEqual(t.status(h).state, "idle")

    def test_minimum_read_interval_avoids_a_request(self):
        t, h = self.launched(self.transport(min_read_seconds=5))
        self.store.publish({h.worker_id: {"state": "running", "busy": True}})
        t.status(h)
        t.status(h)
        self.assertEqual(len([r for r in self.store.requests if r[0] == "GET"]), 1)
        self.clock.t += 6
        t.status(h)
        self.assertEqual(len([r for r in self.store.requests if r[0] == "GET"]), 2)

    def test_session_not_yet_listed_is_queued_and_a_finished_one_keeps_its_last_status(self):
        t, h = self.launched()
        self.assertEqual(t.status(h).state, "queued")   # no state.json yet
        self.store.publish({h.worker_id: {"state": "done", "busy": False, "landed_sha": SHA}})
        t.status(h)
        self.store.publish({})   # dropped after 24 h
        self.assertEqual(t.status(h).landed_sha, SHA)

    def test_bad_state_answers_are_refused(self):
        t, h = self.launched()
        for sessions in ({h.worker_id: {"state": "weird"}}, {h.worker_id: {"state": "done", "landed_sha": "abc"}}):
            self.store.publish(sessions)
            with self.assertRaises(TransportError):
                t.status(h)
        self.store.state, self.store.etag = {"v": 2}, 99
        with self.assertRaises(TransportError):
            self.transport().status(h)

    def test_heartbeat_age_says_whether_floor_is_reachable(self):
        t, h = self.launched()
        self.clock.t = 1_800_000_000.0
        self.store.publish({}, written_at="2027-01-15T08:00:00Z")   # 1_800_000_000 is 2027-01-15T08:00:00Z
        t.status(h)
        self.assertTrue(t.floor_alive())
        self.clock.t += 91
        self.assertFalse(t.floor_alive())

    def test_last_message_reads_the_blob_and_a_missing_blob_is_empty(self):
        t, h = self.launched()
        self.assertEqual(t.last_message(h), "")
        self.store.blobs["msg/%s.json" % h.worker_id] = json.dumps({"v": 1, "text": "landed " + SHA}).encode()
        self.assertEqual(t.last_message(h), "landed " + SHA)

    def test_wait_for_end_sees_done(self):
        t, h = self.launched()
        self.store.publish({h.worker_id: {"state": "done", "busy": False, "landed_sha": SHA}})
        self.assertEqual(wait_for_end(t, h, 10, sleep=lambda s: None, clock=self.clock).landed_sha, SHA)


class StopAndSessionTests(Base):
    def test_stop_adds_one_retire_and_a_repeat_adds_none(self):
        t, h = self.launched()
        t.stop(h, "r")
        t.stop(h, "r")
        self.assertEqual([c["op"] for c in self.store.messages], ["launch", "retire"])

    def test_stop_of_an_ended_session_adds_nothing(self):
        t, h = self.launched()
        self.store.publish({h.worker_id: {"state": "done", "busy": False, "landed_sha": SHA}})
        t.status(h)
        t.stop(h, "r")
        self.assertEqual([c["op"] for c in self.store.messages], ["launch"])
        with self.assertRaises(TransportError):
            t.send(h, "late")

    def test_adopt_continues_the_numbering_of_an_earlier_process(self):
        _, h = self.launched()
        t2 = self.transport()
        with self.assertRaises(TransportError):
            t2.send(h, "x")   # not adopted: no number to use
        t2.adopt(h, 5)
        t2.send(h, "x")
        self.assertEqual(self.store.messages[-1]["seq"], 5)
        for bad in (1, 0, True, "2"):
            with self.assertRaises(TransportError):
                t2.adopt(h, bad)

    def test_foreign_handle_is_refused_without_a_call(self):
        t = self.transport()
        for bad in (WorkerHandle("w", "ses_0123456789abcdef", "fake"), WorkerHandle("w", "ses_../x", "floor")):
            with self.assertRaises(TransportError):
                t.status(bad)
        self.assertEqual(self.store.requests, [])


class SafetyTests(Base):
    def test_errors_are_classified_and_never_carry_a_token(self):
        cases = [(429, True), (503, True), (500, True), (403, False), (401, False), (400, False)]
        for status, retry in cases:
            with self.subTest(status=status):
                t = self.transport()
                self.store.fail = [status]
                with self.assertRaises(TransportError) as cm:
                    launch_remote_worker(t, **GOOD)
                self.assertEqual(cm.exception.retryable, retry)
                for s in SECRETS:
                    self.assertNotIn(s, str(cm.exception))

    def test_network_failure_is_retryable_and_never_carries_a_token(self):
        t = self.transport()
        self.store.fail = [ConnectionRefusedError(QSAS)]
        with self.assertRaises(TransportError) as cm:
            launch_remote_worker(t, **GOOD)
        self.assertTrue(cm.exception.retryable)
        self.assertNotIn(SECRETS[0], str(cm.exception))

    def test_no_token_in_any_repr_or_handle(self):
        t, h = self.launched()
        self.store.publish({})
        for obj in (t, h, t.status(h)):
            for s in SECRETS:
                self.assertNotIn(s, repr(obj))

    def test_tokens_go_only_to_the_two_fixed_hosts(self):
        t, h = self.launched()
        t.send(h, "x" * (50 * 1024))
        t.status(h)
        t.last_message(h)
        for _, url, _, _ in self.store.requests:
            host = urllib.parse.urlsplit(url).hostname
            self.assertIn(host, ("%s.queue.core.windows.net" % ACCOUNT, "%s.blob.core.windows.net" % ACCOUNT))
            self.assertTrue(url.startswith("https://"))

    def test_the_queue_token_never_goes_to_the_blob_host_and_the_container_token_never_to_the_queue(self):
        t, h = self.launched()
        t.send(h, "y" * (50 * 1024))
        t.status(h)
        for _, url, _, _ in self.store.requests:
            if ".queue." in url:
                self.assertNotIn(SECRETS[1], url)
            else:
                self.assertNotIn(SECRETS[0], url)

    def test_a_redirect_is_never_followed(self):
        self.assertTrue(floor_mod._NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example") is None)

    def test_close_forgets_the_tokens(self):
        t = self.transport()
        t.close()
        t.close()
        self.assertEqual((t._queue_sas, t._container_sas), ("", ""))

    def test_the_caller_never_sends_a_delete_or_a_queue_read(self):
        t, h = self.launched()
        t.send(h, "x")
        t.stop(h, "r")
        t.status(h)
        t.last_message(h)
        self.assertEqual({r[0] for r in self.store.requests} - {"POST", "PUT", "GET"}, set())
        for method, url, _, _ in self.store.requests:
            if method == "GET":
                self.assertIn(".blob.", url)


class ConfigurationTests(Base):
    def test_unsafe_settings_are_refused(self):
        bad = [dict(account="X"), dict(account="ab"), dict(account="a.b.c"), dict(floor_id="F1"), dict(caller_id="a/b"), dict(caller_id=""),
               dict(git_base="http://git.example"), dict(git_base="https://u:p@git.example")]
        for change in bad:
            with self.subTest(change=list(change)):
                with self.assertRaises(TransportError):
                    self.transport(**change)

    def test_sas_file_rules(self):
        d = self.dir.name
        ok = sas_file(d, "ok", "?" + QSAS)
        self.assertEqual(load_sas_file(ok), QSAS)
        for name, text, mode in (("open", QSAS, 0o644), ("group", QSAS, 0o660), ("empty", "", 0o600), ("space", "a b c d e f g h i j k", 0o600),
                                 ("big", "a" * 9000, 0o600), ("bin", "\xff\xfe", 0o600)):
            with self.subTest(name=name):
                with self.assertRaises(TransportError) as cm:
                    load_sas_file(sas_file(d, name, text, mode))
                self.assertNotIn(QSAS, str(cm.exception))
        with self.assertRaises(TransportError):
            load_sas_file(os.path.join(d, "missing"))
        os.symlink(ok, os.path.join(d, "link"))
        with self.assertRaises(TransportError):
            load_sas_file(os.path.join(d, "link"))
        with self.assertRaises(TransportError):
            load_sas_file(d)

    def test_names_are_configuration(self):
        t = self.transport(account="otherstore", floor_id="f2", caller_id="acme")
        launch_remote_worker(t, **GOOD)
        url = self.store.requests[0][1]
        self.assertIn("otherstore.queue.core.windows.net/cmd-f2-acme/messages", url)
        t.status(WorkerHandle("widget-1", self.store.messages[0]["session_id"], "floor"))
        self.assertIn("otherstore.blob.core.windows.net/floor-f2-acme/state.json", self.store.requests[-1][1])


if __name__ == "__main__":
    unittest.main()
