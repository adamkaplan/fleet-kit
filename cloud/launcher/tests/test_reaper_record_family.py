"""The families behind #829 (the end hook's result reaching the run record), derived from the code and driven through the real stop:

  * every stop path x pod phase x hook outcome (the table is read from `Launcher.stop` and the fake cluster, not listed twice);
  * every writer of a run record file (read from run_record.py with `ast`), the second write failing, being skipped, and a crash between;
  * the operator's stop, pinned to a fixture captured from main (output, stderr, run.json, audit line);
  * the shared `scrub` over credential shapes, and every place that stores or prints a push error.

Offline: the fake kubectl. One mutant per cell is named in the PR body, with the test that kills it.
"""
import ast
import json
import os
import re
import subprocess
import sys
import unittest
import io
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw
run_record = cw.run_record

LAUNCHER = os.path.dirname(HERE)


def _tree(name):
    with open(os.path.join(LAUNCHER, name)) as f:
        return ast.parse(f.read())


def _function(tree, qualname):
    cls, _, fn = qualname.rpartition(".")
    scope = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls][0] if cls else tree
    return [n for n in ast.walk(scope) if isinstance(n, ast.FunctionDef) and n.name == fn][0]


def phases_the_stop_treats_as_ended():
    """The pod phases `Launcher.stop` compares with `in (...)`: read from the code."""
    out = set()
    for n in ast.walk(_function(_tree("cloud_worker.py"), "Launcher.stop")):
        if isinstance(n, ast.Compare) and isinstance(n.ops[0], ast.In) and isinstance(n.comparators[0], ast.Tuple):
            out |= {e.value for e in n.comparators[0].elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    return out


def record_file_writers():
    """Every function in run_record.py that calls `write_private`, and the file it writes (the literal name argument)."""
    out = {}
    tree = _tree("run_record.py")
    for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
            for c in ast.walk(fn):
                if isinstance(c, ast.Call) and getattr(c.func, "id", "") == "write_private":
                    out.setdefault("%s.%s" % (cls.name, fn.name), set()).add(c.args[1].value)
    return out


def callers_of(attr):
    """The methods of cloud_worker.py that call `<anything>.<attr>(...)`."""
    out = set()

    def own_nodes(fn):   # the function's own body: a nested def is its own caller
        stack = list(fn.body)
        while stack:
            n = stack.pop()
            yield n
            if not isinstance(n, ast.FunctionDef):
                stack.extend(ast.iter_child_nodes(n))
    for fn in [n for n in ast.walk(_tree("cloud_worker.py")) if isinstance(n, ast.FunctionDef)]:
        if any(isinstance(c, ast.Call) and getattr(c.func, "attr", "") == attr for c in own_nodes(fn)):
            out.add(fn.name)
    return out


# The table of writers: who writes which record file, and what each does when it fails. A writer added to run_record.py
# that is missing here fails test_the_writer_table_is_complete.
WRITERS = {
    "Recorder.record": {"run.json"},
    "Recorder.add_hook": {"run.json"},
    "Recorder._write_transcript": {"transcript.jsonl", "tally.json"},
}

PHASES = ["Running", "Pending", "Failed", "Succeeded"]
HOOKS = {"ok": {"pushed": True, "wip_branch": "coder7/wip"}, "nothing": {"pushed": False, "wip_branch": None},
         "error": {"pushed": False, "wip_branch": None, "error": "push refused"}}


class Quiet(base.Base):
    def setUp(self):
        super().setUp()
        self.name = cw.pod_name("coder7")

    def make_pod(self, phase, kept, hook):
        self.patch_state(hook_result=HOOKS[hook])
        self.create("coder7")
        st = self.st()
        pod = st["pods"][self.name]
        pod["status"]["phase"] = phase
        if kept:
            pod["metadata"]["annotations"]["cw/kept"] = "true"
        self.write_state(st)

    def run_json(self):
        runs = os.path.join(os.path.dirname(self.env["CW_CLOUD_AUDIT"]), "runs")
        if not os.path.isdir(runs):
            return None
        names = os.listdir(runs)
        self.assertEqual(len(names), 1)
        with open(os.path.join(runs, names[0], "run.json")) as f:
            return json.load(f)


class StopPathTable(Quiet):
    def test_the_phase_table_is_complete(self):
        ended = phases_the_stop_treats_as_ended()
        self.assertEqual(ended, {"Failed", "Succeeded"})   # a phase the code adds must be added to PHASES with its expectations
        self.assertLessEqual(ended, set(PHASES))

    def test_every_stop_path_by_pod_phase_kept_mark_and_hook_outcome(self):
        ended = phases_the_stop_treats_as_ended()
        for phase in PHASES:
            for kept in (False, True):
                for hook in HOOKS:
                    with self.subTest(phase=phase, kept=kept, hook=hook):
                        self.setUp()
                        self.make_pod(phase, kept, hook)
                        code, out, err = self.run_cli("stop", "coder7", "--reason", "operator")
                        rec = self.run_json()
                        self.assertIsNotNone(rec, "every stop of a pod that exists makes a record")
                        if phase in ended and not kept:   # nothing runs in the pod: the hook is not run, and the record says so
                            self.assertEqual(code, 0, err)
                            self.assertEqual(rec["end_hook"], {"ran": False, "pushed": False, "wip_branch": None, "skipped": [],
                                                              "note": "pod was %s; end hook not run" % phase})
                            self.assertIsNone(self.pod())
                            self.assertEqual(json.loads(out)["pushed"], False)
                        elif hook == "error":
                            self.assertEqual(code, 1, err)
                            self.assertEqual((rec["end_hook"]["ran"], rec["end_hook"]["pushed"], rec["end_hook"]["error"]), (True, False, "push refused"))
                            self.assertEqual(self.pod()["metadata"]["annotations"]["cw/kept"], "true")
                        else:
                            self.assertEqual(code, 0, err)
                            want = HOOKS[hook]
                            self.assertEqual((rec["end_hook"]["ran"], rec["end_hook"]["pushed"], rec["end_hook"]["wip_branch"]),
                                             (True, want["pushed"], want["wip_branch"]))
                            self.assertEqual(json.loads(out)["wip_branch"], want["wip_branch"])
                            self.assertIsNone(self.pod())

    def test_a_worker_with_no_pod_has_no_record_and_stops_cleanly(self):
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual((code, json.loads(out)["pushed"]), (0, False))
        self.assertIsNone(self.run_json())

    def test_the_reaper_stop_has_the_same_cells(self):
        for phase in PHASES:
            for hook in ("ok", "error"):
                with self.subTest(phase=phase, hook=hook):
                    self.setUp()
                    self.make_pod(phase, False, hook)
                    self.set_activity("coder7", True, base.T0)
                    self.clock.advance(hours=3)
                    code, out, err = self.run_cli("reap")
                    row = json.loads(out)[0]
                    rec = self.run_json()
                    failed = hook == "error" and phase not in phases_the_stop_treats_as_ended()
                    self.assertEqual(rec["end_hook"]["pushed"], row["pushed"])
                    self.assertEqual(bool(rec["end_hook"].get("error")), failed)
                    self.assertEqual((code, row["exit"]), (1, 1) if failed else (0, 0))


class RecordWriters(Quiet):
    def test_the_writer_table_is_complete(self):
        self.assertEqual({k: v for k, v in record_file_writers().items()}, WRITERS)

    def test_run_record_is_written_only_by_the_launchers_stop_path(self):
        # every caller of the recorder, read from the code: the stop's `go` and its hook helper, and nothing else
        self.assertEqual(callers_of("record") & {"go", "stop", "_hook_record", "reap"}, {"go"})
        self.assertEqual(callers_of("add_hook"), {"_hook_record"})
        self.assertEqual(callers_of("_hook_record"), {"go"})

    def _replace_that_fails_for(self, target, nth):
        real, seen = os.replace, []

        def replace(src, dst, *a, **k):
            if os.path.basename(str(dst)) == target:
                seen.append(dst)
                if len(seen) == nth:
                    raise OSError("disk full")
            return real(src, dst, *a, **k)
        run_record.os.replace = replace
        self.addCleanup(setattr, run_record.os, "replace", real)

    def test_the_first_write_failing_leaves_no_run_json_and_the_stop_completes(self):
        self.make_pod("Running", False, "ok")
        self._replace_that_fails_for("run.json", 1)
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.pod())
        runs = os.path.join(os.path.dirname(self.env["CW_CLOUD_AUDIT"]), "runs")
        d = os.path.join(runs, os.listdir(runs)[0])
        self.assertFalse(os.path.exists(os.path.join(d, "run.json")))   # a torn file is never the record
        self.assertIn("run record not updated with the end hook result", err)

    def test_the_second_write_failing_keeps_the_first_record_whole_and_the_stop_completes(self):
        self.make_pod("Running", False, "ok")
        self._replace_that_fails_for("run.json", 2)
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual((code, json.loads(out)["pushed"]), (0, True), err)
        self.assertIsNone(self.pod())
        rec = self.run_json()   # valid JSON, the first write
        self.assertNotIn("end_hook", rec)
        self.assertEqual(rec["worker"], "coder7")
        self.assertIn("run record not updated with the end hook result", err)

    def test_the_second_write_being_skipped_keeps_the_first_record_and_the_stop_completes(self):
        self.make_pod("Running", False, "ok")
        real = run_record.Recorder.__dict__["add_hook"]
        run_record.Recorder.add_hook = staticmethod(lambda d, h: None)
        self.addCleanup(setattr, run_record.Recorder, "add_hook", real)
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual((code, json.loads(out)["pushed"]), (0, True), err)
        self.assertNotIn("end_hook", self.run_json())
        self.assertIsNone(self.pod())

    def test_a_crash_between_the_two_writes_leaves_the_first_record_byte_for_byte(self):
        d = os.path.join(self.tmp.name, "rec")
        os.mkdir(d)
        first = json.dumps({"worker": "coder7", "end": "x"}, sort_keys=True) + "\n"
        run_record.write_private(d, "run.json", first)
        real = os.replace
        run_record.os.replace = lambda *a, **k: (_ for _ in ()).throw(SystemExit("killed"))
        self.addCleanup(setattr, run_record.os, "replace", real)
        with self.assertRaises(SystemExit):   # a crash is not an Exception: it passes through add_hook
            run_record.Recorder.add_hook(d, {"ran": True})
        with open(os.path.join(d, "run.json")) as f:
            self.assertEqual(f.read(), first)   # the rewrite goes through a .part, never in place
        self.assertTrue(os.path.exists(os.path.join(d, "run.json.part")))
        self.assertNotIn("run.json.part", run_record.RECORD_FILES)

    def test_the_hook_view_never_raises_even_for_an_answer_that_is_not_a_dict(self):
        L = cw.Launcher(cw.Kube(self.env), self.env, self.clock)
        for bad in (None, [], "x", 5):
            v = L._hook_view(bad)
            self.assertEqual((v["pushed"], v["wip_branch"], v["skipped"]), (False, None, []))

    def test_the_second_write_is_atomic_not_in_place(self):
        d = os.path.join(self.tmp.name, "rec2")
        os.mkdir(d)
        run_record.write_private(d, "run.json", json.dumps({"worker": "w"}) + "\n")
        seen = []
        real = os.replace
        run_record.os.replace = lambda src, dst, *a, **k: (seen.append((os.path.basename(src), os.path.basename(dst))), real(src, dst, *a, **k))[1]
        self.addCleanup(setattr, run_record.os, "replace", real)
        run_record.Recorder.add_hook(d, {"ran": True})
        self.assertEqual(seen, [("run.json.part", "run.json")])


