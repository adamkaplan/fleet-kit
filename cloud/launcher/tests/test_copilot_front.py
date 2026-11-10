"""Ask #2341 (S2 to S5 and S8): the policy front cloud/copilot-bridge/bridge-front.mjs, started with `node` against a fake bridge written
in Python's standard library. Offline: the fake listens on the loopback only. The mutant is a scratch copy of the front."""
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FRONT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "cloud", "copilot-bridge", "bridge-front.mjs")
NODE = shutil.which("node")
BANNED = "model banned from the council: opus"
ANSWER = "ANSWER-TEXT-4417"
BODY_TEXT = "BODY-TEXT-9921 please review"
TOKEN = "ghu_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"


class Fake:
    """The fake bridge: records every request it receives and answers a fixed non-streaming completion."""

    def __init__(self):
        self.seen = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(self.rfile.read(n))})
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": ANSWER}}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Running:
    """One front process; its standard output is the log."""

    def __init__(self, script, fake, **env):
        self.port = free_port()
        e = {"PATH": os.environ.get("PATH", ""), "BRIDGE_FRONT_PORT": str(self.port), "BRIDGE_FRONT_HOST": "127.0.0.1", "BRIDGE_UPSTREAM_PORT": str(fake.port)}
        e.update({k: str(v) for k, v in env.items()})
        self.proc = subprocess.Popen([NODE, script], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines = [self.proc.stdout.readline()]   # the "listening" line
        assert "listening" in self.lines[0], self.lines[0]

    def request(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=headers or {})
        r = c.getresponse()
        text = r.read().decode()
        c.close()
        return r.status, text

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        rest, _ = self.proc.communicate(timeout=20)   # waits for the process and closes its pipe
        self.lines += rest.splitlines(True)
        return "".join(self.lines)


def chat(model, effort=None, text="hello"):
    b = {"model": model, "messages": [{"role": "user", "content": text}], "tools": [], "stream": False}
    if effort:
        b["reasoning"] = {"effort": effort}
    return b


@unittest.skipUnless(NODE, "node is not installed")
class Base(unittest.TestCase):
    def setUp(self):
        self.fake = Fake()
        self.addCleanup(self.fake.close)
        self.tmp = tempfile.mkdtemp(prefix="front-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_front(self, script=FRONT, **env):
        r = Running(script, self.fake, **env)
        self.addCleanup(r.stop)
        return r


class ModelPolicy(Base):
    def test_s2_models(self):
        f = self.run_front()
        for model, effort in (("anthropic/claude-sonnet-5.5", "high"), ("gpt-4o", None), ("claude-haiku-5.5", "low")):
            status, text = f.request("POST", "/chat/completions", chat(model, effort), {"Authorization": "Bearer " + TOKEN, "content-type": "application/json"})
            self.assertEqual(status, 200, text)
            self.assertIn(ANSWER, text)
        got = [(s["body"]["model"], s["body"]["reasoning"]["effort"], s["body"]["reasoning_effort"]) for s in self.fake.seen]
        self.assertEqual(got, [("claude-sonnet-5.5", "medium", "medium"), ("claude-sonnet-5.5", "medium", "medium"), ("claude-haiku-5.5", "low", "low")])
        for banned in ("anthropic/claude-opus-5.5", "Claude-OPUS-5", "OPUS"):
            status, text = f.request("POST", "/v1/chat/completions", chat(banned, "low"))
            self.assertEqual(status, 403)
            self.assertEqual(json.loads(text)["error"], BANNED)
        self.assertEqual(len(self.fake.seen), 3)
        self.assertFalse([s for s in self.fake.seen if "opus" in json.dumps(s["body"]).lower().replace("hello", "")])
        for s in self.fake.seen:   # the body's other fields reach the bridge; no incoming header does
            self.assertEqual(s["path"], "/chat/completions")
            self.assertNotIn("authorization", s["headers"])
            self.assertEqual(s["body"]["messages"], [{"role": "user", "content": "hello"}])
            self.assertEqual(s["body"]["stream"], False)

    def test_haiku_without_effort_and_odd_bodies(self):
        f = self.run_front()
        self.assertEqual(f.request("POST", "/chat/completions", chat("claude-haiku-5.5"))[0], 200)
        self.assertNotIn("reasoning", self.fake.seen[0]["body"])
        for raw in (b"not json", b"[]", json.dumps({"model": 5, "messages": []}).encode(), json.dumps({"model": "x"}).encode()):
            self.assertEqual(f.request("POST", "/chat/completions", raw=raw)[0], 400)
        self.assertEqual(len(self.fake.seen), 1)


class Paths(Base):
    def test_s3_refused_paths_and_cap(self):
        f = self.run_front(BRIDGE_MAX_BODY=1000)
        self.assertEqual(f.request("GET", "/models")[0], 404)
        self.assertEqual(f.request("POST", "/embeddings", chat("gpt-4o"))[0], 404)
        self.assertEqual(f.request("GET", "/chat/completions")[0], 404)
        self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o", text="x" * 2000))[0], 413)
        self.assertEqual(self.fake.seen, [])
        self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o"))[0], 200)
        status, text = f.request("GET", "/usage")
        self.assertEqual(status, 200)
        usage = json.loads(text)
        day = usage["day"]
        self.assertEqual(usage["days"][day]["requests"], 1)
        self.assertEqual(usage["days"][day]["models"], {"claude-sonnet-5.5": 1})
        for s in (ANSWER, "hello", "messages", "xxxx"):
            self.assertNotIn(s, text)

    def test_default_cap_is_400000(self):
        f = self.run_front()
        self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o", text="x" * 399000))[0], 200)
        self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o", text="x" * 401000))[0], 413)
        self.assertEqual(len(self.fake.seen), 1)


class Hardening(Base):
    def test_only_the_allowed_fields_reach_the_bridge(self):
        f = self.run_front()
        b = chat("gpt-4o")
        b.update({"models": ["claude-opus-5"], "fallback": "opus", "extra_body": {"model": "opus"}, "route": "x", "max_tokens": 10 ** 9})
        self.assertEqual(f.request("POST", "/chat/completions", raw=json.dumps(b).replace('{"model"', '{"__proto__": {"model": "opus"}, "model"', 1).encode())[0], 200)
        sent = self.fake.seen[0]["body"]
        self.assertEqual(sorted(sent), ["max_tokens", "messages", "model", "reasoning", "reasoning_effort", "stream", "tools"])
        self.assertEqual(sent["max_tokens"], 64000)

    def test_a_body_nested_too_deep_is_refused_and_the_front_lives(self):
        f = self.run_front()
        deep = ('{"model": "gpt-4o", "messages": [], "x": ' + "[" * 150000 + "]" * 150000 + "}").encode()
        self.assertEqual(f.request("POST", "/chat/completions", raw=deep)[0], 200)   # the unknown field is dropped before it is re-serialised
        deep = ('{"model": "gpt-4o", "messages": ' + "[" * 150000 + "]" * 150000 + "}").encode()
        self.assertEqual(f.request("POST", "/chat/completions", raw=deep)[0], 400)
        self.assertIsNone(f.proc.poll())
        self.assertEqual(f.request("GET", "/usage")[0], 200)
        used = json.loads(f.request("GET", "/usage")[1])
        self.assertEqual(used["days"][used["day"]]["requests"], 1)   # the refused one was not counted


class Budget(Base):
    def test_s4_daily_budget(self):
        clock = os.path.join(self.tmp, "day")
        with open(clock, "w") as fh:
            fh.write("2026-11-10")
        f = self.run_front(BRIDGE_DAILY_BUDGET=3, BRIDGE_CLOCK_FILE=clock)
        codes = [f.request("POST", "/chat/completions", chat("gpt-4o"))[0] for _ in range(3)]
        status, text = f.request("POST", "/chat/completions", chat("gpt-4o"))
        self.assertEqual(codes, [200, 200, 200])
        self.assertEqual(status, 429)
        self.assertIn("budget", json.loads(text)["error"])
        self.assertEqual(len(self.fake.seen), 3)
        with open(clock, "w") as fh:
            fh.write("2026-11-11")
        self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o"))[0], 200)
        self.assertEqual(len(self.fake.seen), 4)
        days = json.loads(f.request("GET", "/usage")[1])["days"]
        self.assertEqual({d: v["requests"] for d, v in days.items()}, {"2026-11-10": 3, "2026-11-11": 1})


def log_problems(script, test):
    """Drive requests that carry a body text and a token-shaped string; the problems found in the front's log (empty = clean)."""
    fake = Fake()
    try:
        f = Running(script, fake)
        try:
            f.request("POST", "/chat/completions", chat("gpt-4o", text=BODY_TEXT + " " + TOKEN), {"Authorization": "Bearer " + TOKEN})
            f.request("POST", "/chat/completions", chat("claude-opus-5", text=BODY_TEXT), {"Authorization": "Bearer " + TOKEN})
            f.request("GET", "/models", headers={"Authorization": "Bearer " + TOKEN})
        finally:
            log = f.stop()
    finally:
        fake.close()
    counts = [l for l in log.splitlines() if '"status"' in l]
    problems = []
    if len(counts) != 3:
        problems.append("%d count lines for 3 requests" % len(counts))
    if TOKEN in log or "A1b2C3d4E5f6" in log:
        problems.append("the token was logged")
    if BODY_TEXT in log or "BODY-TEXT" in log:
        problems.append("a body was logged")
    if ANSWER in log:
        problems.append("an answer was logged")
    return problems


def check_log(script, test):
    problems = log_problems(script, test)
    if problems:
        test.fail("; ".join(problems))


@unittest.skipUnless(NODE, "node is not installed")
class Log(unittest.TestCase):
    OLD = "    logLine(entry);\n"
    NEW = '    logLine({ ...entry, auth: req.headers.authorization || "" });\n'

    def test_s5_log_has_counts_and_no_body_answer_or_token(self):
        check_log(FRONT, self)

    def test_s8_mutant_control_first_then_the_mutant_fails_with_the_anchored_text(self):
        with open(FRONT, encoding="utf-8") as fh:
            src = fh.read()
        check_log(FRONT, self)   # the control: the unmutated front passes
        self.assertEqual(src.count(self.OLD), 1, "the text to mutate must occur once")
        mutated = src.replace(self.OLD, self.NEW)
        self.assertTrue(mutated != src and self.OLD not in mutated, "the mutation was not applied")   # a no-op mutant fails here
        scratch = tempfile.mkdtemp(prefix="front-mutant-")
        self.addCleanup(shutil.rmtree, scratch, True)
        path = os.path.join(scratch, "bridge-front.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(mutated)
        with self.assertRaisesRegex(AssertionError, r"\Athe token was logged"):
            check_log(path, self)


if __name__ == "__main__":
    unittest.main()
