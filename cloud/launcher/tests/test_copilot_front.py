"""Ask #2341 (S2 to S5 and S8): the policy front cloud/copilot-bridge/bridge-front.mjs, started with `node` against a fake bridge written
in Python's standard library. Offline: the fake listens on the loopback only. The mutant is a scratch copy of the front."""
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "worker", "tests"))
import stub_support  # noqa: E402
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
        self.usage = None   # the copilot_usage block of every answer; None: no block
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(self.rfile.read(n))})
                ans = {"choices": [{"message": {"role": "assistant", "content": ANSWER}}]}
                if outer.usage is not None:
                    ans["copilot_usage"] = outer.usage
                out = json.dumps(ans).encode()
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

    def __init__(self, test, script, fake, **env):
        self.port = free_port()
        e = {"PATH": os.environ.get("PATH", ""), "BRIDGE_FRONT_PORT": str(self.port), "BRIDGE_FRONT_HOST": "127.0.0.1", "BRIDGE_UPSTREAM_PORT": str(fake.port)}
        e.update({k: str(v) for k, v in env.items()})
        self.proc = stub_support.start(test, [NODE, script], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines = [self.proc.stdout.readline()]   # the "listening" line
        assert "listening" in self.lines[0], self.lines[0]
        self.reader = threading.Thread(target=self._read, daemon=True)   # the log is collected as it is written
        self.reader.start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line)

    def wait_for_counts(self, n, seconds=5.0):
        """The front writes a request's count line after it has sent the answer: wait (50 ms steps) until n count lines are in the log."""
        for _ in range(int(seconds / 0.05)):
            if sum('"status"' in l for l in self.lines) >= n:
                return
            time.sleep(0.05)

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
        self.proc.wait(timeout=20)
        self.reader.join(timeout=20)   # the reader ends at the end of the pipe
        self.proc.stdout.close()
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
        r = Running(self, script, self.fake, **env)
        self.addCleanup(r.stop)
        return r


class ModelPolicy(Base):
    def test_s2_models(self):
        f = self.run_front()
        for model, effort in (("anthropic/claude-sonnet-5.5", "high"), ("gpt-4o", None), ("claude-haiku-5.5", "low")):
            status, text = f.request("POST", "/chat/completions", chat(model, effort), {"Authorization": "Bearer " + TOKEN, "content-type": "application/json"})
            self.assertEqual(status, 200, text)
            self.assertIn(ANSWER, text)
        got = [(s["body"]["model"], s["body"].get("reasoning", {}).get("effort"), s["body"].get("reasoning_effort")) for s in self.fake.seen]
        self.assertEqual(got, [("claude-haiku-5.5", "high", "high"), ("claude-haiku-5.5", None, None), ("claude-haiku-5.5", "low", "low")])
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
        self.assertEqual(list(usage["days"][day]["models"]), ["claude-haiku-5.5"])
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
        self.assertEqual(sorted(sent), ["max_tokens", "messages", "model", "stream", "tools"])
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


def clock_front(test, **env):
    clock = os.path.join(test.tmp, "day")
    with open(clock, "w") as fh:
        fh.write("2026-11-10")
    return test.run_front(BRIDGE_CLOCK_FILE=clock, **env), clock


def post(f):
    """One request; returns after its count line is written, which is after its spend is added."""
    before = sum('"status"' in l for l in f.lines)
    out = f.request("POST", "/chat/completions", chat("gpt-4o"))
    f.wait_for_counts(before + 1)
    return out


