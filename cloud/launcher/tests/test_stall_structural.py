"""Ask #2443: the stall watch reads the harness's own turn completion, not a text sentinel.

Cause: on 11 November 2026 the stall watch of ask #2399 called three finished pods stalled because it read a text line the agent writes (`done` at the end of the progress file) as
the proof that a pod had finished. The harness reports its own completion: GET /api/session/active (is the session running), the `outcome` of the last `idle` message and the `finish`
of the last `assistant` message. The fixtures hold the real answer shapes (cloud/launcher/tests/fixtures/server_shapes/turn.json). Offline: a loopback server in the test."""
import http.server
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
sys.path.insert(0, HERE)
import stall_watch as sw  # noqa: E402
from test_stall_watch import World, conditions  # noqa: E402

ACTIVITY = os.path.join(ROOT, "cloud", "worker", "activity.py")
with open(os.path.join(HERE, "fixtures", "server_shapes", "turn.json")) as _f:
    FIX = json.load(_f)
FINISHED = {"turn": "idle", "outcome": "succeeded", "finish": "stop"}


class Server:
    """A loopback headless server answering with the fixtures' shapes: running or not, the newest idle message, the newest assistant message."""
    def __init__(self, running=False, idle="idle_succeeded", assistant="assistant_stop", active=None, idle_body=None):
        outer = self
        self.paths = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                outer.paths.append(self.path)
                kind = urllib.parse.parse_qs(url.query).get("type", [None])[0]
                if url.path == "/api/session/active":
                    body = active if active is not None else (FIX["active_running"] if running else FIX["active_none"])
                elif url.path == "/api/session":
                    body = {"data": [FIX["session"]], "cursor": {"previous": None, "next": None}}
                elif url.path.endswith("/message"):
                    pick = {"idle": idle_body if idle_body is not None else (FIX[idle] if idle else None), "assistant": FIX[assistant] if assistant else None}
                    items = [pick[kind]] if kind in pick and pick[kind] else [m for m in pick.values() if m]
                    body = {"data": [] if kind in pick and not pick[kind] else items, "cursor": {"previous": None, "next": None}}
                else:
                    body = {}
                raw = json.dumps(body).encode() if not isinstance(body, bytes) else body
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def helper_turn(server, script=ACTIVITY):
    """The `turn` the activity helper prints against the server ({} when it printed nothing)."""
    d = tempfile.mkdtemp(prefix="turn-")
    pw = os.path.join(d, "pw")
    with open(pw, "w") as f:
        f.write("synthetic-password\n")
    proc = os.path.join(d, "proc")
    os.mkdir(proc)
    env = dict(os.environ, CW_ACTIVITY_PORT=str(server.port), CW_ACTIVITY_PASSWORD_FILE=pw, CW_ACTIVITY_UNDER_TEST="1", CW_ACTIVITY_PROC=proc)
    out = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout).get("turn", {}) if out.stdout.strip() else {}


def probe_turn(server):
    """The `turn` the stall watch's own probe reads from the same server (the probe runs in the pod; here against loopback)."""
    d = tempfile.mkdtemp(prefix="turn-probe-")
    pw = os.path.join(d, "pw")
    with open(pw, "w") as f:
        f.write("synthetic-password\n")
    env = dict(os.environ, CW_RUNS_DIR=d, CW_ACTIVITY_PORT=str(server.port), CW_ACTIVITY_PASSWORD_FILE=pw)
    out = subprocess.run([sys.executable, "-I", "-c", sw.PROBE_CODE], env=env, capture_output=True, text=True, timeout=30)
    return json.loads(out.stdout.strip().splitlines()[-1])["turn"]


def s2_wrong(script=ACTIVITY):
    """S2: the number of fixture answers the helper reports wrongly: finished that is not a succeeded-stop, a garbled answer reported as known."""
    wrong = 0
    expected = [(dict(running=True), {"turn": "running", "outcome": None, "finish": None}),
                (dict(), FINISHED),
                (dict(idle="idle_failed"), {"turn": "idle", "outcome": "failed", "finish": "stop"}),
                (dict(idle="idle_interrupted", assistant="assistant_tool_calls"), {"turn": "idle", "outcome": "interrupted", "finish": "tool-calls"})]
    for kw, want in expected:
        s = Server(**kw)
        try:
            wrong += helper_turn(s, script) != want
        finally:
            s.close()
    # an unreadable /api/session/active keeps the helper's old exit rule: nothing printed, exit 1 (never a known state)
    s = Server(active={"data": "x"})
    try:
        wrong += helper_turn(s, script) != {}
    finally:
        s.close()
    garbled = [dict(idle=None), dict(idle_body={"id": "m", "time": {"created": 1}, "type": "idle", "outcome": "finished"}), dict(idle_body={"type": "idle"}),
               dict(idle_body={"id": "m", "time": {"created": 1}, "type": "idle", "outcome": "succeeded"}),   # an assistant message newer than the idle one: a turn began after it
               dict(assistant="assistant_in_flight", idle_body={"id": "m", "time": {"created": 1}, "type": "idle", "outcome": "bogus"})]
    for kw in garbled:
        s = Server(**kw)
        try:
            wrong += helper_turn(s, script) != {"turn": "unknown", "outcome": None, "finish": None}
        finally:
            s.close()
    return wrong


