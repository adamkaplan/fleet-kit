"""A kept pod that ended Failed or Succeeded is deleted with its Secrets once its grace has passed (ask #2343).

Offline: the fake kubectl and a fake clock of the other launcher tests. S2 (25 h deleted, 23 h kept), S3 (the others untouched), S4 (the status), S5 (the mutant).
"""
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_failed_pod_record as fpr  # noqa: E402
import test_cloud_status_summary as ss  # noqa: E402
cw = fpr.cw
ANCHOR = "a kept pod outlived its grace"


class Scen(fpr.Fixture):
    def at(self, worker, **past):
        """The clock at the pod's hard deadline plus `past` (hours=, minutes=)."""
        hard = cw.parse_iso(self.pod(worker)["metadata"]["annotations"]["cw/hard-deadline"])
        self.clock.t = hard
        self.clock.advance(**past)

    def launcher(self):
        la = cw.Launcher(cw.Kube(self.env), self.env, self.clock)
        la.branch_exists = lambda token, repo, branch: branch.endswith("/wip")   # the remote: the wip branch exists
        return la

    def reap(self):
        with mock.patch.object(cw, "run_git_token_cmd", lambda *a, **k: "synthetic-token"), mock.patch.object(cw, "check_repo_allowed", lambda *a: None):
            return self.launcher().reap()

    def expired(self, worker):
        return [x for x in self.audit_lines() if x["action"] == "kept-expired" and x["worker"] == worker]

    def secrets(self, worker):
        return sorted(k for k in self.st().get("secrets", {}) if k.startswith(cw.pod_name(worker)))

    def scenario(self, worker, phase="Failed"):
        """A kept pod of `phase`: at its deadline it is marked once; at 23 h it stays; at 25 h it and its Secrets go and ONE audit line says so."""
        self.fail(worker, {"phase": phase}, kept=True)
        st = self.st()
        for suffix in ("-pw", "-run"):
            st.setdefault("secrets", {})[cw.pod_name(worker) + suffix] = 1
        self.write_state(st)
        self.at(worker, hours=1)
        self.reap()
        self.assertEqual(self.pod(worker) is not None, True, "KEPT_AT_1H")
        self.at(worker, hours=23)
        self.reap()
        self.assertEqual(self.pod(worker) is not None, True, "KEPT_AT_23H")
        self.assertEqual(len(self.secrets(worker)), 2, "SECRETS_AT_23H")
        self.assertEqual(self.expired(worker), [], "AUDIT_AT_23H")
        self.at(worker, hours=25)
        out = self.reap()
        self.assertEqual(self.pod(worker), None, ANCHOR + " (the pod)")
        self.assertEqual(self.secrets(worker), [], ANCHOR + " (the Secrets)")
        lines = self.expired(worker)
        self.assertEqual(len(lines), 1, ANCHOR + " (the audit line)")
        self.assertEqual(("%s/wip exists: yes" % worker) in lines[0]["reason"], True, "WIP_STATE")
        self.assertEqual("not saved" in lines[0]["reason"], True, "EXEC_SAID")   # an ended pod cannot be exec'd: the line says so
        self.assertEqual((worker, "kept pod expired", 0) in out, True, "REPORT")
        self.reap()
        self.assertEqual(len(self.expired(worker)), 1, "AUDIT_ONCE")


