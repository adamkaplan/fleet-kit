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
        workers = ("coder1", "coder2", "coder3")
        for w in workers:
            self.create(w)
        st = self.st()
        self.assertEqual(set(st["pdbs"]), {cw.pod_name(w) + "-pdb" for w in workers})
        for w in workers:
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

    def test_s3_a_stop_of_a_gone_pod_removes_its_budget_and_not_the_others(self):
        for nonce in (A, None):
            self.create("coder1", *(["--run-id", nonce] if nonce else []))
            self.other()
            st = self.st()
            del st["pods"][cw.pod_name("coder1")]   # the pod is gone: the retire path
            self.write_state(st)
            code, _, err = self.run_cli("stop", "coder1", "--no-record", *(["--run-id", nonce] if nonce else []))
            self.assertEqual(code, 0, err)
            self.assert_only_other_left("gone pod")
            self.run_cli("stop", "other", "--no-record", "--run-id", B)

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
        self.assertEqual(set(self.pdbs()), {cw.pod_name("other") + "-pdb"})

    def test_an_unreadable_budget_list_deletes_nothing(self):
        self.create("coder1")
        L = cw.Launcher(cw.Kube(self.env), self.env, self.clock)
        L.kube.list_pdbs = lambda: (_ for _ in ()).throw(cw.CloudError("garbled"))
        self.assertEqual(L.remove_orphan_pdbs([], self.clock()), [])
        self.assertEqual(set(self.pdbs()), {cw.pod_name("coder1") + "-pdb"})

    def test_the_orphan_pass_keeps_the_budget_of_every_listed_pod_however_old(self):
        self.create("coder1")
        self.create("other")
        L = cw.Launcher(cw.Kube(self.env), self.env, self.clock)
        self.clock.advance(hours=2)
        every = list(self.st()["pods"].values())
        self.assertEqual(L.remove_orphan_pdbs(every, self.clock()), [])
        self.assertEqual(L.remove_orphan_pdbs(every[:1], self.clock()), [cw.pod_name("other") + "-pdb"])   # a truncated list would delete a live pod's
        src = open(os.path.join(os.path.dirname(HERE), "cloud_worker.py")).read()
        self.assertEqual(src.count("self.remove_orphan_pdbs(pods, now)"), 1)   # called with the pass's own pod list

    def test_the_selector_keys_are_the_pods_own_and_no_two_workers_share_a_value_pair(self):
        self.create("coder1")
        self.create("coder2")
        pods = self.st()["pods"]
        keys = [(p["metadata"]["labels"]["app"], p["metadata"]["labels"]["cw/worker"]) for p in pods.values()]
        self.assertEqual(len(set(keys)), len(keys))
        for b in self.pdbs().values():
            self.assertEqual(set(b["spec"]["selector"]["matchLabels"]), {"app", "cw/worker"})

    def test_a_young_budget_left_by_a_crashed_run_makes_a_relaunch_fail_and_stays_the_other_runs(self):
        self.create("coder1", "--run-id", A)
        st = self.st()
        del st["pods"][cw.pod_name("coder1")]   # the crashed run's pod is gone, its budget stays (under the hour)
        self.write_state(st)
        code, _, _ = self.run_cli("create", "coder1", "--repo", "a/b", "--branch", "m", "--run-id", B)
        self.assertNotEqual(code, 0)
        self.assertEqual(self.pdbs()[cw.pod_name("coder1") + "-pdb"]["metadata"]["labels"]["cw/run-id"], A)
        self.assertEqual(self.st()["pods"], {})

    # U1: every earlier launcher test passes; only the fake cluster and two forced lines changed (named in the PR body)

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
        err = self.refused("odd")
        self.assertIn("odd", err)
        self.assertRegex(err, r"kubectl create: .*odd")