def pod(world, turn, results_exists, **kw):
    world.add(**kw)
    inner = world.pods["w1-pod"]["probe"]
    world.pods["w1-pod"]["probe"] = lambda now: dict(inner(now), turn=turn, results_exists=results_exists)
    return world


def class_cases(module):
    """S3/S4: a list of (case, wanted, got) for the finished, failed, stranded, running, unknown and sentinel-on-a-running-pod cases (16 minutes without change)."""
    rows = []
    for case, turn, exists, tail, want in [
            ("finished", FINISHED, True, ("working",), []),
            ("failed", {"turn": "idle", "outcome": "failed", "finish": "error"}, True, ("done",), ["progress-stale"]),
            ("stranded", FINISHED, False, ("done",), ["progress-stale"]),
            ("running with the word done", {"turn": "running", "outcome": None, "finish": None}, True, ("done",), ["progress-stale"]),
            ("unknown", {"turn": "unknown", "outcome": None, "finish": None}, True, ("done",), ["progress-stale"])]:
        w = pod(World(), turn, exists, progress_age=16, tail=tail)
        rows.append((case, want, conditions(w.run(module))))
    return rows


def old_watch():
    """The watch as it was before this change: the line `finished = classify(...)` put back as the text-sentinel line it replaced."""
    with open(os.path.join(LAUNCHER, "stall_watch.py")) as f:
        text = f.read()
    new = '    finished = classify(data, active) == "finished"\n'
    old = '    last_p = tail[-1].strip().lower() if tail else ""\n    finished = last_p in ("done", "status: done") or str(data.get("results") or "").strip().lower() == "status: done"\n'
    assert text.count(new) == 1
    mod = types.ModuleType("stall_watch_old")
    exec(compile(text.replace(new, old), "old", "exec"), mod.__dict__)
    return mod


class Structural(unittest.TestCase):
    def test_s2_the_helper_reports_the_structural_turn_state_and_unknown_for_a_garbled_answer(self):
        self.assertEqual(s2_wrong(), 0)

    def test_s2_the_probe_reads_the_same_turn_as_the_helper(self):
        for kw in (dict(running=True), dict(), dict(idle="idle_failed"), dict(idle=None)):
            s = Server(**kw)
            try:
                self.assertEqual(probe_turn(s), helper_turn(s), kw)
            finally:
                s.close()

    def test_s2_the_helper_asks_for_the_idle_and_the_assistant_messages_by_type(self):
        s = Server()
        try:
            helper_turn(s)
            self.assertTrue(any("type=idle" in p and "order=desc" in p and "limit=1" in p for p in s.paths), s.paths)
            self.assertTrue(any("type=assistant" in p for p in s.paths), s.paths)
        finally:
            s.close()

    def test_s2_control_a_helper_without_the_turn_fails_the_check(self):
        with open(ACTIVITY) as f:
            text = f.read()
        old = '"turn": turn_state(port, auth, active, sessions)}))'
        self.assertEqual(text.count(old), 1)
        d = tempfile.mkdtemp(prefix="turn-ctl-")
        path = os.path.join(d, "activity.py")
        with open(path, "w") as f:
            f.write(text.replace(old, '}))').replace('"last_activity": last.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),', '"last_activity": last.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")'))
        self.assertGreater(s2_wrong(path), 0)

    def test_s3_each_pod_gets_its_class_and_the_stranded_one_is_stalled(self):
        wrong = [r for r in class_cases(sw) if r[1] != r[2]]
        self.assertEqual(wrong, [])

    def test_s3_the_classes_themselves(self):
        self.assertEqual(sw.classify({"turn": FINISHED, "results_exists": True}), "finished")
        self.assertEqual(sw.classify({"turn": FINISHED, "results_exists": False}), "stranded")
        self.assertEqual(sw.classify({"turn": FINISHED, "results_exists": True}, job_active=True), "unknown")
        for outcome, finish in (("failed", "stop"), ("succeeded", "error"), ("succeeded", "content-filter"), ("succeeded", "length")):
            self.assertEqual(sw.classify({"turn": {"turn": "idle", "outcome": outcome, "finish": finish}, "results_exists": True}), "failed")
        self.assertEqual(sw.classify({"turn": {"turn": "idle", "outcome": "succeeded", "finish": "tool-calls"}, "results_exists": True}), "unknown")
        self.assertEqual(sw.classify({"turn": {"turn": "running", "outcome": None, "finish": None}, "results_exists": True}), "running")

    def test_s3_a_pod_with_an_active_run_is_not_finished_and_the_text_decides_nothing(self):
        w = pod(World(), FINISHED, True, progress_age=16, runs=[("build", 2, "still going")])
        self.assertEqual(w.run(), [])   # an active run keeps progress-stale silent as before
        for line in ("done", "STATUS: done"):
            w = pod(World(), {"turn": "unknown", "outcome": None, "finish": None}, True, progress_age=16, tail=(line,))
            w.set_results("w1-pod", "STATUS: done")
            self.assertEqual(conditions(w.run()), ["progress-stale"], line)

    def test_s4_a_running_pod_whose_last_progress_line_is_done_is_not_finished(self):
        w = pod(World(), {"turn": "running", "outcome": None, "finish": None}, True, progress_age=16, tail=("done",))
        w.set_results("w1-pod", "STATUS: done")
        self.assertEqual(sum(1 for _, c in w.run() if c == "progress-stale"), 1)

    def test_s5_no_text_tells_an_agent_to_end_its_progress_file_with_the_word_done(self):
        files = [os.path.join(ROOT, "skills", "fleet-coordination", "operator-brief-header.txt")]
        for base, _dirs, names in os.walk(os.path.join(ROOT, "skills")):
            files += [os.path.join(base, n) for n in names if n.endswith((".md", ".txt"))]
        files += [os.path.join(ROOT, "bin", "cloud_worker.py"), os.path.join(LAUNCHER, "cloud_worker.py")]
        self.assertEqual(finish_line_count(files), 0)
        self.assertEqual(finish_line_count([], "Write progress to /work/progress.txt and end it with a last line exactly `done`."), 1, "the pattern sees the rule")

    def test_s6_controls_the_old_watch_fails_each_check(self):
        old = old_watch()
        rows = class_cases(old)
        failing = {r[0] for r in rows if r[1] != r[2]}
        self.assertEqual(failing, {"finished", "failed", "stranded", "running with the word done", "unknown"}, rows)

    def test_s6_control_the_old_watch_calls_the_text_sentinel_pod_finished(self):
        old = old_watch()
        for tail, results in ((("done",), ""), (("STATUS: done",), ""), (("x",), "STATUS: done")):
            w = World()
            w.add(progress_age=16, tail=tail)
            w.set_results("w1-pod", results)
            self.assertEqual(w.run(old), [], tail)   # silent = finished: a pod that is running or stranded is not told
            w = World()
            w.add(progress_age=16, tail=tail)
            w.set_results("w1-pod", results)
            self.assertEqual(conditions(w.run(sw)), ["progress-stale"], tail)   # the structural watch sees no finish in this fixture (no turn): unknown, alerted


