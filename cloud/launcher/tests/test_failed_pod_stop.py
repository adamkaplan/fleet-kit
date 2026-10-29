"""Ask #1526: the reaper ends a pod in the phase Failed within one pass (cause pod_failed) and the alert says why, by a fixed label.

Offline: the fake cluster of test_cloud_worker and the alert's own pass over the audit file. Mutants are copies of the module text in memory."""
import json
import os
import re
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "bin"))
import test_cloud_worker as tcw  # noqa: E402
import record_json  # noqa: E402
import cloud_end_alert as alert  # noqa: E402

cw = tcw.cw
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
HOSTILE = "boom\n\x1b[31m ghp_abcdefghijklmnopqrstuvwxyz0123456789 " + "A" * 5000
WRITES = ("delete", "patch", "apply", "create", "replace", "annotate", "label")


class Failed(tcw.Base):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cw.Launcher, "run_hook", lambda launcher, name: {"pushed": False, "wip_branch": None})
        patch.start()
        self.addCleanup(patch.stop)
        self.alert_dir = os.path.join(self.tmp.name, "alert")
        os.makedirs(os.path.join(self.alert_dir, "cloud"))
        self.sent = []

    # -- helpers --
    def reap(self, module=cw):
        return module.Launcher(module.Kube(self.env), self.env, self.clock).reap()

    def set_pod(self, worker="coder7", phase="Failed", status=None, annotations=None):
        st = self.st()
        pod = st["pods"][cw.pod_name(worker)]
        pod["status"] = dict(status or {}, **({"phase": phase} if phase is not None else {}))
        pod["metadata"].setdefault("annotations", {}).update(annotations or {})
        self.write_state(st)

    def calls(self):
        if not os.path.exists(self.env["FAKE_KUBE_CALLS"]):
            return []
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return [json.loads(x) for x in f]

    def write_calls(self, since):
        return [c for c in self.calls()[since:] if any(w in c for w in WRITES)]

    def stops(self):
        return [l for l in self.audit_lines() if l["action"] == "stop"]

    def alert_pass(self, module=alert):
        audit = self.env["CW_CLOUD_AUDIT"]
        return module.scan_audit(audit, os.path.join(self.alert_dir, "state.json"), os.path.join(self.alert_dir, "cloud"), {},
                                 lambda to, text: self.sent.append((to, text)), record_json.parse_bounded)

    def start_alert(self):
        with open(os.path.join(self.alert_dir, "cloud", "coder7.json"), "w") as f:
            json.dump({"worker": "coder7", "reports_to": "coder-lead"}, f)
        self.alert_pass()   # the first pass stores the cursor at the end of the audit

    def old_failed(self, status=None, annotations=None):
        self.create()
        self.clock.advance(minutes=5)
        self.set_pod(status=status, annotations=annotations)

    def mutant(self, *pairs):
        with open(cw.__file__) as f:
            text = f.read()
        for old, new in pairs:
            self.assertEqual(text.count(old), 1, "the mutation did not apply: %r" % old)
            text = text.replace(old, new)
        mod = types.ModuleType("cw_mutant_failed")
        mod.__file__ = cw.__file__
        sys.modules["cw_mutant_failed"] = mod
        self.addCleanup(sys.modules.pop, "cw_mutant_failed", None)
        exec(compile(text, "cw_mutant_failed", "exec"), mod.__dict__)
        mod.Launcher.run_hook = lambda launcher, name: {"pushed": False, "wip_branch": None}
        return mod

    PHASE_LINE = '(pod.get("status") or {}).get("phase") != "Failed" or '
    KEPT_TEST = 'ann(pod, "kept") == "true" or ann(pod, "persistent")'

    # -- S2 --
    def test_s2_a_failed_pod_is_stopped_once_in_one_pass_and_alerted_with_two_messages(self):
        self.create()
        self.start_alert()
        self.clock.advance(minutes=5)
        self.set_pod()
        self.assertEqual(self.reap(), [("coder7", "pod failed", 0)])
        stops = self.stops()
        self.assertEqual([(l["cause"], l["ok"]) for l in stops], [("pod_failed", True)])
        self.assertIsNone(self.pod())
        self.assertEqual(self.st().get("secrets", {}), {})
        self.assertEqual(len(self.alert_pass()), 2)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(sorted(to for to, _ in self.sent), ["chief-of-staff", "coder-lead"])
        self.assertEqual(self.reap(), [])   # the pod is gone: no second stop
        self.assertEqual(len(self.stops()), 1)

    def test_s4_a_mutant_that_ignores_the_phase_fails_s2(self):
        mod = self.mutant((self.PHASE_LINE, ""))
        self.create()
        self.clock.advance(minutes=5)   # a Running pod whose activity cannot be read
        self.assertEqual(self.reap(mod), [("coder7", "pod failed", 0)])   # the real code reports activity unreadable and stops nothing
        self.assertEqual(self.reap(), [])
        self.assertEqual(len(self.stops()), 1)

    # -- S3 --
    def test_s3_no_other_pod_is_stopped_by_the_rule_and_no_write_call_is_made(self):
        cases = {"kept": ("Failed", {"cw/kept": "true"}), "running": ("Running", {}), "pending": ("Pending", {}),
                 "succeeded": ("Succeeded", {}), "missing": (None, {}), "odd": ("failed", {}), "odd2": ("Failed ", {})}
        for label, (phase, annotations) in cases.items():
            with self.subTest(case=label):
                self.write_state({"pods": {}})
                self.create()
                self.clock.advance(minutes=5)
                self.set_pod(phase=phase, annotations=annotations)
                before = len(self.calls())
                self.assertEqual(self.reap(), [("coder7", "activity unreadable", 1)])
                self.assertEqual(self.write_calls(before), [])
                self.assertIsNotNone(self.pod())
                self.assertEqual([l for l in self.stops() if l.get("cause") == "pod_failed"], [])

    def test_s3_a_running_pod_is_still_judged_by_the_idle_rule(self):
        self.create()
        self.set_activity("coder7", False, self.clock())
        self.clock.advance(minutes=12)
        self.set_pod(phase="Running")
        self.assertEqual([r[:2] for r in self.reap()], [("coder7", "idle")])

    def test_s4_a_mutant_that_stops_a_kept_failed_pod_fails_s3(self):
        mod = self.mutant((self.KEPT_TEST, 'False or ann(pod, "persistent")'))
        self.old_failed(annotations={"cw/kept": "true"})
        self.assertEqual(self.reap(), [("coder7", "activity unreadable", 1)])
        self.assertEqual(self.reap(mod)[0][:2], ("coder7", "pod failed"))   # the mutant stops the kept pod

    # -- S6 --
    def test_s6_a_pod_the_floor_killed_is_still_hard_limit(self):
        self.create("coder7", "--hard", "1h", "--max-hard", "1h")
        self.set_pod(status={"reason": "DeadlineExceeded"})
        self.clock.advance(hours=1, minutes=6)
        self.assertEqual(self.reap(), [("coder7", "hard limit", 0)])
        self.assertEqual([l["cause"] for l in self.stops()], ["pod_failed"])   # the stop of a Failed pod names it, as before

    def test_s6_a_persistent_pod_that_failed_is_untouched(self):
        self.old_failed(annotations={"cw/persistent": "true"})
        before = len(self.calls())
        self.assertEqual(self.reap(), [("coder7", "activity unreadable", 1)])
        self.assertEqual(self.write_calls(before), [])
        self.assertIsNotNone(self.pod())

    def test_s6_a_pod_younger_than_the_create_grace_is_left_and_gives_one_stop_and_one_alert(self):
        self.assertEqual(cw.CREATE_GRACE_SECONDS, 120)
        self.create()
        self.start_alert()
        self.clock.advance(seconds=cw.CREATE_GRACE_SECONDS - 1)
        self.set_pod()
        before = len(self.calls())
        self.assertEqual(self.reap(), [("coder7", "activity unreadable", 1)])
        self.assertEqual((self.write_calls(before), self.stops()), ([], []))
        # the create path's own cleanup removes the pod; the reaper then makes no stop
        st = self.st()
        st["pods"].pop(cw.pod_name("coder7"))
        self.write_state(st)
        self.assertEqual(self.reap(), [])
        self.assertEqual(self.stops(), [])
        # a pod the create path did not remove ends exactly once after the grace
        self.write_state({"pods": {}})
        self.create()
        self.start_alert()
        self.clock.advance(seconds=cw.CREATE_GRACE_SECONDS - 1)
        self.set_pod()
        self.reap()
        self.clock.advance(seconds=2)
        self.reap()
        self.reap()
        self.assertEqual(len([l for l in self.stops() if l["cause"] == "pod_failed"]), 1)
        self.assertEqual(len(self.alert_pass()), 2)
        self.assertEqual(self.alert_pass(), [])

    def test_s6_a_mutant_without_the_create_grace_stops_a_young_pod(self):
        mod = self.mutant(("return (now - parse_iso(ann(pod, \"started\"))).total_seconds() >= CREATE_GRACE_SECONDS", "return True"))
        self.create()
        self.set_pod()
        self.assertEqual(self.reap(), [("coder7", "activity unreadable", 1)])
        self.assertEqual(self.reap(mod)[0][:2], ("coder7", "pod failed"))

    # -- S7 --
    LABELS = [("OOMKilled", "OOMKilled"), ("Error", "Error"), ("DeadlineExceeded", "DeadlineExceeded"), ("Evicted", "Evicted"),
              ("SomethingElse", "other"), (HOSTILE, "other"), ("oomkilled", "other"), (None, "other"), (7, "other")]

    def terminated(self, reason, code=None, key="state"):
        term = {"reason": reason}
        if code is not None:
            term["exitCode"] = code
        return {"containerStatuses": [{"name": "worker", key: {"terminated": term}}]}

    def test_s7_the_label_is_chosen_from_the_fixed_set(self):
        for reason, want in self.LABELS:
            for key in ("state", "lastState"):
                with self.subTest(reason=str(reason)[:12], key=key):
                    self.assertEqual(cw.failure_of({"status": self.terminated(reason, key=key)})[0], want)
        self.assertEqual(cw.failure_of({"status": {"reason": "Evicted"}})[0], "Evicted")
        self.assertEqual(cw.failure_of({"status": {"reason": "DeadlineExceeded"}})[0], "DeadlineExceeded")
        self.assertEqual(cw.failure_of({"status": {"reason": HOSTILE}})[0], "other")
        self.assertEqual(cw.failure_of({})[0], "other")
        self.assertEqual(cw.failure_of({"status": {"containerStatuses": "x"}})[0], "other")

    def test_s7_the_exit_code_is_kept_only_as_an_int_from_0_to_255(self):
        for code, want in ((137, 137), (0, 0), (255, 255), (True, None), ("137", None), (300, None), (-1, None), (1.5, None), (None, None)):
            with self.subTest(code=code):
                self.assertEqual(cw.failure_of({"status": self.terminated("Error", code)})[1], want)

    def test_s7_the_record_the_stop_line_and_the_message_carry_only_the_label_and_the_code(self):
        self.create()
        self.start_alert()
        self.clock.advance(minutes=5)
        self.set_pod(status=self.terminated(HOSTILE, 137))
        self.reap()
        stop = self.stops()[0]
        self.assertEqual((stop["failure"], stop["exit_code"]), ("other", 137))
        record = re.search(r"run record (\S+)", stop["reason"])
        self.assertIsNotNone(record, stop["reason"])
        with open(os.path.join(record.group(1), "run.json")) as f:
            run_text = f.read()
        run = json.loads(run_text)
        self.assertEqual((run["failure"], run["exit_code"]), ("other", 137))
        self.alert_pass()
        self.assertEqual(len(self.sent), 2)
        for to, text in self.sent:
            self.assertTrue(text.endswith(" Pod termination: other, exit code 137."), text)
        with open(self.env["CW_CLOUD_AUDIT"]) as f:
            audit_text = f.read()
        for where in (run_text, audit_text, " ".join(t for _, t in self.sent)):
            for bad in ("ghp_", "\x1b", "AAAA", "boom"):
                self.assertNotIn(bad, where)

    def test_s7_a_bad_exit_code_is_absent_from_the_line(self):
        for code in (True, "137", 300, -1):
            with self.subTest(code=code):
                self.write_state({"pods": {}})
                self.old_failed(status=self.terminated("Error", code))
                self.reap()
                self.assertEqual(self.stops()[-1]["failure"], "Error")
                self.assertNotIn("exit_code", self.stops()[-1])

    def test_s7_the_alert_validates_again_and_a_line_without_the_fields_gives_the_five_fact_text(self):
        line = {"cause": "pod_failed"}
        self.assertIsNone(alert.failure_text(line))
        for bad in (HOSTILE, "oom", None, 5, ["Error"]):
            self.assertIsNone(alert.failure_text(dict(line, failure=bad)))
        self.assertEqual(alert.failure_text(dict(line, failure="Error")), "Error")
        for code in (True, "1", 256, -1, 2.0):
            self.assertEqual(alert.failure_text(dict(line, failure="Error", exit_code=code)), "Error")
        self.assertEqual(alert.failure_text(dict(line, failure="OOMKilled", exit_code=137)), "OOMKilled, exit code 137")
        self.assertNotIn("Pod termination", alert.message_text("w", "pod failed", "2026-01-01T10:00:00Z", "no", None))

    def test_s7_an_edited_audit_line_never_puts_raw_text_in_a_message(self):
        self.create()
        self.start_alert()
        with open(self.env["CW_CLOUD_AUDIT"], "a") as f:
            f.write(json.dumps({"ts": "2026-01-01T10:00:00Z", "action": "stop", "worker": "coder7", "ok": True, "cause": "pod_failed",
                                "reason": "pod failed; run-id=abc123", "failure": HOSTILE, "exit_code": "9"}) + "\n")
        self.alert_pass()
        self.assertEqual(len(self.sent), 2)
        for _, text in self.sent:
            self.assertNotIn("Pod termination", text)
            self.assertNotIn("ghp_", text)

    def test_s7_a_mutant_that_copies_the_reason_is_caught(self):
        mod = self.mutant(('label = next((l for l in FAILURE_LABELS if isinstance(reason, str) and reason == l), "other")',
                           'label = reason if isinstance(reason, str) else "other"'))
        self.old_failed(status=self.terminated(HOSTILE, 1))
        self.reap(mod)
        with open(self.env["CW_CLOUD_AUDIT"]) as f:
            self.assertIn("ghp_", f.read())   # the real code writes the hostile string in 0 places

    def test_s7_an_alert_mutant_that_copies_the_label_is_caught(self):
        with open(os.path.join(ROOT, "bin", "cloud_end_alert.py")) as f:
            text = f.read()
        old = "if not isinstance(label, str) or label not in FAILURES:"
        self.assertEqual(text.count(old), 1)
        mod = types.ModuleType("alert_mutant")
        exec(compile(text.replace(old, "if not isinstance(label, str):"), "alert_mutant", "exec"), mod.__dict__)
        self.assertEqual(mod.failure_text({"failure": HOSTILE}), HOSTILE)
        self.assertIsNone(alert.failure_text({"failure": HOSTILE}))


