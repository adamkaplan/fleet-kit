"""The StatefulSet lookup of a gone pod's stop/delete, across the RBAC states. Matrix: callers x pod states x lookup states; every cell
asserts what is left, the answer and the exit code. A FORBIDDEN lookup (the Role not yet applied) is 'no StatefulSet can exist': the
original path runs (and one stderr line says so); any OTHER lookup error stays 'REMAINS unknown'. Offline: the fake kubectl."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

A, B = "aaaaaaaa11111111", "bbbbbbbb22222222"
NOTE = "statefulset lookup forbidden: not checked"
CALLERS = {"stop-match": ("stop", "--run-id", A), "stop-other": ("stop", "--run-id", B), "stop-none": ("stop",),
           "delete-force": ("delete", "--force"), "delete": ("delete",)}
LOOKUPS = {"allowed": None, "notfound": None, "forbidden": "forbidden", "500": "500"}
ALL = ["coder7", "coder7-pw", "coder7-run", "statefulset"]


class Matrix(base.Base):
    def setUp(self):
        super().setUp()
        self.name = cw.pod_name("coder7")

    def plant(self, state):
        st = self.st()
        if state == "present":
            st["pods"] = {self.name: {"metadata": {"name": self.name, "annotations": {"cw/run-id": A, "cw/hook-ran": "true", "cw/started": cw.iso(self.clock()), "cw/shape": "S"}},
                                      "status": {"phase": "Running"}}}
            st["hook_result"] = {"pushed": False, "wip_branch": None}
        if state in ("present", "plain-secrets", "statefulset"):
            for n in (self.name + "-pw", self.name + "-run"):
                st.setdefault("secrets", {})[n] = 1
                st.setdefault("secret_labels", {})[n] = {"cw/run-id": A}
        if state == "statefulset":
            st["statefulsets"] = {self.name: {"spec": {"template": {"metadata": {"annotations": {"cw/run-id": A}}}}}}
        self.write_state(st)

    def left(self):
        st = self.st()
        return sorted(list(st.get("pods", {})) + list(st.get("secrets", {})) + (["statefulset"] if st.get("statefulsets") else []))

    def expect(self, caller, state, lookup):
        """(exit code, what is left, deleted-answer or None) of one cell."""
        everything = sorted(([self.name] if state == "present" else []) + [self.name + "-pw", self.name + "-run"] + (["statefulset"] if state == "statefulset" else []))
        secrets = [self.name + "-pw", self.name + "-run"]
        stop = caller.startswith("stop")
        if state == "present":   # the pod is read: the lookup is never made
            return (6, everything, None) if caller == "stop-other" else (0, [], None if stop else True)
        if state == "none":
            secrets, everything = [], []
        if state == "statefulset" and lookup in ("allowed", "notfound"):   # the set is really there: seen, and removed first
            if caller == "stop-other":
                return 0, everything, None
            return 0, [], None if stop else True
        if lookup == "500":
            return 3, everything, None
        # NOT FOUND, or FORBIDDEN (no set can exist / is seen): the original path. A set that is there but unseen stays.
        kept_set = ["statefulset"] if state == "statefulset" else []
        if caller == "stop-match":
            return 0, kept_set, None
        return 0, sorted(secrets + kept_set), None if stop else True

    def cell(self, caller, state, lookup):
        self.write_state({"pods": {}})
        self.plant(state)
        self.patch_state(fail_get_statefulset=LOOKUPS[lookup])
        if os.path.exists(self.env["FAKE_KUBE_CALLS"]):
            os.remove(self.env["FAKE_KUBE_CALLS"])
        code, out, err = self.run_cli(*(CALLERS[caller][:1] + ("coder7",) + (("--no-record",) if caller.startswith("stop") else ()) + CALLERS[caller][1:]))
        want_code, want_left, want_deleted = self.expect(caller, state, lookup)
        where = "%s / %s / %s" % (caller, state, lookup)
        self.assertEqual(code, want_code, where + ": " + err)
        self.assertEqual(self.left(), want_left, where)
        if want_deleted:
            self.assertIs(json.loads(out)["deleted"], True, where)
        else:
            self.assertNotIn('"deleted": true', out, where)
        if code == 3:
            self.assertIn("REMAINS unknown", err, where)
        if lookup == "forbidden" and state != "present":
            self.assertEqual(err.count(NOTE), 1, where)
        else:
            self.assertNotIn(NOTE, err, where)
        if state == "none" and code == 0:
            self.assertFalse([c for c in self.kube_calls() if "delete" in c], where + ": nothing exists = zero delete calls")

    def kube_calls(self):
        p = self.env["FAKE_KUBE_CALLS"]
        return [json.loads(x) for x in open(p)] if os.path.exists(p) else []

    def test_matrix(self):
        for caller in CALLERS:
            for state in ("present", "statefulset", "none", "plain-secrets"):
                for lookup in LOOKUPS:
                    if state == "statefulset" and lookup == "notfound":
                        continue   # a set that is there cannot be NotFound
                    with self.subTest(cell="%s / %s / %s" % (caller, state, lookup)):
                        self.cell(caller, state, lookup)


class KubeLevel(base.Base):
    """The real Kube.get_workload against the fake kubectl: NotFound is None, forbidden and 500 are errors the launcher tells apart."""
    def test_get_workload_error_kinds(self):
        kube = cw.Kube(self.env)
        self.assertIsNone(kube.get_workload("coder7-0"))
        for mode, word in (("forbidden", "forbidden"), ("500", "InternalError")):
            self.patch_state(fail_get_statefulset=mode)
            with self.assertRaises(cw.CloudError) as c:
                kube.get_workload("coder7-0")
            self.assertIn(word, str(c.exception))
        self.patch_state(fail_get_statefulset=False, statefulsets={"coder7": {"kind": "StatefulSet"}})
        self.assertEqual(kube.get_workload("coder7"), {"kind": "StatefulSet"})
        kube.delete_workload("coder7")
        self.assertIsNone(kube.get_workload("coder7"))


if __name__ == "__main__":
    unittest.main()