class RecordAndNotice(unittest.TestCase):
    """The end notice's wording comes from the structural state the launcher recorded (run.json `turn`), not from the results file's text."""
    def recorder(self, answer):
        import importlib.util
        spec = importlib.util.spec_from_file_location("run_record_structural", os.path.join(LAUNCHER, "run_record.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class Kube:
            def exec(self_, pod, *cmd, **kw):
                self.assertEqual(cmd[0], "cw-worker-activity")
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return mod.Recorder(Kube(), {})

    def test_the_record_keeps_only_a_turn_state_of_the_schema_values(self):
        good = json.dumps({"busy": False, "last_activity": "2026-11-12T00:00:00Z", "turn": FINISHED})
        self.assertEqual(self.recorder(good).turn_state("p"), FINISHED)
        for bad in ("", "not json", json.dumps({"busy": False}), json.dumps({"turn": "idle"}), json.dumps({"turn": {"turn": "idle", "outcome": "done", "finish": "stop"}}),
                    json.dumps({"turn": dict(FINISHED, extra="x")}), OSError("down")):
            self.assertIsNone(self.recorder(bad).turn_state("p"), bad)

    def test_the_end_notice_wording_comes_from_the_structural_state_alone(self):
        sys.path.insert(0, os.path.join(ROOT, "bin"))
        import cloud_end_alert as cea
        d = tempfile.mkdtemp(prefix="notice-")
        def said(run):
            with open(os.path.join(d, "run.json"), "w") as f:
                json.dump(run, f)
            return cea.reported_done(d, json.loads)
        self.assertTrue(said({"turn": FINISHED}))
        self.assertFalse(said({"results": {"done": True, "text": "STATUS: done"}}), "the text line decides nothing")
        self.assertFalse(said({"turn": dict(FINISHED, outcome="failed"), "results": {"done": True}}))
        self.assertFalse(said({"turn": dict(FINISHED, turn="running")}))
        self.assertIn("ended idle after a succeeded turn", cea.message_text("w", "idle limit", "now", "nothing unpushed", "rec", done=True))
        self.assertNotIn("reporting done", cea.message_text("w", "idle limit", "now", "nothing unpushed", "rec", done=True))


def finish_line_count(files, text=None):
    """Lines that tell an agent to end its progress file with the word done."""
    rx = re.compile(r"progress[^\n]{0,120}(?:last line|final line|end (?:it |the file )?with|ends? with)[^\n]{0,40}`?\bdone\b`?", re.I)
    n = len(rx.findall(text)) if text is not None else 0
    for p in files:
        with open(p, errors="replace") as f:
            n += sum(1 for line in f if rx.search(line) and "results.txt" not in line)
    return n


if __name__ == "__main__":
    unittest.main()