class FailedDocs(unittest.TestCase):
    def text(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return " ".join(f.read().split())

    PHRASES = ("a failed pod is ended and alerted within one pass", "the earlier wording",
               "is not recoverable either way", "unpushed work in it is lost with or without this change",
               "only shortens the time to removal and adds the alert", "a burst of failed pods", "one alert per worker",
               "the hard and idle rules judge first", "kept for unpushed work", "persistent orchestrator", "younger than")

    def test_s8_the_interface_doc_and_the_launcher_readme_say_it(self):
        interface = self.text("docs", "cloud-worker-interface.md").lower()
        readme = self.text("cloud", "launcher", "README.md").lower()
        for phrase in self.PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), interface)
        for phrase in ("a failed pod is ended and alerted within one pass", "the earlier wording", "is not recoverable either way",
                       "unpushed work in it is lost with or without this change", "one alert per worker", "a pod kept for unpushed work"):
            with self.subTest(readme=phrase):
                self.assertIn(phrase.lower(), readme)

    def test_s8_the_old_wording_is_gone(self):
        for text in (self.text("docs", "cloud-worker-interface.md"), self.text("cloud", "launcher", "README.md"), self.text("bin", "cloud_end_alert.py")):
            self.assertEqual(text.count("is alerted only then"), 0)
            self.assertEqual(text.count("is filed as #1526"), 0)
            self.assertEqual(text.count("the reaper does not stop a Failed pod sooner"), 0)

    def test_s8_a_document_without_a_phrase_is_caught(self):
        interface = self.text("docs", "cloud-worker-interface.md")
        self.assertEqual(interface.count("lost with or without this change"), 1)
        broken = interface.replace("lost with or without this change", "").lower()
        self.assertEqual(sum(p.lower() in broken for p in self.PHRASES), len(self.PHRASES) - 1)


if __name__ == "__main__":
    unittest.main()
