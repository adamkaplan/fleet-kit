"""A pod replaced by the next run's pod of the same name while a long step runs (the end hook, minutes): nothing the old run's
`stop`, `delete` or `extend` does afterwards lands on the NEW pod. A node loss, an eviction, a preemption or a manual delete
removes the pod as well as a stop does. Offline: the fake kubectl swaps the pod during the hook. One test per row of the
README's table "Acting on an object later, by name" that acts on a pod after a gap."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

OLD, NEW = "aaaaaaaa11111111", "bbbbbbbb22222222"


class Replaced(base.Base):
    def setUp(self):
        super().setUp()
        self.name = cw.pod_name("coder7")

    def plant(self, run_id=OLD, uid=None):
        st = self.st()
        meta = {"name": self.name, "annotations": {"cw/started": cw.iso(self.clock()), "cw/shape": "S"}}
        if run_id:
            meta["annotations"]["cw/run-id"] = run_id
        if uid:
            meta["uid"] = uid
        st.setdefault("pods", {})[self.name] = {"metadata": meta, "status": {"phase": "Running"}}
        self.write_state(st)

    def pod(self):
        return self.st().get("pods", {}).get(self.name)

    def stop(self, *extra):
        return self.run_cli("stop", "coder7", "--no-record", *extra)

    def audit_stop(self):
        return [l for l in self.audit_lines() if l["action"] == "stop"]

    def test_a_good_hook_does_not_mark_the_new_pod_hook_ran_and_does_not_delete_it(self):
        self.plant()
        self.patch_state(swap_pod_during_hook=NEW)
        code, out, err = self.stop("--run-id", OLD)
        self.assertNotEqual(code, 0)
        pod = self.pod()
        self.assertIsNotNone(pod)
        self.assertNotIn("cw/hook-ran", pod["metadata"]["annotations"])   # so a plain `delete` of it still refuses
        self.assertEqual(len(self.audit_stop()), 1)
        self.assertFalse(self.audit_stop()[0]["ok"])
        self.assertIn("changed since it was read", self.audit_stop()[0]["reason"])
        self.assertEqual(self.run_cli("delete", "coder7")[0], cw.EXIT_REFUSED)   # the guard holds for the new run

    def test_a_failed_hook_does_not_mark_the_new_pod_kept(self):
        self.plant()
        self.patch_state(swap_pod_during_hook=NEW, hook_fails_after_swap=True,
                         hook_result_after_swap={"pushed": False, "error": "push refused"})
        code, out, err = self.stop("--run-id", OLD)
        self.assertNotEqual(code, 0)
        self.assertNotIn("cw/kept", self.pod()["metadata"]["annotations"])   # so the Failed/Succeeded cleanup can still remove it
        self.assertIn("not marked kept", err + str(self.audit_stop()))

    def test_a_stop_without_a_run_id_does_not_delete_the_new_pod_or_its_secrets(self):
        self.plant(run_id=None)
        self.patch_state(swap_pod_during_hook=NEW, swap_secrets=True)
        code, out, err = self.stop()
        self.assertNotEqual(code, 0)
        self.assertIsNotNone(self.pod())
        self.assertEqual(sorted(self.st().get("secrets", {})), [self.name + "-pw", self.name + "-run"])
        self.assertEqual(len(self.audit_stop()), 1)

    def test_a_stop_with_a_run_id_leaves_the_new_pod_whole(self):
        self.plant()
        self.patch_state(swap_pod_during_hook=NEW, swap_secrets=True)
        self.stop("--run-id", OLD)
        self.assertIsNotNone(self.pod())
        self.assertEqual(len(self.st().get("secrets", {})), 2)

    def test_the_same_pod_is_still_marked_and_removed(self):   # the guard does not break the plain path
        self.plant(uid="uid-x")
        self.patch_state(hook_result={"pushed": True, "wip_branch": "w/wip"})
        code, out, err = self.stop("--run-id", OLD)
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.pod())

    def test_delete_does_not_remove_a_pod_that_changed_between_its_read_and_its_delete(self):
        self.plant(uid="uid-old")
        st = self.st(); st["pods"][self.name]["metadata"]["annotations"]["cw/hook-ran"] = "true"; st["swap_after_get"] = NEW
        self.write_state(st)
        code, out, err = self.run_cli("delete", "coder7")
        self.assertEqual(code, cw.EXIT_REFUSED, err)
        self.assertIsNotNone(self.pod())

    def test_extend_does_not_extend_a_pod_that_changed_between_its_read_and_its_annotate(self):
        self.plant(uid="uid-old")
        st = self.st(); a = st["pods"][self.name]["metadata"]["annotations"]
        a.update({"cw/hard-deadline": "2026-01-01T01:00:00Z", "cw/max-hard": "2026-01-01T09:00:00Z", "cw/idle-seconds": "600"})
        st["swap_after_get"] = NEW; self.write_state(st)
        code, out, err = self.run_cli("extend", "coder7", "--hard", "+1h")
        self.assertEqual(code, cw.EXIT_REFUSED, err)
        self.assertEqual(self.pod()["metadata"]["annotations"].get("cw/idle-seconds"), None)   # the new pod carries nothing of the extension

    def kube(self):
        return cw.Kube(self.env)

    def test_a_bound_delete_and_a_bound_annotate_are_refused_by_the_server_on_another_uid(self):
        self.plant(uid="uid-new")
        kube = self.kube()
        with self.assertRaises(cw.PodChanged):
            kube.delete_pod(self.name, "uid-old")
        with self.assertRaises(cw.PodChanged):
            kube.annotate(self.name, "uid-old", hard_deadline="x")
        with self.assertRaises(cw.PodChanged):
            kube.delete(self.name, "uid-old")
        self.assertIsNotNone(self.pod())
        self.assertNotIn("cw/hard-deadline", self.pod()["metadata"]["annotations"])
        kube.annotate(self.name, "uid-new", hard_deadline="x")
        kube.delete_pod(self.name, "uid-new")
        self.assertIsNone(self.pod())


if __name__ == "__main__":
    unittest.main()
