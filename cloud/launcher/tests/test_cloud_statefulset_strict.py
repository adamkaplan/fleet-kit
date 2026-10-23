"""Strict match of the StatefulSet lookup errors: only kubectl's own 'Error from server (Forbidden):' / '(NotFound):' status prefix counts.
Any other error (a 5xx whose text says forbidden / not found, in any case or in a resource name) is 'REMAINS unknown': exit 3, Secrets left."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_statefulset_lookup as m  # noqa: E402
cw = m.cw
ODD = ["webhook", "timeout", "upper", "resname", "etcdnotfound"]


class Strict(m.Matrix):
    def test_odd_errors_remain_unknown(self):
        for caller in m.CALLERS:
            for state in ("none", "plain-secrets", "statefulset"):
                for odd in ODD:
                    with self.subTest(cell="%s / %s / %s" % (caller, state, odd)):
                        self.write_state({"pods": {}})
                        self.plant(state)
                        self.patch_state(fail_get_statefulset=odd)
                        code, out, err = self.run_cli(*(m.CALLERS[caller][:1] + ("coder7",) + (("--no-record",) if caller.startswith("stop") else ()) + m.CALLERS[caller][1:]))
                        self.assertEqual(code, 3, err)
                        self.assertIn("REMAINS unknown", err)
                        self.assertNotIn(m.NOTE, err)
                        self.assertNotIn('"deleted": true', out)
                        want = sorted(([self.name + "-pw", self.name + "-run"] if state != "none" else []) + (["statefulset"] if state == "statefulset" else []))
                        self.assertEqual(self.left(), want)

    def test_genuine_forms_unchanged(self):
        self.write_state({"pods": {}})
        self.plant("plain-secrets")
        for mode, note in (("forbidden", True), (False, False)):
            self.patch_state(fail_get_statefulset=mode)
            code, out, err = self.run_cli("delete", "coder7", "--force")
            self.assertEqual(code, 0, err)
            self.assertEqual(m.NOTE in err, note)
            self.plant("plain-secrets")

    def test_kube_level_not_forbidden(self):
        kube = cw.Kube(self.env)
        for odd in ODD:
            self.patch_state(fail_get_statefulset=odd)
            with self.assertRaises(cw.CloudError) as c:
                kube.get_workload("coder7-0")
            self.assertFalse(str(c.exception).startswith("kubectl get: Error from server (Forbidden):"))


if __name__ == "__main__":
    unittest.main()