class OperatorStopIsPinned(Quiet):
    """U1: an operator's stop, run on this branch, reproduces the output, stderr, record and audit line captured from main,
    except that run.json gains `end_hook` (and nothing else)."""

    def test_the_operator_stop_matches_the_fixture_captured_from_main(self):
        with open(os.path.join(HERE, "fixtures", "operator_stop_main.json")) as f:
            fx = json.load(f)
        work, _ = base.make_repo(self.tmp.name, "r", dirty=True)
        self.patch_state(hook_env={"CW_WORKDIR": work, "CW_WORKER": "coder7"})
        self.create("coder7")
        code, out, err = self.run_cli("stop", "coder7", "--reason", "operator")
        runs = os.path.join(os.path.dirname(self.env["CW_CLOUD_AUDIT"]), "runs")
        d = os.path.join(runs, os.listdir(runs)[0])
        rid = os.path.basename(d)
        with open(os.path.join(d, "run.json")) as f:
            raw = f.read()
        audit = self.audit_lines()[-1]

        def norm(s):
            s = s.replace(d, "<RECORD>").replace(rid, "<RUN_ID>")
            return re.sub(r'"end": "[^"]*"', '"end": "<END>"', s)
        self.assertEqual(code, fx["code"])
        self.assertEqual(norm(out), fx["stdout"])
        self.assertEqual(norm(err), fx["stderr"])
        self.assertEqual(sorted(os.listdir(d)), fx["files"])
        rec = json.loads(norm(raw))
        hook = rec.pop("end_hook")
        self.assertEqual(json.dumps(rec, sort_keys=True) + "\n", fx["run_json"])   # byte for byte, once end_hook is set aside
        self.assertEqual((hook["ran"], hook["pushed"], hook["wip_branch"], hook["skipped"]), (True, True, "coder7/wip", [".env"]))
        want = json.loads(fx["audit"])
        audit["actor"] = want["actor"] = "<ACTOR>"
        audit["reason"] = norm(audit["reason"])
        self.assertEqual(audit, want)


