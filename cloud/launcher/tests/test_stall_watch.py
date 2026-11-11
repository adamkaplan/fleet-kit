"""Ask #2399: the stall watch (cloud/launcher/stall_watch.py). A fake pod, a fake clock, a recording switchboard. Offline; no cluster."""
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import stall_watch as sw  # noqa: E402

START = dt.datetime(2026, 11, 11, 12, 0, 0, tzinfo=dt.timezone.utc)
SOURCE = os.path.join(LAUNCHER, "stall_watch.py")
TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
NAME_OK = lambda n: bool(n) and n.replace("-", "").isalnum()  # noqa: E731


class FakeKube:
    """The cluster: annotate writes the pod's annotations (as the real one does), exec answers the probe with the pod's file state at the fake time."""
    def __init__(self, world):
        self.world, self.execs = world, []

    def exec(self, name, *cmd, **kw):
        self.execs.append((name, kw.get("timeout")))
        pod = self.world.pods[name]
        if pod["exec_fails"]:
            raise RuntimeError("kubectl exec failed: secret-pod-text")
        return json.dumps(pod["probe"](self.world.now)) + "\n"

    def annotate(self, name, uid, **kv):
        ann = self.world.pods[name]["doc"]["metadata"]["annotations"]
        for k, v in kv.items():
            ann["cw/" + k.replace("_", "-")] = str(v)

    def scrub(self, text, limit=300):
        return str(text).replace(TOKEN, "[redacted]")[:limit]


