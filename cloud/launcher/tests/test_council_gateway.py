#!/usr/bin/env python3
"""Ask #2322, S2 to S5 and S8: the council gateway against a real local fake provider (real HTTP on 127.0.0.1, no network beyond it).

S2 the key is added on the provider side only; S3 a refused path, model and size reach the provider 0 times; S4 the daily budget;
S5 the log holds one count line per request and no body and no key; S8 a scratch copy that logs the key fails S5's check with `the key was logged`."""
import datetime
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
GATEWAY = os.path.join(ROOT, "cloud", "gateway", "council_gateway.py")
KEY = "provider-key-0123456789abcdef"
BODY_TEXT = "the secret question about the repository"
ANSWER_TEXT = "the secret answer of the provider"
MODELS = ["anthropic/claude-haiku-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-opus-5.5"]


def load(path):
    spec = importlib.util.spec_from_file_location("council_gateway_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeProvider:
    """Records every request it receives (path, headers, body) and answers a fixed OpenAI-style answer."""
    def __init__(self):
        self.seen = []
        self.mode = None   # None answers; ("redirect", url) answers 302; ("error", status) answers that status with a body that holds the key
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                outer.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if outer.mode and outer.mode[0] == "redirect":
                    self.send_response(302)
                    self.send_header("Location", outer.mode[1])
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                if outer.mode and outer.mode[0] == "error":
                    out = json.dumps({"error": {"message": "bad key " + KEY}}).encode()
                    self.send_response(outer.mode[1])
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                out = json.dumps({"choices": [{"message": {"content": ANSWER_TEXT}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def council_body(model=MODELS[0], text=BODY_TEXT, **extra):
    body = {"model": model, "temperature": 0, "max_tokens": 24000, "reasoning": {"effort": "low"},
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": text}]}
    body.update(extra)
    return body


class Harness:
    def __init__(self, test, gateway_path=GATEWAY, budget=500, max_chars=400000, today=None):
        self.provider = FakeProvider()
        self.tmp = tempfile.mkdtemp(prefix="council-gw-")
        key_file = os.path.join(self.tmp, "key")
        with open(key_file, "w") as f:
            f.write(KEY + "\n")
        settings = {"models": MODELS, "max_request_chars": max_chars, "max_answer_tokens": 24000, "daily_budget": budget,
                    "provider_url": self.provider.url, "key_file": key_file}
        self.mod = load(gateway_path)
        self.lines = []
        self.today = today or datetime.date(2026, 11, 10)
        self.server = self.mod.make_server(settings, host="127.0.0.1", port=0, log=self.lines.append, clock=lambda: self.today)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.provider.close()
        shutil.rmtree(self.tmp, True)

    def post(self, body, path="/chat/completions", headers=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        h = {"content-type": "application/json", "Authorization": "Bearer host-gateway"}
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, data=data, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as res:
                return res.status, res.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode()


def check_log(test, gateway_path):
    """S5's check, run against any copy of the gateway: 1 line per request, no body text, no answer text, no key."""
    h = Harness(test, gateway_path)
    for _ in range(2):
        status, _ = h.post(council_body())
        test.assertEqual(status, 200)
    h.post(council_body(model="other/model"))
    test.assertEqual(len(h.lines), 3, "one count line per request")
    log = "\n".join(h.lines)
    test.assertNotIn(KEY, log, "the key was logged")
    test.assertNotIn(BODY_TEXT, log, "the request body was logged")
    test.assertNotIn(ANSWER_TEXT, log, "the answer body was logged")


class GatewayTests(unittest.TestCase):
    def test_s2_key_added_on_the_provider_side_only(self):
        h = Harness(self)
        body = council_body()
        status, answer = h.post(body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(answer)["choices"][0]["message"]["content"], ANSWER_TEXT)
        self.assertEqual(len(h.provider.seen), 1)
        seen = h.provider.seen[0]
        self.assertEqual(seen["headers"]["authorization"], "Bearer " + KEY)
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(json.loads(seen["body"]), body)   # the body is passed through unchanged
        self.assertNotIn(KEY, answer)   # the answer to the caller holds no key
        self.assertNotIn("x-api-key", seen["headers"])

    def test_s2_control_without_the_caller_header(self):
        h = Harness(self)
        req = urllib.request.Request(h.url + "/chat/completions", data=json.dumps(council_body()).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as res:
            self.assertEqual(res.status, 200)
        self.assertEqual(h.provider.seen[0]["headers"]["authorization"], "Bearer " + KEY)

    def test_s3_refusals_reach_the_provider_0_times(self):
        h = Harness(self, max_chars=1000)
        refused = [
            h.post(council_body(), path="/v1/models")[0],
            h.post(council_body(), path="/chat/completions?x=1")[0],
            h.post(council_body(model="openai/gpt-x"))[0],
            h.post(council_body(text="x" * 2000))[0],
        ]
        self.assertEqual(refused, [403, 403, 403, 413])
        self.assertEqual(h.post(None, raw=b"not json")[0], 400)
        self.assertEqual(h.post(council_body(max_tokens=24001))[0], 400)
        self.assertEqual(h.post(council_body(stream=True))[0], 400)
        req = urllib.request.Request(h.url + "/chat/completions", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=20)
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(len(h.provider.seen), 0)
        self.assertEqual(h.post(council_body(text="ok"))[0], 200)   # control: an allowed request is forwarded
        self.assertEqual(len(h.provider.seen), 1)

    def test_s3_all_three_council_models_are_served(self):
        h = Harness(self)
        for m in MODELS:
            self.assertEqual(h.post(council_body(model=m))[0], 200)
        self.assertEqual([json.loads(s["body"])["model"] for s in h.provider.seen], MODELS)

    def test_s4_daily_budget_and_next_day(self):
        h = Harness(self, budget=3)
        statuses = [h.post(council_body())[0] for _ in range(4)]
        self.assertEqual(statuses, [200, 200, 200, 429])
        self.assertEqual(len(h.provider.seen), 3)
        status, text = h.post(council_body())
        self.assertEqual(status, 429)
        self.assertIn("daily budget of 3 requests is spent", json.loads(text)["error"]["message"])
        h.today += datetime.timedelta(days=1)
        self.assertEqual(h.post(council_body())[0], 200)
        self.assertEqual(len(h.provider.seen), 4)

    def test_s4_refused_requests_do_not_use_the_budget(self):
        h = Harness(self, budget=1)
        h.post(council_body(model="other/model"))
        h.post(council_body(), path="/x")
        self.assertEqual(h.post(council_body())[0], 200)

    def test_s3_duplicate_keys_nan_and_size_fields_are_refused(self):
        h = Harness(self)
        dup = b'{"model": "anthropic/claude-haiku-5.5", "max_tokens": 10, "max_tokens": 999999, "messages": []}'
        self.assertEqual(h.post(None, raw=dup)[0], 400)
        self.assertEqual(h.post(None, raw=b'{"model": "anthropic/claude-haiku-5.5", "max_tokens": NaN}')[0], 400)
        self.assertEqual(h.post(None, raw=b"[" * 100000)[0], 400)
        self.assertEqual(h.post(council_body(max_completion_tokens=999999))[0], 400)
        self.assertEqual(h.post(council_body(n=5))[0], 400)
        for extra in ({"models": ["x"]}, {"plugins": []}, {"provider": {}}, {"n": True}, {"tool_choice": "auto"}, {"reasoning": {"max_tokens": 9}}, {"reasoning": "high"}):
            self.assertEqual(h.post(council_body(**extra))[0], 400, extra)
        self.assertEqual(len(h.provider.seen), 0)

    def test_s3_a_trickling_request_is_cut_off_and_holds_no_slot(self):
        import socket
        h = Harness(self)
        h.mod.READ_SECONDS = 1
        sock = socket.create_connection(("127.0.0.1", h.server.server_address[1]), timeout=20)
        sock.sendall(b"POST /chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n{")
        self.assertIn(b" 408 ", sock.recv(4096).split(b"\r\n")[0])
        sock.close()
        self.assertEqual(h.post(council_body())[0], 200)
        self.assertEqual(len(h.provider.seen), 1)

    def test_s3_a_length_far_over_the_cap_is_refused_before_the_body_is_read(self):
        h = Harness(self, max_chars=100)
        self.assertEqual(h.post(None, raw=b"x" * 1000)[0], 413)
        self.assertEqual(len(h.provider.seen), 0)

    def test_s2_a_redirect_is_not_followed_and_the_key_goes_nowhere_else(self):
        h = Harness(self)
        other = FakeProvider()
        self.addCleanup(other.close)
        h.provider.mode = ("redirect", other.url.replace("/v1", "") + "/elsewhere")
        self.assertEqual(h.post(council_body())[0], 502)
        self.assertEqual(len(other.seen), 0)

    def test_s5_provider_error_text_is_not_passed_on(self):
        h = Harness(self)
        h.provider.mode = ("error", 429)
        status, text = h.post(council_body())
        self.assertEqual(status, 429)
        self.assertNotIn(KEY, text)
        h.provider.mode = ("error", 401)
        status, text = h.post(council_body())
        self.assertEqual(status, 502)
        self.assertNotIn(KEY, text)

    def test_a_missing_key_file_answers_502_not_a_dropped_connection(self):
        h = Harness(self)
        os.remove(os.path.join(h.tmp, "key"))
        self.assertEqual(h.post(council_body())[0], 502)

    def test_s5_log_has_one_line_per_request_and_no_body_or_key(self):
        check_log(self, GATEWAY)

    def test_s5_budget_refusal_is_logged_once(self):
        h = Harness(self, budget=1)
        h.post(council_body())
        h.post(council_body())
        self.assertEqual(len(h.lines), 2)
        self.assertIn("status=429", h.lines[1])

    def test_s8_mutant_that_logs_the_key_fails_with_the_anchored_text(self):
        # control first: the unmutated gateway passes S5's check
        check_log(self, GATEWAY)
        with open(GATEWAY, encoding="utf-8") as f:
            text = f.read()
        anchor = "            gw.log(line)\n"
        self.assertEqual(text.count(anchor), 1)   # the text asserted once
        mutated = text.replace(anchor, "            gw.log(line + ' auth=' + gw.key())\n")
        self.assertNotEqual(mutated, text)       # applied, computed: a no-op fails
        tmp = tempfile.mkdtemp(prefix="council-gw-mutant-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "council_gateway.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(mutated)
        with self.assertRaises(AssertionError) as ctx:
            check_log(self, path)
        self.assertIn("the key was logged", str(ctx.exception))


class SettingsTests(unittest.TestCase):
    def test_settings_validation_names_the_key_never_the_value(self):
        mod = load(GATEWAY)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        good = {"models": MODELS, "max_request_chars": 400000, "max_answer_tokens": 24000, "daily_budget": 500,
                "provider_url": "https://provider.example/v1", "key_file": "/etc/council/key"}

        def write(d):
            p = os.path.join(tmp, "s.json")
            with open(p, "w") as f:
                json.dump(d, f)
            return p
        self.assertEqual(mod.load_settings(write(good))["daily_budget"], 500)
        for key, bad in (("provider_url", "http://provider.example/v1"), ("models", []), ("daily_budget", 0), ("provider_url", "ftp://x"), ("key_file", "relative"), ("max_request_chars", "9")):
            with self.assertRaises(mod.SettingsError):
                mod.load_settings(write(dict(good, **{key: bad})))
        missing = dict(good)
        del missing["key_file"]
        with self.assertRaises(mod.SettingsError):
            mod.load_settings(write(missing))


if __name__ == "__main__":
    unittest.main()