SECRETS = ["hunter2hunter2", "p4ss:w0rd+with!odd", "p4ss/w0rd?with#odd", "x" * 64, "tok_" + "ab12" * 8, "s3cr3t"]
ENCODINGS = [lambda s: s, lambda s: __import__("urllib.parse", fromlist=["quote"]).quote(s, safe=""),
             lambda s: __import__("urllib.parse", fromlist=["quote"]).quote(s, safe="") .replace("%", "%25")]
URL_SCHEMES = ["https", "http", "ssh", "git", "ftp", "git+https", "ws"]
AUTH_SCHEMES = ["Bearer", "Basic", "Token", "Digest", "Negotiate", "X-Custom-1", ""]


def credential_texts(secret):
    """Texts that carry `secret` in a credential shape: every (scheme, spacing, case, encoding) combination."""
    for enc in ENCODINGS:
        v = enc(secret)
        for sch in URL_SCHEMES:
            for user in ("user", "x-access-token", "", "u%40d"):
                yield "fatal: unable to access '%s://%s:%s@github.com/acme/widget.git/': 403" % (sch, user, v)
        for sch in AUTH_SCHEMES:
            for key in ("Authorization", "authorization", "AUTHORIZATION", "AuThOrIzAtIoN"):
                for sp in (" ", "  ", "\t", ""):
                    yield "request failed: %s%s:%s%s %s" % (key, sp, sp or " ", sch, v)
                yield '{"%s": "%s %s", "x": 1}' % (key, sch, v)
                yield "%s=%s %s" % (key, sch, v)


