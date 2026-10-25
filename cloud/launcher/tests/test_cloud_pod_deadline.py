"""The cluster-side floor (#876): every pod the launcher builds carries spec.activeDeadlineSeconds = cap + grace.

Offline: the fake kubectl records the applied pod. The ONE table below is derived from the code (the shapes the launcher
knows, crossed with the pod's variants); a completeness test fails if the source gains a second pod builder.
"""
import json, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

# One row per way a pod gets built: a shape (derived from SHAPES) with a variant of the optional parts.
VARIANTS = {"plain": [], "pull-secret": ["--pull-secret", "cw-pull"]}
BUILDERS = [(shape, var) for shape in sorted(cw.SHAPES) for var in sorted(VARIANTS)]
CAPS = {"default": ([], 8 * 3600), "1h cap": (["--hard", "30m", "--max-hard", "1h"], 3600), "30m cap": (["--hard", "10m", "--max-hard", "30m"], 1800)}


class PodDeadline(base.Base):
    def applied_pod(self):
        return [o for o in self.st()["applied"] if o["kind"] == "Pod"][0]

    def test_every_builder_sets_the_floor_at_cap_plus_grace(self):
        for shape, var in BUILDERS:
            for capname, (extra, secs) in CAPS.items():
                with self.subTest(shape=shape, variant=var, cap=capname):
                    self.write_state({"pods": {}})
                    self.create("coder7", "--shape", shape, *VARIANTS[var], *extra)
                    spec = self.applied_pod()["spec"]
                    self.assertEqual(spec["activeDeadlineSeconds"], secs + cw.POD_DEADLINE_GRACE_SECONDS)
                    self.assertIs(type(spec["activeDeadlineSeconds"]), int)
                    self.assertEqual(spec["restartPolicy"], "Never")

    def test_the_floor_is_never_below_the_stored_hard_deadline_and_stays_legal_after_an_extension(self):
        self.create("coder7", "--hard", "1h", "--max-hard", "3h")
        pod = self.pod()
        floor = pod["spec"]["activeDeadlineSeconds"]
        self.run_cli("extend", "coder7", "--hard", "+2h")   # up to the cap: still inside the floor
        stored = cw.parse_iso(self.pod()["metadata"]["annotations"]["cw/hard-deadline"]) - cw.parse_iso(pod["metadata"]["annotations"]["cw/started"])
        self.assertLessEqual(stored.total_seconds(), floor)
        self.assertEqual(floor, 3 * 3600 + cw.POD_DEADLINE_GRACE_SECONDS)
        self.assertEqual(self.pod()["spec"]["activeDeadlineSeconds"], floor)  # extend never touches the field

    def test_the_floor_is_finite_and_the_grace_is_a_few_minutes(self):
        self.assertTrue(0 < cw.POD_DEADLINE_GRACE_SECONDS <= 600)
        self.assertEqual(cw.pod_deadline_floor(cw.DEFAULT_MAX_HARD), 8 * 3600 + cw.POD_DEADLINE_GRACE_SECONDS)
        self.assertEqual(cw.pod_deadline_floor(None), 8 * 3600 + cw.POD_DEADLINE_GRACE_SECONDS)

    def test_the_default_pod_differs_from_before_only_by_the_one_field(self):
        self.create()
        spec = dict(self.applied_pod()["spec"])
        self.assertEqual(spec.pop("activeDeadlineSeconds"), 8 * 3600 + 300)
        self.assertEqual(spec.pop("terminationGracePeriodSeconds"), cw.POD_TERMINATION_GRACE_SECONDS)
        self.assertEqual(sorted(spec), sorted(["restartPolicy", "serviceAccountName", "automountServiceAccountToken",
                                              "securityContext", "containers", "volumes"]))

    def test_there_is_exactly_one_pod_builder_in_the_source(self):
        src = os.path.join(os.path.dirname(HERE), "cloud_worker.py")
        with open(src) as f:
            text = f.read()
        self.assertEqual(len(re.findall(r'"kind":\s*"Pod"', text)), 1)
        self.assertEqual(len(re.findall(r'"restartPolicy":\s*"Never"', text)), 1)
        self.assertEqual(len(re.findall(r'"activeDeadlineSeconds":\s*pod_deadline_floor', text)), 1)
        self.assertGreaterEqual(len(BUILDERS), 2)

    def test_the_run_secret_variant_of_the_builder_carries_the_floor(self):
        # render_pod called directly: the run-secret volume shape is a variant of the one builder.
        import argparse, datetime as dt
        now = dt.datetime(2026, 10, 19, tzinfo=dt.timezone.utc)
        for shape in sorted(cw.SHAPES):
            with self.subTest(shape=shape):
                a = argparse.Namespace(image=None, repo="acme-org/widget-service", branch="main", idle="10m", hard="1h",
                                       max_hard="2h", shape=shape, run_id=None, pull_secret=None)
                pod = cw.render_pod(a, "coder7", "cw-coder7", now, now + dt.timedelta(hours=1), self.env, ["model-key"])
                self.assertEqual(pod["spec"]["activeDeadlineSeconds"], 7200 + cw.POD_DEADLINE_GRACE_SECONDS)
                self.assertIn("projected", pod["spec"]["volumes"][0])

    def test_every_frozen_fixture_case_including_the_stack_pod_carries_the_floor(self):
        # The cases the frozen fixtures are built from (default and stack, S and M, run secrets, pull secret): all go through render_pod.
        import test_cloud_stack as st
        cases = {**st.CASES, **st.STACK_CASES}
        self.assertTrue(any(a.__dict__.get("stack") for a, _ in cases.values()))
        for name, (a, names) in sorted(cases.items()):
            with self.subTest(case=name):
                pod = st.build(a, names)
                self.assertEqual(pod["spec"]["activeDeadlineSeconds"], 8 * 3600 + cw.POD_DEADLINE_GRACE_SECONDS)

    def test_a_pod_the_floor_killed_is_still_cleaned_up_by_stop_and_by_the_reaper(self):
        """A regression guard on stop and reap (unchanged by this PR): a Failed/DeadlineExceeded pod and its Secrets still go."""
        for how in ("stop", "reap"):
            with self.subTest(how=how):
                self.write_state({"pods": {}})
                self.create("coder7", "--hard", "1h", "--max-hard", "1h")
                st = self.st()
                pod = st["pods"][cw.pod_name("coder7")]
                pod["status"] = {"phase": "Failed", "reason": "DeadlineExceeded"}
                self.write_state(st)
                self.clock.advance(hours=1, minutes=6)
                if how == "stop":
                    self.assertEqual(self.run_cli("stop", "coder7")[0], 0)
                else:
                    res = cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()
                    self.assertEqual(res, [("coder7", "hard limit", 0)])
                self.assertIsNone(self.pod())
                self.assertEqual(self.st().get("secrets", {}), {})


if __name__ == "__main__":
    unittest.main()
