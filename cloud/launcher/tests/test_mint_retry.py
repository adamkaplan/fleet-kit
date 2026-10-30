#!/usr/bin/env python3
"""A launch's preflight retries a passing failure of its token mint and never retries a refusal (ask #1621).

Offline: the vault is a shell command, GitHub is a fake opener, the clock is a list. The old behaviour is the old get_token, kept below as text.
"""
import datetime as dt
import email.message
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.environ.get("MINT_RETRY_SRC") or os.path.dirname(HERE)   # a mutant's scratch copy, when a child is started below
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, LAUNCHER)
sys.path.insert(0, os.path.join(ROOT, "cloud", "worker", "tests"))
import app_token  # noqa: E402
import preflight  # noqa: E402
import stub_support  # noqa: E402

TARGET = "acme-org/widget-service"
TOKEN = "ghs_TOKENVALUE0123456789"
OLD_GET_TOKEN = '''
def old_get_token(target):
    try:
        token = app_token.mint(target)
        return token
    except SystemExit:
        raise Unreadable("no token could be minted")
'''


def answer(method):
    """The good answers of the fake GitHub."""
    if method == "GET":
        return {"id": 5, "account": {"login": "acme-org", "id": 9}}
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=55)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"token": TOKEN, "expires_at": exp, "permissions": dict(app_token.EXPECTED_PERMISSIONS),
            "repositories": [{"name": "widget-service", "full_name": TARGET, "owner": {"id": 9}}]}


class Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http(code, retry_after=None):
    m = email.message.Message()
    if retry_after:
        m["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://api.github.com/x", code, "x", m, io.BytesIO(b"{}"))


class Harness(unittest.TestCase):
    """One mint with a scripted failure: `fail` is raised by the first GitHub call (every call with always), `vault_miss` fails the first
    vault call, `edit(method, body)` changes the good answers."""

    def run_mint(self, fail=None, vault_miss=False, edit=None, always=False, get_token=None):
        tmp = tempfile.mkdtemp(prefix="mint-retry-")
        self.addCleanup(shutil.rmtree, tmp, True)
        flag = os.path.join(tmp, "missed")
        id_cmd = "echo 123"
        if vault_miss:
            id_cmd = "if [ -e %s ]; then echo 123; else : > %s; exit 1; fi" % (flag, flag)
        env = {"CW_APP_ID_CMD": id_cmd, "CW_APP_KEY_CMD": "echo KEYTEXT"}
        calls, waits, github = [], [], []

        def opener(req, timeout=30):
            github.append(req.get_method())
            if fail is not None and (always or len(github) == 1):
                raise fail
            body = answer(req.get_method())
            return Answer(edit(req.get_method(), body) if edit else body)

        real = app_token.mint

        def counting(target, role="worker"):
            calls.append(1)
            return real(target, role)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(app_token, "open_url", opener), \
                mock.patch.object(app_token, "sign", lambda k, d: b"sig"), mock.patch.object(app_token, "mint", counting), \
                mock.patch.object(preflight, "_sleep", waits.append), redirect_stdout(out), redirect_stderr(err):
            os.environ.pop("CW_PREFLIGHT_TOKEN_CMD", None)
            token, refusal = None, None
            try:
                token = (get_token or preflight.get_token)(TARGET)
            except preflight.Unreadable as e:
                refusal = str(e)
        return {"token": token, "refusal": refusal, "calls": len(calls), "waits": waits, "out": out.getvalue(), "err": err.getvalue()}


class TestMintRetryOld(Harness):
    def test_old_code_refuses_at_the_first_miss(self):
        ns = {"app_token": app_token, "Unreadable": preflight.Unreadable}
        exec(OLD_GET_TOKEN, ns)
        r = self.run_mint(vault_miss=True, get_token=ns["old_get_token"])
        self.assertEqual((r["calls"], r["refusal"]), (1, "no token could be minted"))
        print("S2: mint calls %d, refusal text %s" % (r["calls"], r["refusal"]))


class TestMintRetryTransient(Harness):
    def test_each_transient_kind_recovers_on_the_second_try(self):
        kinds = [("vault miss", dict(vault_miss=True)), ("network error", dict(fail=urllib.error.URLError("down"))),
                 ("HTTP 503", dict(fail=http(503))), ("HTTP 429 with Retry-After", dict(fail=http(429, "3")))]
        ok = lines = leaked = 0
        for name, kw in kinds:
            r = self.run_mint(**kw)
            self.assertEqual((r["token"], r["calls"]), (TOKEN, 2), name)
            n = [l for l in r["err"].splitlines() if l.startswith("mint try 1 of 4 failed: ")]
            self.assertEqual(len(n), 1, name)
            self.assertIn("trying again in", n[0])
            ok += 1
            lines += len(n)
            leaked += (TOKEN in r["err"] + r["out"]) + ("KEYTEXT" in r["err"] + r["out"])
        self.assertEqual((ok, lines, leaked), (4, 4, 0))
        print("S3: failure kinds recovered on the second try %d of 4, retry lines %d, token values printed %d" % (ok, lines, leaked))

    def test_a_403_with_retry_after_is_transient_and_a_long_wait_ends_the_retries(self):
        r = self.run_mint(fail=http(403, "3"))
        self.assertEqual((r["token"], r["calls"]), (TOKEN, 2))
        r = self.run_mint(fail=http(429, "60"), always=True)
        self.assertEqual((r["calls"], r["waits"]), (1, []))
        self.assertIn("not retried", r["refusal"])
        self.assertIn("after 1 try:", r["refusal"])

    def test_always_failing_makes_four_tries_with_growing_waits(self):
        r = self.run_mint(fail=http(503), always=True)
        self.assertEqual((r["calls"], r["waits"]), (4, [2, 5, 10]))
        self.assertTrue(r["refusal"].startswith("no token could be minted after 4 tries: "), r["refusal"])
        self.assertEqual(len([l for l in r["err"].splitlines() if l.startswith("mint try ")]), 3)
        print("S5: tries %d, waits %s, refusals 1" % (r["calls"], " ".join(map(str, r["waits"]))))