class ScrubShapes(unittest.TestCase):
    def test_no_credential_shape_survives_in_any_scheme_case_spacing_or_encoding(self):
        n = 0
        for secret in SECRETS:
            for text in credential_texts(secret):
                n += 1
                out = cw.scrub(text, ())
                for part in (secret, __import__("urllib.parse", fromlist=["quote"]).quote(secret, safe="")):
                    # a 6-character secret is checked as a whole; a longer one by its whole value
                    self.assertNotIn(part, out, text)
                self.assertIn("[redacted]", out, text)
        self.assertGreater(n, 1000)

    def test_a_multi_parameter_authorization_header_goes_whole(self):
        out = cw.scrub('Authorization: Digest username="u", response="abc123xyz", nonce="n1" tail', ())
        for part in ("abc123xyz", "n1", "username"):
            self.assertNotIn(part, out)

    def test_pod_controlled_text_cannot_make_the_scan_slow(self):
        import time
        for n in ("a.a." * 50000, "authorization" * 15000, " " * 199987 + "authorization", "http://" * 28000, "x" * 199999 + "@"):
            t = time.monotonic()
            cw.scrub(n, limit=10)
            self.assertLess(time.monotonic() - t, 5, n[:20])   # linear in practice (well under a second); quadratic would be minutes
        for pat, _ in cw.CREDENTIAL_SHAPES:   # structural: no unbounded scheme or userinfo quantifier in a URL shape
            if "://" in pat.pattern:
                self.assertNotRegex(pat.pattern, r"\[a-z0-9\+\.\-\]\*|\[\^[^\]]*\]\*")

    def test_text_around_a_credential_and_plain_urls_survive(self):
        self.assertEqual(cw.scrub("see https://github.com/acme/widget.git and git@github.com:acme/widget.git", ()),
                         "see https://github.com/acme/widget.git and git@github.com:acme/widget.git")
        self.assertTrue(cw.scrub("fatal: unable to access 'https://u:pw@github.com/a/b.git/': 403", ()).endswith("github.com/a/b.git/': 403"))
        self.assertTrue(cw.scrub("note: the authorization step is next", ()).startswith("note: the authorization step"))

    def test_the_limit_and_control_characters_still_apply(self):
        self.assertLessEqual(len(cw.scrub("https://u:" + "p" * 5000 + "@h/ " + "z " * 1000, ())), 300)
        self.assertNotIn("\n", cw.scrub("Authorization: Bearer a\nb", ()))

    def test_every_consumer_of_the_one_scrub_gets_it(self):
        # the launcher (Kube.scrub, the audit, stop, main), the run record (kube.scrub) and the driver (cw.scrub): one function
        sys.path.insert(0, LAUNCHER)
        import run_task
        self.assertIs(run_task.cw.scrub, cw.scrub)
        self.assertEqual(cw.Kube({"CW_KUBE_CONTEXT": "c"}).scrub("Authorization: Bearer abcdefghij"), "authorization: [redacted]")
        with open(os.path.join(LAUNCHER, "run_record.py")) as f:
            src = f.read()
        self.assertNotIn("def scrub", src)
        self.assertNotIn("re.sub(", src)


