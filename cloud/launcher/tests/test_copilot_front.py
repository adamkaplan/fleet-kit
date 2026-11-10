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
        self.usages = []   # one copilot_usage block (or None) per answer, in order; empty: no block
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": json.loads(self.rfile.read(n))})
                answer = {"choices": [{"message": {"role": "assistant", "content": ANSWER}}]}
                u = outer.usages.pop(0) if outer.usages else None
                if u is not None:
                    answer["copilot_usage"] = u
                out = json.dumps(answer).encode()
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
        self.assertEqual(usage["days"][day]["models"], {"claude-sonnet-5.5": {"credits": "0.0100", "requests": 1, "refused": 0}})   # unpriced: the fallback constant
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


def total(nano):
    return {"total_nano_aiu": nano}


DETAILS = {"token_details": [
    {"token_type": "input", "token_count": 17, "batch_size": 1000000, "cost_per_batch": 200000000000},
    {"token_type": "output", "token_count": 4, "batch_size": 1000000, "cost_per_batch": 1000000000000}]}


def check_spend(script, test):
    """S2: three answers of 7400000, 370000 and 7400000 nano-AIU add up to 0.01517 credits."""
    fake = Fake()
    try:
        fake.usages[:] = [total(7400000), total(370000), total(7400000)]
        f = Running(script, fake)
        try:
            for m in ("gpt-4o", "claude-haiku-5.5", "gpt-4o"):
                test.assertEqual(f.request("POST", "/chat/completions", chat(m))[0], 200)
            used = json.loads(f.request("GET", "/usage")[1])
        finally:
            f.stop()
    finally:
        fake.close()
    row = used["days"][used["day"]]
    test.assertEqual(row["credits"], "0.0152", "requests were counted, not credits" if row["credits"] == "3.0000" else row)
    test.assertEqual({m: v["credits"] for m, v in row["models"].items()}, {"claude-sonnet-5.5": "0.0148", "claude-haiku-5.5": "0.0004"})


class Budget(Base):
    def test_s4_daily_credit_budget(self):
        clock = os.path.join(self.tmp, "day")
        with open(clock, "w") as fh:
            fh.write("2026-11-10")
        f = self.run_front(BRIDGE_DAILY_CREDITS="0.01", BRIDGE_CLOCK_FILE=clock)
        self.fake.usages[:] = [total(7400000)] * 4
        codes = [f.request("POST", "/chat/completions", chat("gpt-4o"))[0] for _ in range(2)]   # the second crosses the budget
        status, text = f.request("POST", "/chat/completions", chat("gpt-4o"))
        self.assertEqual(codes, [200, 200])
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(text)["error"], "daily AI credit budget spent: 0.0148 of 0.01 credits")
        self.assertEqual(len(self.fake.seen), 2)
        with open(clock, "w") as fh:
            fh.write("2026-11-11")
        self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o"))[0], 200)
        self.assertEqual(len(self.fake.seen), 3)
        days = json.loads(f.request("GET", "/usage")[1])["days"]
        self.assertEqual({d: (v["requests"], v["refused"]) for d, v in days.items()}, {"2026-11-10": (2, 1), "2026-11-11": (1, 0)})

    def test_s2_s5_spend_adds_up_and_usage_fields(self):
        self.fake.usages[:] = [total(7400000), total(370000), total(7400000)]
        f = self.run_front(BRIDGE_DAILY_CREDITS="5")
        for m in ("gpt-4o", "claude-haiku-5.5", "gpt-4o"):
            self.assertEqual(f.request("POST", "/chat/completions", chat(m))[0], 200)
        status, text = f.request("GET", "/usage")
        used = json.loads(text)
        self.assertEqual(used["budget"], 5)
        row = used["days"][used["day"]]
        self.assertEqual((row["credits"], row["requests"], row["refused"]), ("0.0152", 3, 0))
        self.assertEqual(row["models"], {"claude-sonnet-5.5": {"credits": "0.0148", "requests": 2, "refused": 0}, "claude-haiku-5.5": {"credits": "0.0004", "requests": 1, "refused": 0}})
        for t in (ANSWER, "hello", "messages"):
            self.assertNotIn(t, text)

    def test_s3_token_details_and_the_unpriced_fallback(self):
        self.fake.usages[:] = [dict(DETAILS), {}, None]
        f = self.run_front(BRIDGE_UNPRICED_NANO="5000000")
        for _ in range(3):
            self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o"))[0], 200)
        used = json.loads(f.request("GET", "/usage")[1])
        row = used["days"][used["day"]]["models"]["claude-sonnet-5.5"]
        self.assertEqual(row["credits"], "0.0174")   # 7400000 + 2 * 5000000 nano
        log = f.stop()
        self.assertEqual(len([l for l in log.splitlines() if "unpriced" in l]), 2)   # one line per unpriced answer
        self.assertEqual(len([l for l in log.splitlines() if '"nano"' in l]), 3)

    def test_s3_details_alone_give_7400000(self):
        self.fake.usages[:] = [dict(DETAILS)]
        f = self.run_front(BRIDGE_UNPRICED_NANO="5000000")
        self.assertEqual(f.request("POST", "/chat/completions", chat("gpt-4o"))[0], 200)
        log = f.stop()
        self.assertEqual([json.loads(l)["nano"] for l in log.splitlines() if '"nano"' in l], [7400000])
        self.assertNotIn("unpriced", log)

    def test_s7_mutant_control_first_then_the_mutant_fails_with_the_anchored_text(self):
        with open(FRONT, encoding="utf-8") as fh:
            src = fh.read()
        check_spend(FRONT, self)   # the control: the unmutated front passes
        old = "        mrow.nano += nano;\n"
        new = "        mrow.nano += NANO;\n"   # one credit per request: requests are counted
        self.assertEqual(src.count(old), 1, "the text to mutate must occur once")
        mutated = src.replace(old, new)
        self.assertTrue(mutated != src and old not in mutated, "the mutation was not applied")   # a no-op mutant fails here
        scratch = tempfile.mkdtemp(prefix="front-mutant-")
        self.addCleanup(shutil.rmtree, scratch, True)
        path = os.path.join(scratch, "bridge-front.mjs")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(mutated)
        with self.assertRaisesRegex(AssertionError, r"requests were counted, not credits"):
            check_spend(path, self)


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
