"""Ask #2446: one PodDisruptionBudget (minAvailable 1) per worker pod, made before the pod, removed with it on every ending path.

Scenarios S2 (3 budgets, own-pod selectors, before the pod), S3 (4 endings leave 0 own budgets, 1 other), S4 (no extra field, no secret shape),
S8 (a refused budget create ends the launch with 0 pods and 0 Secrets). Offline: the fake kubectl. The mutants are scratch copies of the real
source, each mutation asserted applied; a control run of the copy without the mutation comes first and must pass.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

A, B = "aaaaaaaa11111111", "bbbbbbbb22222222"
MODULE_ENV = "CW_TEST_PDB_MODULE"
OK_LINE = "PDB-PROBE-OK"


def matches(sel, pod):
    labels = pod["metadata"].get("labels", {})
    return all(labels.get(k) == v for k, v in sel.items())


class Pdb(base.Base):
    def setUp(self):
        super().setUp()
        self.stub_hook = lambda: self.patch_state(hook_result={"pushed": True, "wip_branch": "w/wip"})
        self.stub_hook()

    def pdbs(self):
        return self.st().get("pdbs", {})

    # S2
    def test_s2_three_launches_make_three_budgets_each_matching_only_its_pod_and_made_first(self):
        for w in ("coder1", "coder2", "coder3"):
            self.create(w)
        st = self.st()
        self.assertEqual(len(st["pdbs"]), 3)
        for w in ("coder1", "coder2", "coder3"):
            name = cw.pod_name(w)
            b = st["pdbs"][name + "-pdb"]
            self.assertEqual(b["spec"]["minAvailable"], 1)
            hit = [n for n, p in st["pods"].items() if matches(b["spec"]["selector"]["matchLabels"], p)]
            self.assertEqual(hit, [name])
            self.assertLess(st["order"].index("pdb:" + name + "-pdb"), st["order"].index("pod:" + name))

    # S4
    def test_s4_the_budget_holds_only_name_labels_selector_and_min_available(self):
        obj = cw.render_pdb(cw.pod_name("coder1"), "coder1", A)
        self.assertEqual(set(obj), {"apiVersion", "kind", "metadata", "spec"})
        self.assertEqual(set(obj["metadata"]), {"name", "labels"})
        self.assertEqual(set(obj["spec"]), {"minAvailable", "selector"})
        text = json.dumps(obj)
        self.assertEqual(cw.scan_credentials(obj), [])
        self.assertNotIn("secret", text.lower())
        self.assertNotIn("annotations", text)

    # S3
    def other(self):
        self.create("other", "--run-id", B)

    def assert_only_other_left(self, w):
        names = set(self.pdbs())
        self.assertEqual(names, {cw.pod_name("other") + "-pdb"}, w)

    def test_s3_stop_removes_own_budget_only(self):
        self.create("coder1", "--run-id", A)
        self.other()
        code, _, err = self.run_cli("stop", "coder1", "--no-record", "--run-id", A)
        self.assertEqual(code, 0, err)
        self.assert_only_other_left("stop")

    def test_s3_stop_without_a_run_id_removes_own_budget_only(self):
        self.create("coder1")
        self.other()
        code, _, err = self.run_cli("stop", "coder1", "--no-record")
        self.assertEqual(code, 0, err)
        self.assert_only_other_left("stop by name")

    def test_s3_a_stop_with_another_run_id_leaves_the_budget(self):
        self.create("coder1", "--run-id", A)
        code, _, _ = self.run_cli("stop", "coder1", "--no-record", "--run-id", B)
        self.assertNotEqual(code, 0)
        self.assertIn(cw.pod_name("coder1") + "-pdb", self.pdbs())

    def test_s3_the_reaper_removes_the_budget_with_the_pod(self):
        self.create("coder1", "--hard", "1h")
        self.other()
        self.patch_state(hook_env={"CW_WORKDIR": base.make_repo(self.tmp.name, "r", False)[0], "CW_WORKER": "x"})
        self.set_activity("coder1", True, base.T0)
        self.set_activity("other", True, base.T0)
        self.clock.advance(minutes=61)
        st = self.st()
        st["pods"][cw.pod_name("other")]["metadata"]["annotations"]["cw/hard-deadline"] = cw.iso(self.clock() + base.dt.timedelta(hours=5))
        self.write_state(st)
        rows = cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()
        self.assertEqual([r[0] for r in rows], ["coder1"])
        self.assert_only_other_left("reaper")

    def test_s3_a_launch_that_fails_after_the_budget_removes_it(self):
        self.other()
        self.patch_state(never_ready=True)
        code, _, _ = self.run_cli("create", "coder1", "--repo", "a/b", "--branch", "m", "--wait", "10", "--run-id", A)
        self.assertEqual(code, 4)
        self.assert_only_other_left("failed launch")

    def test_s3_the_reaper_pass_removes_a_budget_whose_pod_is_gone_and_old(self):
        self.create("coder1")
        self.create("other")
        st = self.st()
        del st["pods"][cw.pod_name("coder1")]
        self.write_state(st)
        L = cw.Launcher(cw.Kube(self.env), self.env, self.clock)
        pods = list(self.st()["pods"].values())
        st = self.st(); st["pdbs"][cw.pod_name("coder1") + "-pdb"]["metadata"]["creationTimestamp"] = cw.iso(self.clock()); self.write_state(st)
        self.assertEqual(L.remove_orphan_pdbs(pods, self.clock()), [])   # too young: its pod may be about to be made
        self.clock.advance(hours=2)
        self.assertEqual(L.remove_orphan_pdbs(pods, self.clock()), [cw.pod_name("coder1") + "-pdb"])

    # S8
    def refused(self, how):
        self.patch_state(fail_pdb=how)
        code, _, err = self.run_cli("create", "coder1", "--repo", "a/b", "--branch", "m")
        self.assertNotEqual(code, 0)
        st = self.st()
        self.assertEqual((st["pods"], st.get("secrets", {})), ({}, {}))
        self.assertEqual(len([l for l in err.splitlines() if l.strip()]), 1, err)
        return err

    def test_s8_a_forbidden_budget_create_ends_the_launch_with_no_pod_and_no_secret(self):
        self.assertIn("forbidden", self.refused("forbidden").lower())

    def test_s8_an_unreadable_answer_ends_the_launch_with_no_pod_and_no_secret(self):
        self.assertIn("odd", self.refused("odd"))


class Mutants(base.Base):
    """The probe runs the scenario against a scratch copy of the source. Control: the copy as it is passes. Each mutant fails with its text."""
    PROBE = r'''
import os, sys, json, io, importlib.util
sys.path.insert(0, %(here)r)
import test_cloud_worker as base
spec = importlib.util.spec_from_file_location("cloud_worker", os.environ["CW_TEST_PDB_MODULE"])
mod = importlib.util.module_from_spec(spec); sys.modules["cloud_worker"] = mod; spec.loader.exec_module(mod)
base.cw = mod
import test_pdb
test_pdb.cw = mod
suite = unittest.TestSuite() if False else None
import unittest
t = unittest.defaultTestLoader.loadTestsFromTestCase(test_pdb.Pdb)
r = unittest.TextTestRunner(stream=io.StringIO()).run(t)
if r.wasSuccessful():
    print("PDB-PROBE-OK")
else:
    for t_, _ in r.failures + r.errors:
        print("PDB-PROBE-FAIL: " + t_.id().rsplit(".", 1)[1])
    sys.exit(1)
'''

    def probe(self, old=None, new=None):
        src = open(os.path.join(os.path.dirname(HERE), "cloud_worker.py")).read()
        if old is not None:
            self.assertEqual(src.count(old), 1, "the mutation did not apply")
            src = src.replace(old, new)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cloud_worker.py")
            with open(path, "w") as f:
                f.write(src)
            code = "import unittest\n" + self.PROBE % {"here": HERE}
            p = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, **{MODULE_ENV: path}), capture_output=True, text=True, timeout=90, cwd=HERE)
        return p.returncode, p.stdout

    def test_control_the_unmutated_copy_passes(self):
        code, out = self.probe()
        self.assertEqual((code, out.strip()), (0, OK_LINE), out)

    def test_mutant_selector_on_the_shared_label_fails_s2(self):
        self.assertEqual(self.probe()[0], 0)   # control first
        code, out = self.probe('"selector": {"matchLabels": {"app": LABEL, "cw/worker": worker}}}}\n', '"selector": {"matchLabels": {"app": LABEL}}}}\n')
        self.assertEqual(code, 1)
        self.assertRegex(out, r"PDB-PROBE-FAIL: test_s2_three_launches")

    def test_mutant_budget_not_deleted_by_stop_fails_s3(self):
        self.assertEqual(self.probe()[0], 0)
        code, out = self.probe('''("pdb", "poddisruptionbudget/%s-pdb" % name, self.kube.delete_pdb),
                                ("secret", "secret/%s-pw" % name, self.kube.delete_secret),''', '''("secret", "secret/%s-pw" % name, self.kube.delete_secret),''')
        self.assertEqual(code, 1)
        self.assertRegex(out, r"PDB-PROBE-FAIL: test_s3_stop_removes_own_budget_only")


if __name__ == "__main__":
    unittest.main()