class Mutants(base.Base):
    """The probe runs the scenario against a scratch copy of the source. Control: the copy as it is passes. Each mutant fails with its text."""
    PROBE = r'''
import os, sys, json, io, importlib.util, unittest
sys.path.insert(0, %(here)r)
import test_cloud_worker as base
spec = importlib.util.spec_from_file_location("cloud_worker", os.environ["CW_TEST_PDB_MODULE"])
mod = importlib.util.module_from_spec(spec); sys.modules["cloud_worker"] = mod; spec.loader.exec_module(mod)
base.cw = mod
import test_pdb
test_pdb.cw = mod
t = unittest.defaultTestLoader.loadTestsFromTestCase(test_pdb.Pdb)
only = os.environ.get("CW_TEST_PDB_ONLY")
if only:
    t = unittest.TestSuite(x for x in t if x.id().rsplit(".", 1)[1].startswith(only))
    assert t.countTestCases() >= 1
r = unittest.TextTestRunner(stream=io.StringIO(), failfast=True).run(t)
if r.wasSuccessful():
    print("PDB-PROBE-OK")
else:
    for t_, _ in r.failures + r.errors:
        print("PDB-PROBE-FAIL: " + t_.id().rsplit(".", 1)[1])
    sys.exit(1)
'''

    def probe(self, old=None, new=None, only=None):
        src = open(os.path.join(os.path.dirname(HERE), "cloud_worker.py")).read()
        if old is not None:
            self.assertEqual(src.count(old), 1, "the mutation did not apply")
            src = src.replace(old, new)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cloud_worker.py")
            with open(path, "w") as f:
                f.write(src)
            code = "import unittest\n" + self.PROBE % {"here": HERE}
            p = subprocess.run([sys.executable, "-c", code], env=dict(os.environ, **{MODULE_ENV: path, "CW_TEST_PDB_ONLY": only or ""}), capture_output=True, text=True, timeout=90, cwd=HERE)
        return p.returncode, p.stdout

    def test_control_the_unmutated_copy_passes(self):
        code, out = self.probe()
        self.assertEqual((code, out.strip()), (0, OK_LINE), out)

    def test_mutant_selector_on_the_shared_label_fails_s2(self):
        self.assertEqual(self.probe(only="test_s2_three_launches")[0], 0)   # control first, on the same test
        code, out = self.probe('"selector": {"matchLabels": {"app": LABEL, "cw/worker": worker}}}}\n', '"selector": {"matchLabels": {"app": LABEL}}}}\n', only="test_s2_three_launches")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"PDB-PROBE-FAIL: test_s2_three_launches")

    def test_mutant_budget_not_deleted_by_stop_fails_s3(self):
        self.assertEqual(self.probe(only="test_s3_stop_removes_own_budget_only")[0], 0)
        code, out = self.probe(only="test_s3_stop_removes_own_budget_only", old='''("pdb", "poddisruptionbudget/%s-pdb" % name, self.kube.delete_pdb),
                                ("secret", "secret/%s-pw" % name, self.kube.delete_secret),''', new='''("secret", "secret/%s-pw" % name, self.kube.delete_secret),''')
        self.assertEqual(code, 1)
        self.assertRegex(out, r"PDB-PROBE-FAIL: test_s3_stop_removes_own_budget_only")

    def mutant(self, old, new, anchor):
        self.assertEqual(self.probe(only=anchor)[0], 0)   # the control first, on the same tests
        code, out = self.probe(old, new, only=anchor)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"PDB-PROBE-FAIL: " + anchor)

    def test_mutant_budget_copies_annotations_fails_s4(self):
        self.mutant('"spec": {"minAvailable": 1, "selector"', '"annotations": {"x": "y"}, "spec": {"minAvailable": 1, "selector"', "test_s4_the_budget_holds")

    def test_mutant_pod_made_without_the_budget_first_fails_s2(self):
        self.mutant("                if not persistent:   # the budget BEFORE the pod", "                if False:   # the budget BEFORE the pod", "test_s2_three_launches")

    def test_mutant_a_refused_budget_create_is_ignored_fails_s8(self):
        self.mutant('self._make(made, "pdb", self.kube.apply, render_pdb(name, worker, getattr(a, "run_id", None)))',
                    'self.kube.run(["version"]) if False else made.update(pdb=True)', "test_s8_a_forbidden")

    def test_mutant_stop_keeps_the_budget_fails_s3(self):
        self.mutant('("pdb", "poddisruptionbudget/%s-pdb" % name, self.kube.delete_pdb),\n                                ("secret", "secret/%s-pw" % name, self.kube.delete_secret),',
                    '("secret", "secret/%s-pw" % name, self.kube.delete_secret),', "test_s3_stop")

    def test_mutant_reaper_keeps_orphan_budgets_fails_s3(self):
        self.mutant("                self.kube.delete_pdb(pod)\n", "                pass\n", "test_s3_the_reaper_pass")

    def test_mutant_failed_launch_keeps_the_budget_fails_s3(self):
        self.mutant("                if made.get(kind):\n", '                if made.get(kind) and kind != "pdb":\n', "test_s3_a_launch_that_fails")

    def test_mutant_gone_pod_retire_keeps_the_budget_fails_s3(self):
        self.mutant("                    if self.kube.get_pdb(name):   # this worker's budget", "                    if False:   # this worker's budget", "test_s3_a_stop_of_a_gone_pod")

    def test_mutant_delete_by_the_shared_label_removes_the_others_budget_fails_s3(self):
        self.mutant('self.run(["delete", "poddisruptionbudget", name + "-pdb", "--ignore-not-found"])',
                    'self.run(["delete", "poddisruptionbudget", "-l", "app=" + LABEL, "--ignore-not-found"])', "test_s3_")


if __name__ == "__main__":
    unittest.main()