class PushErrorSinks(Quiet):
    """Every place that stores or prints a push error's text, driven through the real stop with a hook that echoes a URL
    carrying a password and an Authorization header: the hook answer's own error, the CloudError text, the stop's output and
    stderr, the audit reason, the run record (end_hook), the reaper's row and the transcript record."""
    LEAK = "hunter2-pa55word"
    ERR = "fatal: unable to access 'https://x-access-token:%s@github.com/acme/widget.git/': 403; Authorization: Bearer %s" % (LEAK, LEAK)

    def test_the_hook_error_reaches_no_sink_unscrubbed(self):
        self.patch_state(hook_result={"pushed": False, "wip_branch": None, "error": self.ERR})
        self.create("coder7")
        code, out, err = self.run_cli("stop", "coder7", "--reason", "operator")
        self.assertEqual(code, 1)
        self.assertIsNotNone(self.pod())
        rec = json.dumps(self.run_json())
        audit = json.dumps(self.audit_lines())
        for sink, text in (("stdout", out), ("stderr", err), ("record", rec), ("audit", audit)):
            self.assertNotIn(self.LEAK, text, sink)
        self.assertIn("[redacted]", rec)

    def test_the_hook_answer_view_and_run_hook_scrub_it(self):
        class Fake(cw.Kube):
            def exec(self, name, *cmd, keep_stdout=False):
                return json.dumps({"pushed": False, "error": PushErrorSinks.ERR, "note": PushErrorSinks.ERR, "wip_branch": "b/" + PushErrorSinks.ERR,
                                   "skipped": [PushErrorSinks.ERR]}) + "\n"
        L = cw.Launcher(Fake(self.env), self.env, self.clock)
        self.assertNotIn(self.LEAK, L.run_hook("p")["error"])
        view = L._hook_view(json.loads(Fake(self.env).exec("p")))
        self.assertNotIn(self.LEAK, json.dumps(view))

    def test_the_reaper_row_and_a_reaper_stop_record_scrub_it(self):
        self.patch_state(hook_result={"pushed": False, "wip_branch": None, "error": self.ERR})
        self.create("coder7", "--hard", "1h")
        self.set_activity("coder7", True, base.T0)
        self.clock.advance(hours=2)
        code, out, err = self.run_cli("reap")
        self.assertEqual(code, 1)
        for sink, text in (("row", out), ("stderr", err), ("record", json.dumps(self.run_json())), ("audit", json.dumps(self.audit_lines()))):
            self.assertNotIn(self.LEAK, text, sink)

    def test_the_transcript_record_scrubs_the_same_shapes(self):
        k = cw.Kube(self.env)
        rec = run_record.Recorder(k, dict(self.env))
        d = os.path.join(self.tmp.name, "t")
        os.mkdir(d)
        info = {}
        rec._write_transcript(d, [{"type": "assistant", "text": self.ERR}, {"parts": [{"text": self.ERR}]}], info)
        with open(os.path.join(d, "transcript.jsonl")) as f:
            text = f.read()
        self.assertNotIn(self.LEAK, text)
        for line in text.splitlines():
            json.loads(line)   # the JSON stays JSON


if __name__ == "__main__":
    unittest.main()
