"""#1757: the launcher tells a worker, N minutes before its hard limit, to send its final message (once). Offline: a fake kube, a fake clock,
the real Launcher.reap. Scenarios S2 S3 S4 S5 S6 S7 U2 U5; the mutants are run from a scratch copy by the mutant tests."""
import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as real  # noqa: E402

T0 = dt.datetime(2026, 11, 1, 9, 0, 0, tzinfo=dt.timezone.utc)
MIN = dt.timedelta(minutes=1)
MODULE_ENV = "CW_TEST_FINAL_NOTE_MODULE"
WORDS = "Your time is almost up. Send your final message now: what you pushed, your counts and what is left. Then continue only if time remains."


from test_cloud_settings import GOOD  # noqa: E402  (a complete good settings set)
FILE = "".join("%s=%s\n" % kv for kv in GOOD.items()) + "CW_FINAL_NOTE_MINUTES=%s\n"


def target_module():
    path = os.environ.get(MODULE_ENV)
    if not path:
        return real
    spec = importlib.util.spec_from_file_location("cloud_worker_final_scratch", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeKube:
    def __init__(self, pods, fail_exec=False):
        self.pods, self.sent, self.fail_exec = pods, [], fail_exec

    def list_pods(self):
        return list(self.pods)

    def annotate(self, name, uid, **kv):
        for p in self.pods:
            if p["metadata"]["name"] == name:
                p["metadata"]["annotations"].update({"cw/%s" % k.replace("_", "-"): str(v) for k, v in kv.items()})
                self.order.append(("annotate", name))

    order = []

    def exec(self, name, *args):
        self.order.append(("exec", name))
        if self.fail_exec:
            raise RuntimeError("exec failed")
        self.sent.append((name, args[-1]))

    def scrub(self, text, limit=300):
        return str(text)[:limit]

    def add_secrets(self, *a):
        pass

    def close(self):
        pass


def pod(name="w1", worker="coder", left=10, life=60, phase="Running", hard=True, started=None, hard_text=None):
    """A pod whose hard deadline is `left` minutes after T0 and whose whole life is `life` minutes."""
    ann = {"cw/worker": worker, "cw/started": started or real.iso(T0 + MIN * (left - life))}
    if hard:
        ann["cw/hard-deadline"] = hard_text or real.iso(T0 + MIN * left)
    return {"metadata": {"name": name, "uid": "u-" + name, "annotations": ann}, "status": {"phase": phase}}


class Scene:
    def __init__(self, pods, tmp, env=None, fail_exec=False):
        self.tmp, self.pods = tmp, pods
        self.audit_path = os.path.join(tmp, "audit.jsonl")
        self.kube = FakeKube(pods, fail_exec)
        self.kube.order = []
        self.env = dict({"CW_CLOUD_AUDIT": self.audit_path, "XDG_STATE_HOME": tmp}, **(env or {}))
        self.mod = target_module()

    def launcher(self):
        mod = self.mod

        class L(mod.Launcher):
            def hard_deadline(self, p):
                return T0 + 600 * MIN

            def activity(self, name):
                return (True, T0)

            def cost(self, p):
                return 0.0

            def send_wip_notes(self, live, now):
                pass

            def renew_git_tokens(self, pods, now):
                return []

        return L(self.kube, self.env, lambda: T0, lambda s: None)

    def reap(self, launcher=None):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = (launcher or self.launcher()).reap()
        return out, err.getvalue()

    def lines(self):
        try:
            with open(self.audit_path) as f:
                rows = [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []
        return [r for r in rows if r.get("action") == "final-note"]


def run(pods, tmp, passes=1, **kw):
    s = Scene(pods, tmp, **kw)
    outs = [s.reap() for _ in range(passes)]
    return s, outs


class FinalNote(unittest.TestCase):
    def test_s2_one_note_at_ten_minutes_left_none_at_eleven(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, _ = run([pod(left=10)], tmp)
            s2, _ = run([pod(left=11)], tempfile.mkdtemp(dir=tmp))
        self.assertEqual(s.kube.sent, [("w1", WORDS)], "exactly one note with the fixed words at 10 minutes left")
        self.assertEqual(s2.kube.sent, [], "no note at 11 minutes left")
        self.assertIsNone(re.search(r"coder|w1|ghp_|gho_|/home|/tmp|/run|\bw1\b", real.FINAL_NOTE_TEXT), "no name, token or path in the words")
        self.assertEqual(real.FINAL_NOTE_MARK, "final_note")

    def test_s3_setting_moves_the_minute_and_the_validator_refuses_bad_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = run([pod(left=15)], tempfile.mkdtemp(dir=tmp), env={"CW_FINAL_NOTE_MINUTES": "15"})
            b, _ = run([pod(left=16)], tempfile.mkdtemp(dir=tmp), env={"CW_FINAL_NOTE_MINUTES": "15"})
            c, _ = run([pod(left=10)], tempfile.mkdtemp(dir=tmp))
            d, _ = run([pod(left=11)], tempfile.mkdtemp(dir=tmp))
            self.assertEqual((len(a.kube.sent), len(b.kube.sent), len(c.kube.sent), len(d.kube.sent)), (1, 0, 1, 0))
            for bad in ("0", "61", "abc", "-1", "1.5"):
                path = os.path.join(tmp, "cloud.env")
                with open(path, "w") as f:
                    f.write(FILE % bad)
                os.chmod(path, 0o600)
                with self.assertRaises(real.CloudError, msg="value %r must be refused" % bad) as caught:
                    real.apply_settings({}, path)
                self.assertEqual(caught.exception.code, real.EXIT_USAGE)
                self.assertIn("CW_FINAL_NOTE_MINUTES", str(caught.exception), "refused for this key, not for another reason")
            with open(path, "w") as f:   # an empty value is "not set" for every setting (parse_settings): no key, so the default 10 applies
                f.write(FILE % "")
            self.assertEqual(real.parse_settings(FILE % "").get("CW_FINAL_NOTE_MINUTES"), None)
            e, _ = run([pod(left=10)], tempfile.mkdtemp(dir=tmp), env={"CW_FINAL_NOTE_MINUTES": ""})
            self.assertEqual((e.kube.sent, [l["reason"] for l in e.lines()]), ([], ["unreadable setting"]), "an empty value in the environment is no note, one line")
            for good in ("1", "10", "60"):
                with open(path, "w") as f:
                    f.write(FILE % good)
                real.apply_settings({}, path)

    def test_s4_ended_worker_and_persistent_orchestrator_get_nothing(self):
        pods = [pod("w1", left=10, phase="Succeeded"), pod("w2", "orch", hard=False), pod("w3", "Bad Name", left=10)]
        with tempfile.TemporaryDirectory() as tmp:
            s, _ = run(pods, tmp)
            self.assertEqual(s.kube.sent, [])
            self.assertEqual(s.lines(), [])

    def test_s5_a_pod_shorter_than_twice_n_gets_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = {}
            for n, life in ((10, 19), (10, 20), (15, 29), (15, 30)):
                s, _ = run([pod(left=n, life=life)], tempfile.mkdtemp(dir=tmp), env={"CW_FINAL_NOTE_MINUTES": str(n)})
                r[(n, life)] = len(s.kube.sent)
        self.assertEqual(r, {(10, 19): 0, (10, 20): 1, (15, 29): 0, (15, 30): 1})

    def test_s6_once_across_passes_and_a_restart_annotation_first_one_audit_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, _ = run([pod(left=10)], tmp, passes=3)
            s.reap(s.launcher())   # a new reaper object on the same pod list
            note = s.pods[0]["metadata"]["annotations"].get("cw/final-note")
            lines = s.lines()
        self.assertEqual(len(s.kube.sent), 1, "one note across 3 passes and a restart")
        self.assertEqual(note, "sent")
        self.assertEqual(s.kube.order[:2], [("annotate", "w1"), ("exec", "w1")], "the annotation is set before the send")
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("10", lines[0]["reason"])
        self.assertTrue(lines[0]["ok"])

    def test_s7_unreadable_data_no_note_one_audit_line_pass_goes_on(self):
        cases = [("annotation", pod(left=10, hard_text="not a date"), {}, "unreadable annotation"),
                 ("started", pod(left=10, started="garbage"), {}, "unreadable annotation"),
                 ("setting", pod(left=10), {"CW_FINAL_NOTE_MINUTES": "abc"}, "unreadable setting")]
        for label, p, env, why in cases:
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                s, outs = run([p], tmp, passes=3, env=env)
                lines = s.lines()
                base, _ = run([], tempfile.mkdtemp(dir=tmp))
                self.assertEqual(s.kube.sent, [])
                self.assertEqual(len(lines), 1, lines)
                self.assertFalse(lines[0]["ok"])
                self.assertIn(why, lines[0]["reason"])
                self.assertEqual(s.pods[0]["metadata"]["annotations"].get("cw/final-note"), "unreadable")
                self.assertEqual(outs[0][0], [], "the pass returns its results as before")

    def test_u2_a_failing_send_never_raises_out_of_the_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            s, outs = run([pod(left=10)], tmp, passes=2, fail_exec=True)
        out, err = outs[0]
        self.assertEqual(out, [])
        self.assertEqual(len([x for x in err.splitlines() if "final note" in x]), 1, err)
        self.assertEqual(s.pods[0]["metadata"]["annotations"].get("cw/final-note"), "sent", "no retry")

    def test_u5_the_minute_forty_note_is_unchanged(self):
        self.assertEqual(real.WIP_NOTE_AFTER_SECONDS, 2400)
        self.assertEqual(real.WIP_NOTE_MARK, "wip_note")
        self.assertEqual(real.wip_note("w"), "Your git token ends in about 20 minutes. Save your work now: commit it, then push it to your "
                         "work-in-progress branch w/wip with exactly this command (plain, never forced): git push origin HEAD:refs/heads/w/wip")
        started = T0 - 40 * MIN
        self.assertTrue(real.wip_note_due(started, T0, ""))
        self.assertFalse(real.wip_note_due(started + MIN, T0, ""))
        self.assertFalse(real.wip_note_due(started, T0, "sent"))


def mutant_copy(directory, kind):
    with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
        src = f.read()
    if kind == "twice":
        old = "if not raw_end or ann(pod, \"final-note\"):"
        new = "if not raw_end:"
    else:
        old = "                self.kube.annotate(name, uid, **{FINAL_NOTE_MARK: \"sent\"})\n                self.send_note(name, FINAL_NOTE_TEXT)\n"
        new = "                self.kube.annotate(name, uid, **{FINAL_NOTE_MARK: \"sent\"})\n"
    assert src.count(old) == 1
    path = os.path.join(directory, "cloud_worker_final_%s.py" % kind)
    with open(path, "w") as f:
        f.write(src.replace(old, new))
    return path


class Mutants(unittest.TestCase):
    def failing(self, kind):
        with tempfile.TemporaryDirectory() as tmp:
            path = mutant_copy(tmp, kind)
            p = subprocess.run([sys.executable, "-m", "unittest", "test_final_note.FinalNote"], cwd=HERE, env=dict(os.environ, **{MODULE_ENV: path}),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)
        return p

    def test_mutant_second_send_is_caught(self):
        p = self.failing("twice")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("FAIL: test_s6_", p.stdout)

    def test_mutant_never_sends_is_caught(self):
        p = self.failing("never")
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("FAIL: test_s2_", p.stdout)


if __name__ == "__main__":
    unittest.main()
