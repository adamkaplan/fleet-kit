"""#2305: cw-worker-activity counts the time of the newest message of any session, a message still being written included. Own loopback server."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "activity.py")
NOW_MS = 1_800_000_000_000
MIN = 60_000


def serve(sessions, messages):
    """A server of /api/session (sessions), /api/session/<id>/message (messages[id]) and an empty /api/session/active; (server, port)."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/api/session/active":
                body = {"data": {}}
            elif self.path == "/api/session":
                body = {"data": sessions}
            elif self.path.startswith("/api/session/") and self.path.endswith("/message") and self.path.split("/")[3] in messages:
                body = {"data": messages[self.path.split("/")[3]]}
            else:
                self.send_response(404)
                self.end_headers()
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def iso(ms):
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ActivityMessage(unittest.TestCase):
    def read(self, sessions, messages, script=SCRIPT):
        server, port = serve(sessions, messages)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pw = os.path.join(tmp.name, "pw")
        with open(pw, "w") as f:
            f.write("x\n")
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        env = dict(os.environ, CW_ACTIVITY_PORT=str(port), CW_ACTIVITY_PASSWORD_FILE=pw, CW_ACTIVITY_UNDER_TEST="1", CW_ACTIVITY_PROC=empty.name)
        p = subprocess.run([sys.executable, script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)

    def scene(self):
        sessions = [{"id": "ses_a", "time": {"created": NOW_MS - 90 * MIN, "updated": NOW_MS - 40 * MIN}}]
        messages = {"ses_a": [{"id": "m1", "type": "user", "time": {"created": NOW_MS - 90 * MIN}},
                              {"id": "m2", "type": "assistant", "time": {"created": NOW_MS - 20 * MIN, "streamed": NOW_MS - 19 * MIN}}]}   # still being written: no `completed`
        return sessions, messages

    def test_a_message_in_progress_moves_the_last_activity(self):
        got = self.read(*self.scene())
        self.assertEqual(got["last_activity"], iso(NOW_MS - 20 * MIN))   # the message's time exactly, not the sessions' 40 minutes
        self.assertNotEqual(got["last_activity"], iso(NOW_MS - 40 * MIN))

    def test_without_messages_the_sessions_time_still_counts(self):
        sessions, _ = self.scene()
        self.assertEqual(self.read(sessions, {"ses_a": []})["last_activity"], iso(NOW_MS - 40 * MIN))

    def test_an_unreadable_message_list_falls_back_to_the_sessions_time(self):
        sessions, _ = self.scene()
        self.assertEqual(self.read(sessions, {})["last_activity"], iso(NOW_MS - 40 * MIN))   # the stub answers 404 for it

    def test_the_newest_of_several_sessions_and_messages_wins_and_a_bad_id_is_not_asked(self):
        sessions = [{"id": "ses_a", "time": {"created": 1, "updated": NOW_MS - 50 * MIN}}, {"id": "../x", "time": {"created": 1, "updated": NOW_MS - 45 * MIN}},
                    {"id": "ses_b", "time": {"created": 1, "updated": NOW_MS - 60 * MIN}}]
        messages = {"ses_a": [{"time": {"created": NOW_MS - 30 * MIN}}, {"time": {"created": NOW_MS - 55 * MIN}}], "ses_b": [{"time": {"created": NOW_MS - 10 * MIN}}, "junk", {"time": "x"}]}
        self.assertEqual(self.read(sessions, messages)["last_activity"], iso(NOW_MS - 10 * MIN))

    def test_idle_seconds_are_1200_not_2400_for_the_scene(self):
        got = self.read(*self.scene())
        import datetime as dt
        last = dt.datetime.strptime(got["last_activity"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(int(NOW_MS / 1000 - last), 1200)

    def test_control_and_mutant_the_read_without_the_messages_reports_the_sessions_time(self):
        with open(SCRIPT) as f:
            src = f.read()
        old = ' + newest_messages(port, auth, sessions)\n'
        self.assertEqual(src.count(old), 1, "anchored text")
        mutant = src.replace(old, "\n")
        self.assertNotEqual(mutant, src)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "activity.py")
            with open(path, "w") as f:
                f.write(mutant)
            got = self.read(*self.scene(), script=path)
        self.assertEqual(got["last_activity"], iso(NOW_MS - 40 * MIN), "the mutant must read the sessions' time only")


if __name__ == "__main__":
    unittest.main()
