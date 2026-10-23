"""Stop takes the run's nonce (#682, work item of #543). Scenarios S1 (the nonce stops its own worker, the audit line names both),
S2 (another nonce changes nothing), S3 (a cut-off create's stop leaves another run's same-name worker whole at 3 of 3 cut-off points),
U1 (a stop without a nonce is as before: the existing stop tests are the proof; one case is kept here). Offline: the fake kubectl."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

A, B = "aaaaaaaa11111111", "bbbbbbbb22222222"
ALL = ("secret", "run", "pod")


class StopNonce(base.Base):
    def setUp(self):
        super().setUp()
        self.name = cw.pod_name("coder7")
        self.patch_state(hook_result={"pushed": True, "wip_branch": "coder7/wip"})

    def plant(self, nonce, parts=ALL):
        """Objects of one run, as create would leave them: any subset of the password Secret, the run Secret and the pod."""
        st = self.st()
        for part in parts:
            if part == "pod":
                st.setdefault("pods", {})[self.name] = {"metadata": {"name": self.name, "annotations": {"cw/run-id": nonce, "cw/started": cw.iso(self.clock()), "cw/shape": "S"}},
                                                        "status": {"phase": "Running"}}
            else:
                n = self.name + ("-pw" if part == "secret" else "-run")
                st.setdefault("secrets", {})[n] = 1
                st.setdefault("secret_labels", {})[n] = {"cw/run-id": nonce}
        self.write_state(st)

    def stop(self, *extra):
        return self.run_cli("stop", "coder7", "--no-record", *extra)

    def objects(self):
        st = self.st()
        return sorted(list(st.get("pods", {})) + list(st.get("secrets", {})))

    # S1
    def test_s1_the_matching_nonce_stops_the_worker_and_the_audit_line_names_worker_and_nonce(self):
        self.plant(A)
        code, out, err = self.stop("--run-id", A, "--reason", "launch failed")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.objects(), [])
        lines = [l for l in self.audit_lines() if l["action"] == "stop"]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["ok"])
        self.assertEqual(lines[0]["worker"], "coder7")
        self.assertIn(A, lines[0]["reason"])
        self.assertIn("launch failed", lines[0]["reason"])

    def test_s1_a_real_create_then_stop_with_the_creates_own_nonce(self):
        self.create("coder7", "--run-id", A)
        code, out, err = self.stop("--run-id", A)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.objects(), [])
        self.assertIn(A, [l for l in self.audit_lines() if l["action"] == "stop"][0]["reason"])

    # S2
    def test_s2_another_nonce_changes_nothing_and_says_it_does_not_match(self):
        self.plant(A)
        before = self.st()
        # a prefix or an extension of the right nonce is another nonce: matching is exact
        for other in (B, A[:8], A + "00", "cccccccc33333333"):
            with self.subTest(other=other):
                code, out, err = self.stop("--run-id", other)
                self.assertEqual(code, cw.EXIT_REFUSED)
                self.assertEqual(err.count("does not match"), 1, err)
                self.assertEqual(self.st(), before)   # 0 objects deleted, nothing annotated, no hook run
        lines = [l for l in self.audit_lines() if l["action"] == "stop"]
        self.assertTrue(lines and not any(l["ok"] for l in lines))

    def test_s2_a_pod_with_no_recorded_nonce_does_not_match_any(self):
        self.plant(A)
        st = self.st()
        st["pods"][self.name]["metadata"]["annotations"].pop("cw/run-id")
        self.write_state(st)
        before = self.st()
        code, out, err = self.stop("--run-id", A)
        self.assertEqual(code, cw.EXIT_REFUSED)
        self.assertIn("does not match", err)
        self.assertEqual(self.st(), before)

    def test_a_malformed_nonce_is_a_usage_error_and_changes_nothing(self):
        self.plant(A)
        before = self.st()
        code, _, _ = self.stop("--run-id", "NOT-HEX")
        self.assertEqual(code, cw.EXIT_USAGE)
        self.assertEqual(self.st(), before)

    # S3: 3 cut-off points of the cut-off run (A), each with another run's (B) worker of the same name present in each state it can be in
    CUT_OFFS = {"before the secret": (), "between the secret and the pod": ("secret", "run"), "after the pod": ("secret", "run", "pod")}
    OTHERS = {"whole": ALL, "secrets only": ("secret", "run"), "pod only": ("pod",)}

    def test_s3_the_other_runs_worker_secrets_and_record_are_left_whole_at_every_cut_off_point(self):
        for cut, own in self.CUT_OFFS.items():
            for what, theirs in self.OTHERS.items():
                with self.subTest(cut=cut, other=what):
                    self.write_state({"pods": {}, "hook_result": {"pushed": True, "wip_branch": "w"}})
                    self.plant(B, theirs)   # a pod and a Secret of one name cannot be shared by two runs: A's objects are those B did not take
                    taken = set(theirs)
                    self.plant(A, tuple(p for p in own if p not in taken))
                    st = self.st()
                    st["marker"] = "record"
                    self.write_state(st)
                    foreign = {self.name + ("-pw" if p == "secret" else "-run") if p != "pod" else self.name for p in theirs}
                    code, out, err = self.stop("--run-id", A)
                    after = self.st()
                    left = set(after.get("pods", {})) | set(after.get("secrets", {}))
                    self.assertTrue(foreign <= left, "another run's object was deleted: %s" % (foreign - left))
                    for n in foreign:   # and still labelled as theirs
                        labels = after.get("secret_labels", {}).get(n)
                        if labels is not None:
                            self.assertEqual(labels, {"cw/run-id": B})
                    if "pod" in theirs:
                        self.assertEqual(after["pods"][self.name]["metadata"]["annotations"]["cw/run-id"], B)
                        self.assertEqual(code, cw.EXIT_REFUSED)
                    self.assertFalse(any(a for a in after.get("annotated", [])))
                    self.assertEqual(after["marker"], "record")

    def test_s3_a_cut_off_runs_own_objects_are_removed_by_its_own_nonce(self):
        for cut, own in self.CUT_OFFS.items():
            with self.subTest(cut=cut):
                self.write_state({"pods": {}, "hook_result": {"pushed": True, "wip_branch": "w"}})
                self.plant(A, own)
                code, out, err = self.stop("--run-id", A)
                self.assertEqual(code, 0, err)
                self.assertEqual(self.objects(), [])

    def test_s3_no_pod_and_a_secret_of_another_run_is_left_whole_with_exit_0(self):
        self.plant(B, ("secret", "run"))
        before = self.st()
        code, out, err = self.stop("--run-id", A)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.st(), before)

    # The Secrets are judged by their own label (_mine), on EVERY path that deletes: a running pod's stop, a Failed pod's stop, and a stop with no pod.
    LOOKALIKES = {"a longer nonce that starts with it": A + "00", "a shorter nonce that it starts with": A[:8], "another run's": B, "none": None}
    PATHS = {"running pod": "Running", "failed pod": "Failed", "no pod": None}

    def test_the_secrets_are_matched_exactly_on_every_path_that_deletes(self):
        for path, phase in self.PATHS.items():
            for which in ("secret", "run"):
                for what, label in self.LOOKALIKES.items():
                    with self.subTest(path=path, secret=which, label=what):
                        self.write_state({"pods": {}, "hook_result": {"pushed": True, "wip_branch": "w"}})
                        self.plant(A, ("secret", "run") + (("pod",) if phase else ()))
                        st = self.st()
                        if phase:
                            st["pods"][self.name]["status"]["phase"] = phase
                        n = self.name + ("-pw" if which == "secret" else "-run")
                        if label is None:
                            st["secret_labels"][n] = {}
                        else:
                            st["secret_labels"][n] = {"cw/run-id": label}
                        self.write_state(st)
                        code, out, err = self.stop("--run-id", A)
                        after = self.st()
                        self.assertEqual(code, 0, err)
                        self.assertIn(n, after["secrets"])   # the look-alike's Secret is left whole, still labelled as it was
                        self.assertEqual(after["secret_labels"][n], {} if label is None else {"cw/run-id": label})
                        other = self.name + ("-run" if which == "secret" else "-pw")
                        self.assertNotIn(other, after["secrets"])   # this run's own other Secret did go
                        self.assertNotIn(self.name, after["pods"])

    def test_a_failed_pod_of_this_run_is_deleted_by_the_nonce_with_its_own_secrets(self):
        self.plant(A)
        st = self.st()
        st["pods"][self.name]["status"]["phase"] = "Failed"
        self.write_state(st)
        code, out, err = self.stop("--run-id", A)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.objects(), [])

    def test_a_mismatch_writes_no_run_record(self):
        self.plant(A)
        rec = os.path.join(self.tmp.name, "records")
        code, out, err = self.run_cli("stop", "coder7", "--run-id", B, "--record-dir", rec)
        self.assertEqual(code, cw.EXIT_REFUSED)
        self.assertFalse(os.path.exists(rec))

    # U1 (the no-pod case)
    def test_u1_a_stop_without_a_nonce_and_without_a_pod_removes_only_the_leftover_secrets_and_exits_0(self):
        self.plant(A, ("secret", "run"))   # (#1068) a by-name stop of a gone worker removes the Secrets that exist
        code, out, err = self.stop()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.objects(), [])

    # U1
    def test_u1_a_stop_without_a_nonce_acts_by_name_as_before(self):
        self.plant(B)
        code, out, err = self.stop()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.objects(), [])
        line = [l for l in self.audit_lines() if l["action"] == "stop"][0]
        self.assertNotIn("run-id", line["reason"])


class DriverPassesItsNonce(base.Base):
    def test_the_drivers_stop_carries_its_own_nonce(self):
        sys.path.insert(0, os.path.dirname(HERE))
        import run_task
        d = run_task.Driver(type("A", (), {"worker": "coder7"})(), {})
        seen = []
        d.run_cloud_worker = lambda argv, timeout=0: seen.append(argv) or (0, "", "")
        d.relay = lambda *a, **k: None
        self.assertTrue(d.stop("driver ended early"))
        self.assertEqual(seen[0][seen[0].index("--run-id") + 1], d.nonce)


if __name__ == "__main__":
    unittest.main()