class Expiry(Scen):
    def test_a_kept_failed_pod_is_deleted_after_its_grace(self):
        self.scenario("kfail")

    def test_a_kept_succeeded_pod_is_deleted_after_its_grace(self):
        self.scenario("kdone", "Succeeded")

    def test_the_audit_says_when_the_wip_branch_is_not_there_or_unknown(self):
        self.fail("nowip", {"phase": "Failed"}, kept=True)
        self.at("nowip", hours=25)
        la = self.launcher()
        la.branch_exists = lambda *a: False
        with mock.patch.object(cw, "run_git_token_cmd", lambda *a, **k: "synthetic-token"), mock.patch.object(cw, "check_repo_allowed", lambda *a: None):
            la.reap()
        self.assertIn("nowip/wip exists: no", self.expired("nowip")[0]["reason"])
        self.fail("nomint", {"phase": "Failed"}, kept=True)
        self.at("nomint", hours=25)
        with mock.patch.object(cw, "run_git_token_cmd", side_effect=cw.CloudError("mint refused")), mock.patch.object(cw, "check_repo_allowed", lambda *a: None):
            self.launcher().reap()
        self.assertIn("nomint/wip exists: unknown", self.expired("nomint")[0]["reason"])
        self.assertIsNone(self.pod("nomint"))

    def test_the_pod_is_deleted_by_its_uid_and_a_pod_that_changed_is_left(self):
        self.fail("swapd", {"phase": "Failed"}, kept=True)
        self.at("swapd", hours=25)
        with mock.patch.object(cw.Kube, "delete", side_effect=cw.PodChanged("changed")):
            out = self.reap()
        self.assertIsNotNone(self.pod("swapd"))
        self.assertEqual(self.expired("swapd"), [])
        self.assertTrue([r for r in out if r[0] == "swapd" and r[2] != 0], out)

    def test_an_unkept_failed_pod_11_minutes_past_is_deleted(self):
        self.fail("plain", {"phase": "Failed"})
        self.at("plain", minutes=11)
        self.reap()
        self.assertIsNone(self.pod("plain"))

    def test_an_unkept_pod_that_survived_its_stop_is_deleted_ten_minutes_past(self):
        self.create("stuck")
        self.at("stuck", minutes=11)
        with mock.patch.object(cw.Launcher, "_run_stops", lambda self_, jobs: [(None, cw.CloudError("stop failed"), None)] * len(jobs)):
            out = self.reap()
        self.assertIsNone(self.pod("stuck"))
        self.assertEqual([x["action"] for x in self.audit_lines() if x["worker"] == "stuck" and x["action"] == "expired"], ["expired"])
        self.assertTrue([r for r in out if r[1] == "hard limit"], out)

    def test_an_unkept_pod_before_the_ten_minutes_is_left_when_its_stop_failed(self):
        self.create("early")
        self.at("early", minutes=9)
        with mock.patch.object(cw.Launcher, "_run_stops", lambda self_, jobs: [(None, cw.CloudError("stop failed"), None)] * len(jobs)):
            self.reap()
        self.assertIsNotNone(self.pod("early"))

    def test_a_running_pod_before_its_deadline_is_untouched(self):
        self.create("alive")
        self.at("alive", hours=-1)
        self.set_activity("alive", True, self.clock())
        self.assertEqual(self.reap(), [])
        self.assertIsNotNone(self.pod("alive"))

    def test_a_kept_running_pod_is_untouched_25_hours_past(self):
        self.create("krun")
        st = self.st()
        ann = st["pods"][cw.pod_name("krun")]["metadata"]["annotations"]
        ann["cw/kept"] = "true"
        ann["cw/" + cw.KEPT_RUNNING_MARK.replace("_", "-")] = "true"
        self.write_state(st)
        self.at("krun", hours=25)
        self.reap()
        self.assertIsNotNone(self.pod("krun"))
        self.assertEqual(self.expired("krun"), [])

    def test_the_grace_is_one_day(self):
        self.assertEqual(cw.KEPT_GRACE_SECONDS, 24 * 3600)
        import cloud_ended_pods
        self.assertEqual(cloud_ended_pods.GRACE_SECONDS, cw.KEPT_GRACE_SECONDS)


class Mutant(Scen):
    def test_a_mutant_without_the_expiry_fails_with_the_anchored_text(self):
        applied = []

        def blind(self_, pod, now):
            applied.append(1)
            return False
        self.scenario("ctrl")   # control first: the unmutated code passes
        self.assertFalse(applied)
        with mock.patch.object(cw.Launcher, "_expire", blind):
            with self.assertRaises(AssertionError) as cm:
                self.scenario("mut")
        self.assertTrue(applied, "the mutation was not applied")
        self.assertIn(ANCHOR, str(cm.exception))


class Status(ss.Base):
    def ended(self, name, phase, hours, kept):
        import datetime
        when = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pod = self.pod("cw-" + name, name, phase=phase, hard=when)
        if kept:
            pod["metadata"]["annotations"]["cw/kept"] = "true"
        return pod

    def test_the_status_counts_the_ended_pods_the_oldest_and_names_the_one_past_its_grace(self):
        self.put_pods(self.ended("young", "Failed", 2, True), self.ended("old", "Succeeded", 30, True), self.pod("cw-run", "run", hard=ss.ALIVE))
        self.record("run", hard=ss.ALIVE)
        lines, _ = self.status()
        text = "\n".join(lines)
        self.assertIn("cloud: ended pods still standing 2, oldest 1d6h", text)
        named = [l for l in lines if "ended pod cw-" in l]
        self.assertEqual(len(named), 1, text)
        self.assertIn("cw-old", named[0])
        self.assertNotIn("cw-young", named[0])
        self.assertTrue(all(not any(ord(c) < 32 for c in l) for l in lines))

    def test_no_ended_pod_adds_no_line(self):
        self.put_pods(self.pod("cw-run", "run", hard=ss.ALIVE))
        lines, _ = self.status()
        self.assertFalse([l for l in lines if "ended pod" in l], lines)


if __name__ == "__main__":
    unittest.main()