class World:
    def __init__(self):
        self.now, self.pods, self.err = START, {}, ""
        self.kube = FakeKube(self)
        self.dir = tempfile.mkdtemp(prefix="stall-")
        self.launcher = types.SimpleNamespace(kube=self.kube, env={"CW_CLOUD_RECORD_DIR": self.dir, "XDG_CONFIG_HOME": tempfile.mkdtemp(prefix="nocfg-")})
        self.audits = []   # the audit lines the launcher's own writer got: (worker, runtime_id, action, cost, ok, reason, extra)

        def audit(worker, runtime_id, action, cost, ok, reason="", extra=None):
            self.audits.append({"worker": worker, "runtime_id": runtime_id, "action": action, "ok": ok, "reason": reason, **(extra or {})})
        self.launcher.audit = audit

    @property
    def sent(self):   # one audit line per alert: the daemon sends the messages
        return self.audits

    def add(self, name="w1-pod", age_minutes=60, boss="coder-lead", progress_age=1, tail=("working",), runs=(), exits=(), msg="all fine", msg_age=1, exec_fails=False):
        """progress_age: minutes since the progress file changed. runs: (name, age minutes, last line). exits: (name, code, age minutes)."""
        def probe(now, n=name):
            t = lambda minutes: now.timestamp() - minutes * 60  # noqa: E731
            p = self.pods[n]
            return {"now": now.timestamp(), "progress": {"mtime": t(p["progress_age"]), "tail": list(p["tail"])},
                    "runs": [{"name": r[0], "mtime": t(r[1]), "last": r[2]} for r in p["runs"]],
                    "exits": [{"name": e[0], "code": str(e[1]), "mtime": t(e[2])} for e in p["exits"]], "msg": p["msg"], "msg_time": t(p["msg_age"])}
        started = (self.now - dt.timedelta(minutes=age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc = {"metadata": {"name": name, "uid": "u-" + name, "annotations": {"cw/worker": "w1", "cw/started": started}}, "status": {"phase": "Running"}}
        self.pods[name] = {"doc": doc, "probe": probe, "progress_age": progress_age, "tail": tail, "runs": runs, "exits": exits, "msg": msg, "msg_age": msg_age, "exec_fails": exec_fails}
        if boss:
            with open(os.path.join(self.dir, "w1.json"), "w") as f:
                json.dump({"worker": "w1", "reports_to": boss}, f)

    def set_results(self, name, line):
        """The pod's /work/results.txt last line, added to the probe's answer (the real probe reads it in the same one exec)."""
        inner = self.pods[name]["probe"]
        self.pods[name]["probe"] = lambda now: dict(inner(now), results=line)

    def run(self, module=sw):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = module.watch_pass(self.launcher, [p["doc"] for p in self.pods.values()], self.now, NAME_OK)
        self.err = err.getvalue()
        return out

    def at(self, minutes):
        self.now = START + dt.timedelta(minutes=minutes)


def conditions(out):
    return sorted(c for _, c in out)


class Stall(unittest.TestCase):
    def test_s2_progress_stale_fires_at_16_not_14_and_an_active_run_keeps_it_silent(self):
        w = World()
        w.add(progress_age=14, runs=[("build", 14, "x")], exits=[("build", 0, 14)])
        self.assertEqual(w.run(), [])
        w = World()
        w.add(progress_age=16, runs=[("build", 16, "x")], exits=[("build", 0, 16)])
        self.assertEqual(conditions(w.run()), ["progress-stale"])
        w = World()
        w.add(progress_age=16, runs=[("build", 2, "still going")])   # no exit file, log changed 2 minutes ago: active
        self.assertEqual(w.run(), [])

    def test_s3_refusal_texts_fire_and_a_clean_pod_does_not(self):
        cases = [dict(msg="Permission denied: shell"), dict(tail=("step 1", "Blocked")), dict(tail=("a", "boom error x", "b", "boom error x", "c", "boom error x", "d")), dict(tail=("step", "blocked"))]
        for kw in cases:
            w = World()
            w.add(**kw)
            self.assertEqual(conditions(w.run()), ["refusal"], kw)
        w = World()
        w.add(msg="all done, pushing", tail=("compiling", "tests passed"))
        self.assertEqual(w.run(), [])

    def test_s4_failed_run_idle_at_11_not_9_and_not_for_exit_0(self):
        for idle, code, want in ((9, 2, []), (11, 2, ["failed-run-idle"]), (11, 0, [])):
            w = World()
            w.add(progress_age=idle, msg_age=idle, runs=[("t", idle, "FAILED")], exits=[("t", code, idle)])
            self.assertEqual(conditions(w.run()), want, (idle, code))

    def _s5(self, module):
        w = World()
        w.add(progress_age=1, msg="Permission denied: shell")
        sizes = []
        for minute in (0, 1, 2, 61):
            w.at(minute)
            w.run(module)
            sizes.append(len(w.sent))
            if minute == 2 and sizes[-1] != 1:
                raise AssertionError("a stall was alerted twice within the hour: %d messages after 3 passes" % sizes[-1])
        return w, sizes

    def test_s5_one_alert_per_condition_per_hour_to_the_boss_and_the_chief(self):
        w, sizes = self._s5(sw)
        self.assertEqual(sizes, [1, 1, 1, 2])   # one audit line in pass 1, none in passes 2 and 3, one again at 61 minutes
        self.assertEqual(w.sent[0]["recipients"], ["coder-lead"])
        self.assertEqual(w.pods["w1-pod"]["doc"]["metadata"]["annotations"]["cw/stall-alert-refusal"], "2026-11-11T13:01:00Z")

    def test_s9_a_mutant_without_the_mark_fails_with_the_anchored_text_after_its_control_passes(self):
        self._s5(sw)   # the control: the unmutated module passes
        with open(SOURCE) as f:
            text = f.read()
        old = "                if mark and (last is None or 0 <= (now - last).total_seconds() < REALERT_SECONDS):\n                    continue"
        self.assertEqual(text.count(old), 1, "the mutation did not apply")
        mod = types.ModuleType("stall_watch_mutant")
        exec(compile(text.replace(old, "                if False:\n                    continue"), "mutant", "exec"), mod.__dict__)
        with self.assertRaises(AssertionError) as caught:
            self._s5(mod)
        self.assertIn("a stall was alerted twice within the hour", str(caught.exception))

    def test_s6_the_evidence_hides_a_token_and_is_cut_to_300_characters(self):
        w = World()
        w.add(msg="Permission denied: git push https://x:%s@github.com/o/r %s" % (TOKEN, "y" * 500))
        w.run()
        self.assertTrue(w.sent)
        for text in (a["evidence"] for a in w.sent):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(TOKEN[4:], text)
            self.assertIn("[redacted]", text)
        self.assertLessEqual(len(sw.evidence("z " * 400)), 300)
        self.assertLessEqual(len(sw.evidence("Permission denied " + "y" * 500)), 300)
        for secret, piece in (("password=hunter2hunter2", "hunter2hunter2"), ("Bearer abcdefghijklmnop12345", "abcdefghijklmnop12345"),
                              ("AKIAABCDEFGHIJKLMNOP", "AKIAABCDEFGHIJKLMNOP"), ("sk-abcdefghijklmnopqr", "sk-abcdefghijklmnopqr")):
            self.assertNotIn(piece, sw.evidence("saw " + secret))

    def test_s7_a_young_pod_and_a_failing_exec_send_nothing_and_leave_one_line_each(self):
        w = World()
        w.add("young-pod", age_minutes=9, msg="Permission denied")
        w.add("broken-pod", exec_fails=True)
        self.assertEqual(w.run(), [])
        self.assertEqual(w.sent, [])
        line = sw.pass_text(w.launcher)
        self.assertIn("young-pod:younger-than-10-minutes", line)
        self.assertIn("broken-pod:the-read-failed", line)
        self.assertEqual(w.err, "", "a skip adds no line of its own to the log: the pass line carries it")
        self.assertNotIn("secret-pod-text", line)
        self.assertNotIn("secret-pod-text", w.err)
        self.assertEqual([n for n, _ in w.kube.execs], ["broken-pod"], "a young pod is not read")

    def test_s8_twelve_pods_are_read_with_one_exec_each_and_the_pass_line_counts_them(self):
        w = World()
        for i in range(12):
            w.add("pod%d-x" % i, boss=None, msg="Permission denied" if i == 0 else "fine")
        w.run()
        self.assertEqual(sorted(n for n, _ in w.kube.execs), sorted("pod%d-x" % i for i in range(12)))
        self.assertTrue(all(t == sw.EXEC_SECONDS for _, t in w.kube.execs))
        self.assertEqual(sw.pass_text(w.launcher), " stall read=12 alerted=1")

    def test_s4_a_recent_session_change_keeps_failed_run_idle_silent_and_an_exit_0_beside_a_failed_one_does_not_hide_it(self):
        w = World()
        w.add(progress_age=30, msg_age=2, runs=[("t", 30, "FAILED")], exits=[("t", 2, 30)])
        self.assertEqual([c for c in conditions(w.run()) if c == "failed-run-idle"], [])
        w = World()
        w.add(progress_age=11, msg_age=11, runs=[("a", 11, "ok"), ("b", 11, "FAILED")], exits=[("a", 0, 11), ("b", 1, 11)])
        self.assertEqual(conditions(w.run()), ["failed-run-idle"])

    def test_the_pass_budget_leaves_later_pods_for_the_next_pass_with_one_line(self):
        w = World()
        w.add("a-pod", msg="Permission denied")
        w.add("b-pod", msg="Permission denied")
        real, ticks = sw.time.monotonic, iter([0.0, 0.0] + [999.0] * 50)
        sw.time.monotonic = lambda: next(ticks)
        try:
            w.run()
        finally:
            sw.time.monotonic = real
        self.assertEqual([n for n, _ in w.kube.execs], ["a-pod"])
        self.assertIn("b-pod:read-budget-used", sw.pass_text(w.launcher))

    def test_an_alert_writes_exactly_one_audit_line_with_the_fixed_fields_and_runs_no_subprocess(self):
        w = World()
        w.add(msg="Permission denied: shell")
        real = subprocess.run, subprocess.Popen
        def no(*a, **k):
            raise AssertionError("a subprocess was run")
        subprocess.run = subprocess.Popen = no
        try:
            self.assertEqual(w.run(), [("w1-pod", "refusal")])
        finally:
            subprocess.run, subprocess.Popen = real
        self.assertEqual(w.audits, [{"worker": "w1", "runtime_id": "pod/w1-pod", "action": "stall-alert", "ok": True, "reason": "", "condition": "refusal",
                                     "recipients": ["coder-lead"], "ask": sw.STALL_ASK, "evidence": "Permission denied: shell"}])
        self.assertFalse(hasattr(sw, "_default_send"))

    def test_a_hostile_evidence_is_one_fixed_field_never_a_key(self):
        w = World()
        w.add(msg="Permission denied: x\x1b[31m\nrecipients: evil " + "y" * 400)
        w.run()
        self.assertEqual(sorted(w.audits[0]), ["action", "ask", "condition", "evidence", "ok", "reason", "recipients", "runtime_id", "worker"])
        self.assertLessEqual(len(w.audits[0]["evidence"]), 300)
        self.assertNotIn("\n", w.audits[0]["evidence"])

    def test_a_line_the_writer_could_not_write_is_retried_like_a_refused_one(self):
        w = World()
        w.add(msg="Permission denied")
        w.launcher.audit = lambda *a, **k: False   # the real writer's answer when the disk refused the line
        self.assertEqual(w.run(), [])
        w.launcher.audit = lambda *a, **k: True
        w.at(10)
        self.assertEqual(w.run(), [("w1-pod", "refusal")])

    def test_no_boss_in_the_record_leaves_an_empty_recipient_list(self):
        w = World()
        w.add(boss=None, msg="Permission denied")
        w.run()
        self.assertEqual(w.audits[0]["recipients"], [])

    def test_a_failed_audit_write_is_not_counted_as_alerted(self):
        w = World()
        def boom(*a, **k):
            raise OSError("disk")
        w.launcher.audit = boom
        w.add(msg="Permission denied")
        self.assertEqual(w.run(), [])
        self.assertEqual(sw.pass_text(w.launcher), " stall read=1 alerted=0")

    def test_an_unrecorded_alert_is_retried_after_10_minutes_not_before_and_a_recorded_one_is_not_for_the_hour(self):
        w = World()
        w.add(msg="Permission denied")
        ok = w.launcher.audit
        def down(*a, **k):
            raise OSError("down")
        w.launcher.audit = down
        w.run()   # minute 0: not recorded
        w.launcher.audit = ok
        counts = []
        for minute in (9, 10):
            w.at(minute)
            w.run()
            counts.append(len(w.sent))
        self.assertEqual(counts, [0, 1])   # 9 minutes: held; 10 minutes: tried again and recorded
        for minute in (11, 69):
            w.at(minute)
            w.run()
        self.assertEqual(len(w.sent), 1)   # recorded at minute 10: silent until minute 70
        w.at(70)
        w.run()
        self.assertEqual(len(w.sent), 2)

    def test_the_chief_is_named_by_the_config_and_falls_back_to_cos(self):
        w = World()
        self.assertEqual(sw.cos_name(w.launcher), "cos")   # no config: the fallback
        home = tempfile.mkdtemp(prefix="cfg-")
        os.makedirs(os.path.join(home, "fl" + "eet-switchboard"))
        w.launcher.env["XDG_CONFIG_HOME"] = home
        path = os.path.join(home, "fl" + "eet-switchboard", "config.json")
        for text, want in (('{"cos_name": "boss-one"}', "boss-one"), ('{"cos_name": "Bad Name;"}', "cos"), ("{}", "cos"), ("not json", "cos")):
            with open(path, "w") as f:
                f.write(text)
            self.assertEqual(sw.cos_name(w.launcher), want, text)

    def _record(self, w, **rec):
        with open(os.path.join(w.dir, "w1.json"), "w") as f:
            json.dump(dict({"worker": "w1", "reports_to": "coder-lead"}, **rec), f)

    def test_the_ask_is_the_pods_own_issue_and_falls_back_to_the_stall_watch_ask(self):
        w = World()
        w.add()
        for rec, want in (({"issue": 812, "repo": "own/repo"}, "own/repo#812"), ({"issue": 7}, "7"), ({}, sw.STALL_ASK),
                          ({"issue": "x; rm", "repo": "own/repo"}, sw.STALL_ASK), ({"issue": True}, sw.STALL_ASK), ({"issue": 0}, sw.STALL_ASK),
                          ({"issue": 5, "repo": "--x/y"}, "5"), ({"issue": 5, "repo": "a/-y"}, "5")):
            self._record(w, **rec)
            self.assertEqual(sw.ask_of(w.launcher, "w1"), want, rec)
        os.remove(os.path.join(w.dir, "w1.json"))
        self.assertEqual(sw.ask_of(w.launcher, "w1"), sw.STALL_ASK)

    def test_the_audit_line_holds_the_pods_own_ask(self):
        w = World()
        w.add(msg="Permission denied")
        self._record(w, issue=812, repo="own/repo")
        w.run()
        self.assertEqual(w.audits[0]["ask"], "own/repo#812")

    def test_the_hour_holds_at_59_minutes_and_ends_at_60(self):
        w = World()
        w.add(msg="Permission denied")
        counts = []
        for minute in (0, 59, 60):
            w.at(minute)
            w.run()
            counts.append(len(w.sent))
        self.assertEqual(counts, [1, 1, 2])

    def test_the_mark_is_set_before_the_audit_line_and_nothing_else_is_called(self):
        w = World()
        order = []
        w.kube.annotate = lambda name, uid, **kv: order.append("annotate")
        w.launcher.audit = lambda *a, **k: order.append("audit")
        w.add(msg="Permission denied")
        w.run()
        self.assertEqual(order[:2], ["annotate", "audit"])
        self.assertEqual(set(order), {"annotate", "audit"})   # the fake kube offers exec and annotate only: no stop, delete or nudge is reachable

    def test_the_reaper_pass_calls_it_and_the_pass_line_carries_the_counts(self):
        with open(os.path.join(LAUNCHER, "cloud_worker.py")) as f:
            text = f.read()
        self.assertEqual(text.count('_stall("watch_pass", self, live, now, NAME_RE.fullmatch)'), 1)
        self.assertIn('_stall("pass_text", L) + step_text(', text)
        self.assertIn("the stall watch failed", text)
        self.assertEqual(sw.pass_text(None), "")

    def _finished(self, module, results="", **kw):
        w = World()
        w.add(progress_age=16, runs=[("build", 16, "x")], exits=[("build", 0, 16)], **kw)
        w.set_results("w1-pod", results)
        return w.run(module)

    def _finished_cases(self, module):
        # S2 (the control inside: an unfinished pod stale for 16 minutes is alerted)
        if conditions(self._finished(module, tail=("working",))) != ["progress-stale"]:
            raise AssertionError("an unfinished pod was called finished: a stale pod was not alerted")
        for kw in (dict(tail=("x", "done")), dict(tail=("x", " DONE  ")), dict(tail=("STATUS: done",)), dict(tail=("x",), results="STATUS: done")):
            if self._finished(module, **kw) != []:
                raise AssertionError("a finished pod was alerted as stalled: %r" % (kw,))
        # S3
        for line in ("not done yet", "All done and pushed"):
            if conditions(self._finished(module, tail=(line,))) != ["progress-stale"]:
                raise AssertionError("an unfinished pod was called finished: %r" % line)

    def test_s2_s3_a_finished_pod_is_not_stalled_and_a_sentence_with_done_still_fires(self):
        self._finished_cases(sw)   # the control: the unmutated module passes
        self.assertEqual(conditions(self._finished(sw, tail=("done", "more work"))), ["progress-stale"])
        self.assertEqual(conditions(self._finished(sw, tail=("x",), results="STATUS: running")), ["progress-stale"])

    def test_s4_a_finished_pod_still_fires_refusal_and_is_silent_for_failed_run_idle(self):
        w = World()
        w.add(progress_age=1, tail=("done",), msg="Permission denied: shell")
        self.assertEqual(conditions(w.run()), ["refusal"])
        w = World()
        w.add(progress_age=11, msg_age=11, tail=("done",), runs=[("t", 11, "FAILED")], exits=[("t", 2, 11)])
        self.assertEqual(w.run(), [])
        w = World()
        w.add(progress_age=11, msg_age=11, tail=("working",), runs=[("t", 11, "FAILED")], exits=[("t", 2, 11)])
        self.assertEqual(conditions(w.run()), ["failed-run-idle"])

    def test_s5_twelve_pods_one_finished_make_twelve_execs_and_the_other_eleven_alert_as_before(self):
        w = World()
        for i in range(12):
            w.add(name="pod%d-x" % i, progress_age=16, tail=("done",) if i == 0 else ("working",))
        out = w.run()
        self.assertEqual(sorted(n for n, _ in w.kube.execs), sorted("pod%d-x" % i for i in range(12)))
        self.assertEqual(sorted(n for n, _ in out), sorted("pod%d-x" % i for i in range(1, 12)))

    def test_s6_a_mutant_that_treats_every_pod_as_finished_fails_with_an_anchored_text_after_its_control(self):
        self._finished_cases(sw)   # the control passes first
        with open(SOURCE) as f:
            text = f.read()
        old = '    finished = last_p in ("done", "status: done") or'
        self.assertEqual(text.count(old), 1, "the mutation did not apply")
        mod = types.ModuleType("stall_watch_mutant2")
        exec(compile(text.replace(old, "    finished = True or"), "mutant", "exec"), mod.__dict__)
        with self.assertRaises(AssertionError) as caught:
            self._finished_cases(mod)
        self.assertIn("an unfinished pod was called finished", str(caught.exception))

    def test_the_probe_prints_one_json_line_on_a_machine_without_a_server(self):
        d = tempfile.mkdtemp(prefix="stall-probe-")
        env = dict(os.environ, CW_RUNS_DIR=d, CW_ACTIVITY_PORT="1", CW_ACTIVITY_PASSWORD_FILE=os.path.join(d, "none"))
        with open(os.path.join(d, "a.log"), "w") as f:
            f.write("one\ntwo\n")
        with open(os.path.join(d, "a.exit"), "w") as f:
            f.write("3\n")
        done = subprocess.run([sys.executable, "-c", sw.PROBE_CODE], capture_output=True, text=True, env=env, timeout=20)
        data = json.loads(done.stdout.strip().splitlines()[-1])
        self.assertEqual(data["runs"][0]["last"], "two")
        self.assertIn("results", data)
        self.assertEqual(data["exits"][0]["code"], "3")


if __name__ == "__main__":
    unittest.main()
