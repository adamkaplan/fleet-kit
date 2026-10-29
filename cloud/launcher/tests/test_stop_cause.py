"""Ask #1480: every stop the launcher makes of its own accord carries `cause` (idle, hard, pod_gone, pod_failed); a stop by a person's call carries none.
The vanished-pod check of the reaper's pass writes ONE stop line per vanished worker. Offline: the fake cluster of test_cloud_worker."""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as tcw  # noqa: E402

cw = tcw.cw
T0 = tcw.T0


class Cause(tcw.Base):
    def reap(self):
        return cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cw.Launcher, "run_hook", lambda launcher, name: {"pushed": False, "wip_branch": None})
        patch.start()
        self.addCleanup(patch.stop)

    def hook(self):
        pass

    def mutant(self, old, new, *more):
        """The launcher module with `old` replaced by `new` (and each further pair), each asserted applied, once."""
        import types
        with open(cw.__file__) as f:
            text = f.read()
        pairs = [(old, new)] + [more[i:i + 2] for i in range(0, len(more), 2)]
        for o, n in pairs:
            self.assertEqual(text.count(o), 1, "the mutation did not apply")
            text = text.replace(o, n)
        mod = types.ModuleType("cw_mutant")
        mod.__file__ = cw.__file__
        sys.modules["cw_mutant"] = mod
        self.addCleanup(sys.modules.pop, "cw_mutant", None)
        exec(compile(text, "cw_mutant", "exec"), mod.__dict__)
        mod.Launcher.run_hook = lambda launcher, name: {"pushed": False, "wip_branch": None}
        return mod

    def stops(self):
        return [l for l in self.audit_lines() if l["action"] == "stop"]

    def lose_pod(self, worker="coder7"):
        st = self.st()
        st["pods"].pop(cw.pod_name(worker))
        self.write_state(st)

    def test_s11_the_reaper_idle_stop_carries_idle(self):
        self.create()
        self.hook()
        self.set_activity("coder7", False, self.clock())
        self.clock.advance(minutes=12)
        self.assertEqual([r[:2] for r in self.reap()], [("coder7", "idle")])
        stops = self.stops()
        self.assertEqual([(l["cause"], l["ok"]) for l in stops], [("idle", True)])
        self.assertTrue(stops[0]["reason"].startswith("idle"))

    def test_s11_the_reaper_hard_stop_carries_hard(self):
        self.create("coder7", "--hard", "1h")
        self.hook()
        self.set_activity("coder7", True, T0)
        self.clock.advance(minutes=61)
        self.assertEqual([r[:2] for r in self.reap()], [("coder7", "hard limit")])
        stops = self.stops()
        self.assertEqual([l["cause"] for l in stops], ["hard"])
        self.assertTrue(stops[0]["reason"].startswith("hard limit"))

    def test_s11_a_stop_by_name_of_an_absent_pod_carries_no_cause(self):
        self.create()
        self.lose_pod()
        code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertEqual(code, 0, err)
        self.assertIs(json.loads(out).get("pod_gone"), True)
        stops = self.stops()
        self.assertEqual(len(stops), 1)
        self.assertNotIn("cause", stops[0])

    def test_s11_a_stop_by_name_of_a_failed_pod_carries_no_cause(self):
        self.create()
        st = self.st()
        st["pods"][cw.pod_name("coder7")].setdefault("status", {})["phase"] = "Failed"
        self.write_state(st)
        code, out, err = self.run_cli("stop", "coder7", "--reason", "retire")
        self.assertEqual(code, 0, err)
        stops = self.stops()
        self.assertEqual(len(stops), 1)
        self.assertNotIn("cause", stops[0])

    def test_s11_a_failed_pod_carries_pod_failed(self):
        self.create("coder7", "--hard", "1h")
        st = self.st()
        st["pods"][cw.pod_name("coder7")].setdefault("status", {})["phase"] = "Failed"
        self.write_state(st)
        self.clock.advance(minutes=61)
        self.reap()
        self.assertEqual([l["cause"] for l in self.stops()], ["pod_failed"])

    def test_s11_a_persons_stop_by_name_carries_no_cause(self):
        self.create()
        with mock.patch.object(cw.Launcher, "run_hook", lambda self, name: {"pushed": False, "wip_branch": None}):
            code, out, err = self.run_cli("stop", "coder7", "--reason", "idle")
        self.assertEqual(code, 0, err)
        stops = self.stops()
        self.assertEqual(len(stops), 1)
        self.assertNotIn("cause", stops[0])
        self.assertTrue(stops[0]["reason"].startswith("idle"))

    # ask #1480 S11: a reaper stop of a pod found gone or failed names that, not idle or hard
    def make_stop(self, hard, phase=None, gone=False, module=None):
        """One reaper stop of coder7 (hard or idle); the pod read says NotFound (gone) or the pod is in `phase`."""
        if hard:
            self.create("coder7", "--hard", "1h")
            self.set_activity("coder7", True, T0)
            self.clock.advance(minutes=61)
        else:
            self.create()
            self.set_activity("coder7", False, self.clock())
            self.clock.advance(minutes=12)
        if phase:
            st = self.st()
            st["pods"][cw.pod_name("coder7")].setdefault("status", {})["phase"] = phase
            self.write_state(st)
        mod = module or cw
        if gone:
            real_get = mod.Kube.get_pod
            target = cw.pod_name("coder7")
            patch = mock.patch.object(mod.Kube, "get_pod", lambda kube, name: None if name == target else real_get(kube, name))
            patch.start()
            self.addCleanup(patch.stop)
        mod.Launcher(mod.Kube(self.env), self.env, self.clock).reap()
        return [l["cause"] for l in self.stops()]

    def test_s11_a_reaper_idle_stop_of_a_gone_pod_carries_pod_gone(self):
        self.assertEqual(self.make_stop(False, gone=True), ["pod_gone"])

    def test_s11_a_reaper_hard_stop_of_a_gone_pod_carries_pod_gone(self):
        self.assertEqual(self.make_stop(True, gone=True), ["pod_gone"])

    def test_s11_a_reaper_idle_stop_of_a_failed_pod_carries_pod_failed(self):
        self.assertEqual(self.make_stop(False, phase="Failed"), ["pod_failed"])

    def test_s11_a_reaper_hard_stop_of_a_failed_pod_carries_pod_failed(self):
        self.assertEqual(self.make_stop(True, phase="Failed"), ["pod_failed"])

    def test_s11_a_reaper_stop_of_a_healthy_pod_still_carries_idle_and_hard(self):
        self.assertEqual(self.make_stop(False), ["idle"])

    def test_s11_a_reaper_hard_stop_of_a_healthy_pod_still_carries_hard(self):
        self.assertEqual(self.make_stop(True), ["hard"])

    def test_m11_a_gone_branch_that_keeps_the_idle_or_hard_cause_is_caught(self):
        mod = self.mutant('info["cause"] = "pod_gone"', "pass")
        self.assertEqual(self.make_stop(True, gone=True, module=mod), ["hard"])   # the real code writes pod_gone

    def test_m12_a_failed_branch_that_does_not_set_pod_failed_is_caught(self):
        mod = self.mutant('info["cause"] = "pod_failed"', "pass")
        self.assertEqual(self.make_stop(True, phase="Failed", module=mod), ["hard"])   # the real code writes pod_failed

    def test_s11_a_failed_end_hook_still_names_the_cause(self):
        self.create("coder7", "--hard", "1h")
        self.set_activity("coder7", True, T0)
        self.clock.advance(minutes=61)
        with mock.patch.object(cw.Launcher, "run_hook", lambda self, name: {"pushed": False, "wip_branch": None, "error": "no remote"}):
            self.reap()
        self.assertEqual([(l["cause"], l["ok"]) for l in self.stops()], [("hard", False)])

    def other_pod_alive(self):
        """A second worker whose pod stays in the snapshot (an empty snapshot is no evidence)."""
        self.create("coder8", "--hard", "5h")

    def vanish(self, minutes=30):
        self.create()
        self.other_pod_alive()
        self.lose_pod()
        self.clock.advance(minutes=minutes)

    def coder7_stops(self):
        return [l for l in self.stops() if l["worker"] == "coder7" and l.get("cause") == "pod_gone"]

    def test_s11_a_vanished_pod_is_written_once_over_three_passes(self):
        self.vanish()
        for _ in range(3):
            self.reap()
        stops = self.coder7_stops()
        self.assertEqual(len(stops), 1)
        self.assertEqual((stops[0]["cause"], stops[0]["ok"], stops[0]["worker"], stops[0]["runtime_id"], stops[0]["reason"]),
                         ("pod_gone", True, "coder7", "pod/" + cw.pod_name("coder7"), "pod gone; no stop was recorded"))

    def test_s15_a_create_two_minutes_old_writes_nothing(self):
        self.vanish(minutes=2)
        self.reap()
        self.assertEqual(self.coder7_stops(), [])

    def test_s15_a_pod_the_by_name_read_returns_writes_nothing(self):
        self.vanish()
        with mock.patch.object(cw.Kube, "get_pod", lambda kube, name: {"metadata": {"name": name}}):
            self.reap()
        self.assertEqual(self.coder7_stops(), [])

    def test_s15_a_by_name_read_that_fails_writes_nothing(self):
        self.vanish()

        def bad(kube, name):
            raise cw.CloudError("kubectl get: the server is unavailable")
        with mock.patch.object(cw.Kube, "get_pod", bad):
            self.reap()
        self.assertEqual(self.coder7_stops(), [])

    def test_s15_an_empty_snapshot_with_the_pod_existing_writes_nothing(self):
        self.create()
        self.clock.advance(minutes=30)
        with mock.patch.object(cw.Kube, "list_pods", lambda kube: []), \
                mock.patch.object(cw.Kube, "get_pod", lambda kube, name: {"metadata": {"name": name}}):
            self.reap()
        self.assertEqual(self.stops(), [])

    def test_s16_a_failed_stop_line_leaves_the_run_open(self):
        self.vanish()
        self.audit_append({"action": "stop", "worker": "coder7", "ok": False, "runtime_id": self.rid_of("coder7")})
        self.reap()
        self.assertEqual(len(self.coder7_stops()), 1)

    def test_s16_a_run_closed_by_a_stop_that_happened_writes_nothing(self):
        self.vanish()
        self.audit_append({"action": "stop", "worker": "coder7", "ok": True, "runtime_id": self.rid_of("coder7")})
        self.reap()
        self.assertEqual(len(self.coder7_stops()), 0)

    def rid_of(self, worker):
        return [l for l in self.audit_lines() if l["action"] == "create" and l["worker"] == worker][0]["runtime_id"]

    def audit_append(self, rec):
        rec = dict(rec, ts=cw.iso(self.clock()))
        with open(self.env["CW_CLOUD_AUDIT"], "a") as f:
            f.write(json.dumps(rec) + "\n")

    def test_s11_a_present_pod_writes_nothing(self):
        self.create()
        for _ in range(3):
            self.reap()
        self.assertEqual(self.stops(), [])

    def test_s11_a_worker_with_a_later_stop_line_writes_nothing_more(self):
        self.create()
        with mock.patch.object(cw.Launcher, "run_hook", lambda self, name: {"pushed": False, "wip_branch": None}):
            self.run_cli("stop", "coder7", "--reason", "done")
        self.assertIsNone(self.pod())
        self.reap()
        self.reap()
        stops = self.stops()
        self.assertEqual(len(stops), 1)
        self.assertNotIn("cause", stops[0])

    def test_s11_a_create_older_than_a_day_is_not_noticed(self):
        self.create()
        self.lose_pod()
        self.clock.advance(hours=25)
        self.reap()
        self.assertEqual(self.stops(), [])

    def test_s11_a_cluster_that_cannot_be_read_writes_nothing(self):
        self.create()
        self.lose_pod()
        self.patch_state(fail_cmd="get")
        with self.assertRaises(Exception):
            self.reap()
        self.assertEqual(self.stops(), [])

    def test_m7_a_mutant_that_writes_the_cause_for_two_of_four_stops_is_caught(self):
        import argparse
        mod = self.mutant('if getattr(a, "cause", None):   # only the reaper\'s own stop names a cause; a by-name stop carries none\n                    info["cause"] = "pod_gone"',
                          'if True:\n                    info["cause"] = "pod_gone"')
        self.create()
        self.lose_pod()
        mod.Launcher(mod.Kube(self.env), self.env, self.clock).stop(argparse.Namespace(worker="coder7", reason="retire", run_id=None))
        self.assertIn("cause", self.stops()[0])   # the mutant writes pod_gone for a by-name stop; the real code writes none

    def test_m8_a_vanished_pass_that_writes_on_every_pass_is_caught(self):
        mod = self.mutant('elif rec.get("action") in ("stop", "delete") and rec.get("ok") is True and rid in open_runs:', 'elif False:')
        self.vanish()
        for _ in range(3):
            mod.Launcher(mod.Kube(self.env), self.env, self.clock).reap()
        self.assertEqual(len(self.coder7_stops()), 3)   # the real code writes 1

    def stamping_mutant(self):
        return self.mutant("if not pods:\n            return", "if False:\n            return",
                           "if (now - created).total_seconds() <= VANISHED_MIN_AGE_SECONDS:", "if False:",
                           "gone = self.kube.get_pod(pod_name(worker)) is None", "gone = True")

    def test_m9_a_mutant_that_stamps_from_the_snapshot_alone_fails_four_s15_tests(self):
        mod = self.stamping_mutant()
        failed = 0
        young = self.vanish_with(mod, minutes=2)
        failed += bool(young)
        returned = self.vanish_with(mod, read=lambda kube, name: {"metadata": {"name": name}})
        failed += bool(returned)

        def bad(kube, name):
            raise cw.CloudError("kubectl get: the server is unavailable")
        failed += bool(self.vanish_with(mod, read=bad))
        self.create("coder9")
        self.clock.advance(minutes=30)
        with mock.patch.object(cw.Kube, "list_pods", lambda kube: []), \
                mock.patch.object(mod.Kube, "list_pods", lambda kube: []):
            mod.Launcher(mod.Kube(self.env), self.env, self.clock).reap()
        failed += any(l["worker"] == "coder9" for l in self.stops())
        self.assertEqual(failed, 4)   # the real code writes nothing in all four

    def vanish_with(self, mod, minutes=30, read=None):
        """Lines the mutant writes for a fresh worker whose pod is gone (the real tests expect none when read is given or minutes is small)."""
        self.n_vanished = getattr(self, "n_vanished", 0) + 1
        name = "wolf%s" % "abcdefgh"[self.n_vanished]
        self.create(name)
        st = self.st()
        st["pods"].pop(cw.pod_name(name))
        self.write_state(st)
        self.clock.advance(minutes=minutes)
        if read:
            with mock.patch.object(mod.Kube, "get_pod", read):
                mod.Launcher(mod.Kube(self.env), self.env, self.clock).reap()
        else:
            mod.Launcher(mod.Kube(self.env), self.env, self.clock).reap()
        return [l for l in self.stops() if l["worker"] == name]

    def test_m10_a_mutant_where_any_stop_closes_the_run_is_caught(self):
        mod = self.mutant('and rec.get("ok") is True and rid in open_runs:', 'and rid in open_runs:')
        self.vanish()
        self.audit_append({"action": "stop", "worker": "coder7", "ok": False, "runtime_id": self.rid_of("coder7")})
        mod.Launcher(mod.Kube(self.env), self.env, self.clock).reap()
        self.assertEqual(len([l for l in self.coder7_stops()]), 0)   # the real code writes 1

    def test_u4_the_vanished_code_reads_the_pod_by_name_before_the_write_and_checks_ok(self):
        with open(cw.__file__) as f:
            text = f.read()
        body = text[text.index("def _note_vanished_pods"):text.index("def renew_git_tokens")]
        self.assertIn("VANISHED_MIN_AGE_SECONDS = 600", text)
        self.assertLess(body.index("self.kube.get_pod(pod_name(worker))"), body.index('self.audit(worker, rid, "stop"'))
        self.assertIn('rec.get("ok") is True', body)


if __name__ == "__main__":
    unittest.main()