def other_repo(method, body):
    if method == "POST":
        body["repositories"][0].update(name="other", full_name="acme-org/other")
    return body


def suspended(method, body):
    if method == "GET":
        body["suspended_at"] = "2026-10-01T00:00:00Z"
    return body


def post_422(method, body):
    if method == "POST":
        raise http(422)
    return body


class TestMintRetryRefusal(Harness):
    def test_each_refusal_ends_after_one_try(self):
        cases = [("not installed", dict(fail=http(404), always=True)), ("installation does not cover", dict(edit=post_422)),
                 ("token covers another repository", dict(edit=other_repo)), ("suspended", dict(edit=suspended)),
                 ("403 without Retry-After", dict(fail=http(403), always=True))]
        once = lines = 0
        for name, kw in cases:
            r = self.run_mint(**kw)
            self.assertEqual(r["calls"], 1, name)
            self.assertTrue(r["refusal"].startswith("no token could be minted"), name)
            self.assertEqual(r["waits"], [], name)
            once += 1
            lines += len([l for l in r["err"].splitlines() if l.startswith("mint try ")])
        self.assertEqual((once, lines), (5, 0))
        print("S4: refusals after exactly one try %d of 4, retry lines %d" % (once - 1, lines))


class TestMintRetryAudit(Harness):
    def test_refusal_names_the_tries_and_one_audit_line_is_written(self):
        r = self.run_mint(fail=http(503), always=True)
        spec = importlib.util.spec_from_file_location("cw_switchboard_lib_t", os.path.join(ROOT, "bin", "cloud_worker.py"))
        lib = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lib)
        tmp = tempfile.mkdtemp(prefix="mint-audit-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "audit.jsonl")
        text = "refused: the repository preflight for %s did not pass: preflight: cannot read: %s; nothing was created" % (TARGET, r["refusal"])
        with mock.patch.dict(os.environ, {"CW_CLOUD_AUDIT": path}):
            lib._audit_refused_preflight("w1", lib._lib().CloudError(text, 2))
        lines = open(path).read().splitlines()
        rec = json.loads(lines[0])
        self.assertEqual((len(lines), rec["action"], rec["worker"], rec["ok"]), (1, "create.failed", "w1", False))
        self.assertTrue(rec["reason"].startswith("preflight: no token could be minted after 4 tries: "), rec["reason"])
        for k in ("ts", "runtime_id", "cost_estimate_usd", "actor", "reason"):
            self.assertIn(k, rec)
        leaked = sum(TOKEN in x or "KEYTEXT" in x for x in (r["refusal"], lines[0], r["err"]))
        self.assertEqual(leaked, 0)
        print("S6: refusal line names 4 tries, audit lines %d, token values %d" % (len(lines), leaked))


MUTANTS = (("retries a refusal", 'if not fail.get("transient"):', "if False:", "TestMintRetryRefusal"),
           ("never retries", 'if not fail.get("transient"):', "if True:", "TestMintRetryTransient"))


class TestMintRetryMutants(unittest.TestCase):
    def test_each_mutant_fails_its_scenario(self):
        failing = applied = 0
        for name, old, new, cls in MUTANTS:
            scratch = tempfile.mkdtemp(prefix="mint-mutant-")
            self.addCleanup(shutil.rmtree, scratch, True)
            for f in ("app_token.py", "preflight.py", "token_mint.py"):
                shutil.copy(os.path.join(os.path.dirname(HERE), f), scratch)
            src = open(os.path.join(scratch, "preflight.py")).read()
            self.assertEqual(src.count(old), 1, name)
            open(os.path.join(scratch, "preflight.py"), "w").write(src.replace(old, new))
            applied += 1
            env = dict(os.environ, MINT_RETRY_SRC=scratch)
            p = stub_support.start(self, [sys.executable, "-m", "unittest", "test_mint_retry." + cls], cwd=HERE, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            p.communicate(timeout=120)
            self.assertEqual(p.returncode, 1, name)
            failing += 1
        self.assertEqual((failing, applied), (2, 2))
        print("S7: %d of 2 mutants fail, %d of 2 mutations applied" % (failing, applied))


class TestMintRetryDocs(unittest.TestCase):
    def test_both_sentences_are_in_both_documents(self):
        found = 0
        for rel in ("cloud/launcher/README.md", "docs/cloud-worker-interface.md"):
            text = " ".join(open(os.path.join(ROOT, rel)).read().split())
            a = "A passing failure of the preflight's mint" in text and "is retried inside the preflight: 4 tries, with waits of 2, 5 and 10 seconds" in text \
                and "is never retried" in text
            b = "A launch that still cannot mint says its tries and the last reason" in text and "writes one audit line" in text
            found += a + b
        self.assertEqual(found, 4)
        print("S8: %d of 2 sentences found" % (found // 2))


if __name__ == "__main__":
    unittest.main()