class Budget(Base):
    def test_spend_adds_up_and_there_is_no_daily_cap(self):
        self.fake.usage = {"total_nano_aiu": 7400000}
        f, clock = clock_front(self)
        codes = [post(f)[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 200])
        self.assertEqual(len(self.fake.seen), 4)
        usage = json.loads(f.request("GET", "/usage")[1])
        self.assertNotIn("budget", usage)
        day = usage["days"]["2026-11-10"]
        self.assertNotIn("budget", day)
        self.assertEqual((day["credits"], day["requests"], day["refused"]), (0.0296, 4, 0))
        self.assertEqual(day["models"], {"claude-haiku-5.5": {"requests": 4, "credits": 0.0296}})
        with open(clock, "w") as fh:
            fh.write("2026-11-11")
        self.assertEqual(post(f)[0], 200)
        days = json.loads(f.request("GET", "/usage")[1])["days"]
        self.assertEqual({d: v["requests"] for d, v in days.items()}, {"2026-11-10": 4, "2026-11-11": 1})
        self.assertEqual(days["2026-11-11"]["credits"], 0.0074)

    def test_spend_is_computed_from_token_details_when_the_total_is_absent(self):
        self.fake.usage = {"token_details": [
            {"token_type": "input", "token_count": 17, "batch_size": 1, "cost_per_batch": 100000},
            {"token_type": "output", "token_count": 4, "batch_size": 2, "cost_per_batch": 1000000}]}
        f, _ = clock_front(self)
        self.assertEqual(post(f)[0], 200)
        f.wait_for_counts(1)
        day = json.loads(f.request("GET", "/usage")[1])["days"]["2026-11-10"]
        self.assertEqual(day["credits"], 0.0037)   # (1.7 + 2.0) million nano
        self.assertNotIn("unpriced", "".join(f.lines))

    def test_fallback_and_one_unpriced_line_when_both_are_absent(self):
        f, _ = clock_front(self, BRIDGE_FALLBACK_CREDITS="2")
        self.assertEqual(post(f)[0], 200)
        f.wait_for_counts(1)
        self.assertEqual(json.loads(f.request("GET", "/usage")[1])["days"]["2026-11-10"]["credits"], 2)
        self.assertEqual(sum('"unpriced"' in l for l in f.lines), 1)


def count_budget_problems(script, test):
    """Two answers of 0.0074 credits: the usage must add up to 0.0148."""
    fake = Fake()
    fake.usage = {"total_nano_aiu": 7400000}
    try:
        f = Running(test, script, fake)
        try:
            for _ in range(2):
                post(f)
            f.wait_for_counts(2)
            usage = json.loads(f.request("GET", "/usage")[1])
        finally:
            f.stop()
    finally:
        fake.close()
    day = usage["days"][usage["day"]]
    if day["credits"] != 0.0148:
        test.fail("the budget did not count credits: %s" % day)


@unittest.skipUnless(NODE, "node is not installed")
class BudgetMutant(unittest.TestCase):
    OLD = " row.nano += nano;"
    NEW = " row.nano += 0;"

    def test_control_first_then_a_request_counting_mutant_fails_with_the_anchored_text(self):
        with open(FRONT, encoding="utf-8") as fh:
            src = fh.read()
        count_budget_problems(FRONT, self)   # the control
        self.assertEqual(src.count(self.OLD), 1)
        scratch = tempfile.mkdtemp(prefix="front-mutant-")
        self.addCleanup(shutil.rmtree, scratch, True)
        path = os.path.join(scratch, "bridge-front.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(src.replace(self.OLD, self.NEW))
        with self.assertRaisesRegex(AssertionError, r"\Athe budget did not count credits"):
            count_budget_problems(path, self)


def log_problems(script, test):
    """Drive requests that carry a body text and a token-shaped string; the problems found in the front's log (empty = clean)."""
    fake = Fake()
    try:
        f = Running(test, script, fake)
        try:
            f.request("POST", "/chat/completions", chat("gpt-4o", text=BODY_TEXT + " " + TOKEN), {"Authorization": "Bearer " + TOKEN})
            f.request("POST", "/chat/completions", chat("claude-opus-5", text=BODY_TEXT), {"Authorization": "Bearer " + TOKEN})
            f.request("GET", "/models", headers={"Authorization": "Bearer " + TOKEN})
            f.wait_for_counts(3)
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
