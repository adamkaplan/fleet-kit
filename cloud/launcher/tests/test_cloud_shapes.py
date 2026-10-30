"""The light pod shape XS (#1081): the one SHAPES table, its default, the stack rule, and every copy that must read it.

Offline: the fake kubectl records the applied pod and every call. Sets are derived from the table (SHAPES), never listed by hand,
and each mutant is an in-memory change of one member of the family; it must make exactly the named check fail."""
import argparse, contextlib, importlib.util, json, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

spec = importlib.util.spec_from_file_location("bin_cloud_worker", os.path.join(ROOT, "bin", "cloud_worker.py"))
bin_cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bin_cw)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def limits(pod):
    return pod["spec"]["containers"][0]["resources"]


class Shapes(base.Base):
    def applied_pod(self):
        return [o for o in self.st()["applied"] if o["kind"] == "Pod"][0]

    def calls(self):
        p = self.env["FAKE_KUBE_CALLS"]
        return open(p).read().splitlines() if os.path.exists(p) else []

    def made(self, *extra):
        self.write_state({"pods": {}})
        self.create("coder7", *extra)
        return self.applied_pod()

    # S2: the row and the rendered pod
    def test_s2_the_xs_row_and_its_pod(self):
        row = cw.SHAPES["XS"]
        self.assertEqual((row["cpu"], row["memory"], row["stack"]), ("1", "2Gi", False))
        pod = self.made("--shape", "XS")
        self.assertEqual(limits(pod), {"limits": {"cpu": "1", "memory": "2Gi"}})   # no requests key: Kubernetes sets requests = limits
        self.assertNotIn("runtimeClassName", pod["spec"])

    # S4: the default, the names, the stack launch
    def test_s4_no_shape_is_xs_and_a_name_is_what_it_names(self):
        for name, row in cw.SHAPES.items():
            with self.subTest(shape=name):
                pod = self.made("--shape", name)
                self.assertEqual(limits(pod)["limits"], {"cpu": row["cpu"], "memory": row["memory"]})
                self.assertEqual(pod["metadata"]["annotations"]["cw/shape"], name)
        pod = self.made()
        self.assertEqual(limits(pod)["limits"], {"cpu": "1", "memory": "2Gi"})
        self.assertEqual(pod["metadata"]["annotations"]["cw/shape"], "XS")

    def test_s4_a_stack_launch_without_a_shape_is_s_with_the_runtime_class(self):
        pod = self.made("--stack")
        self.assertEqual(limits(pod)["limits"], {"cpu": "2", "memory": "4Gi"})
        self.assertEqual(pod["metadata"]["annotations"]["cw/shape"], "S")
        self.assertEqual(pod["spec"]["runtimeClassName"], "sysbox-runc")

    def test_every_stack_row_can_run_a_stack_and_the_others_are_refused_first(self):
        self.assertTrue(cw.SHAPES[cw.DEFAULT_STACK_SHAPE]["stack"])
        for name, row in cw.SHAPES.items():
            with self.subTest(shape=name):
                self.write_state({"pods": {}})
                code, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--stack", "--shape", name)
                if row["stack"]:
                    self.assertEqual(code, 0, err)
                else:
                    self.assertEqual(code, 2, err)
                    self.assertEqual(self.calls(), [] if not os.path.exists(self.env["FAKE_KUBE_CALLS"]) else [], "no kubectl call")
                    self.assertIn(name, err)

    # U2: an unknown name is refused before any call
    def test_u2_an_unknown_shape_is_refused_before_any_call_and_lists_the_table(self):
        for bad in ("Z", "xs", "s", "L", "", "XS ", "XS,S"):
            with self.subTest(shape=bad):
                if os.path.exists(self.env["FAKE_KUBE_CALLS"]):
                    os.remove(self.env["FAKE_KUBE_CALLS"])
                code, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--shape", bad)
                self.assertEqual(code, 2, err)
                self.assertEqual(self.calls(), [])
                for name in cw.SHAPES:
                    self.assertIn(name, err)

    # U3: finite limits on every row
    def test_u3_every_shape_renders_finite_limits_equal_to_its_row(self):
        for name, row in cw.SHAPES.items():
            with self.subTest(shape=name):
                lim = limits(self.made("--shape", name))["limits"]
                self.assertEqual(lim, {"cpu": row["cpu"], "memory": row["memory"]})
                self.assertTrue(lim["cpu"] and lim["memory"])

    def test_the_cost_of_an_unnamed_worker_is_half_an_s_pod(self):
        self.assertEqual(cw.SHAPES["XS"]["usd_per_hour"] * 2, cw.SHAPES["S"]["usd_per_hour"])

    # S6: mutants, one per member of the family of behaviours
    def outcome(self, mutate):
        """True when every behaviour above still holds under `mutate` (a context manager that changes the code)."""
        checks = [self.test_s4_no_shape_is_xs_and_a_name_is_what_it_names, self.test_s4_a_stack_launch_without_a_shape_is_s_with_the_runtime_class,
                  self.test_every_stack_row_can_run_a_stack_and_the_others_are_refused_first, self.test_u2_an_unknown_shape_is_refused_before_any_call_and_lists_the_table,
                  self.test_s2_the_xs_row_and_its_pod]
        failed = []
        with mutate():
            for c in checks:
                result = unittest.TestResult()   # a fresh instance and result: subTest failures count, and none leaks into this test
                type(self)(c.__name__).run(result)
                if result.failures or result.errors:
                    failed.append(c.__name__)
        return failed

    @contextlib.contextmanager
    def patched(self, name, value):
        old = getattr(cw, name)
        setattr(cw, name, value)
        try:
            yield
        finally:
            setattr(cw, name, old)

    def test_s6_each_mutant_is_caught_by_its_check(self):
        real = cw.check_shape

        def ignore_name(a):
            real(a)
            a.shape = cw.DEFAULT_SHAPE

        def stack_gets_default(a):
            if getattr(a, "shape", None) is None:
                a.shape = cw.DEFAULT_SHAPE
            if a.shape not in cw.SHAPES:
                raise cw.CloudError("shape", cw.EXIT_USAGE)

        def unknown_falls_back(a):
            if getattr(a, "shape", None) not in cw.SHAPES:
                a.shape = cw.DEFAULT_SHAPE
            if getattr(a, "stack", False) and not cw.SHAPES[a.shape]["stack"]:
                raise cw.CloudError("stack", cw.EXIT_USAGE)

        def xs_with_stack(a):
            if getattr(a, "shape", None) is None:
                a.shape = cw.DEFAULT_STACK_SHAPE if getattr(a, "stack", False) else cw.DEFAULT_SHAPE
            if a.shape not in cw.SHAPES:
                raise cw.CloudError("shape", cw.EXIT_USAGE)

        mutants = {
            "ignore the shape name (default row for every name)": (lambda: self.patched("check_shape", ignore_name), "test_s4_no_shape_is_xs_and_a_name_is_what_it_names"),
            "the default is S again": (lambda: self.patched("DEFAULT_SHAPE", "S"), "test_s4_no_shape_is_xs_and_a_name_is_what_it_names"),
            "a stack launch takes the default shape": (lambda: self.patched("check_shape", stack_gets_default), "test_s4_a_stack_launch_without_a_shape_is_s_with_the_runtime_class"),
            "an unknown name falls back to the default": (lambda: self.patched("check_shape", unknown_falls_back), "test_u2_an_unknown_shape_is_refused_before_any_call_and_lists_the_table"),
            "--stack lets XS through": (lambda: self.patched("check_shape", xs_with_stack), "test_every_stack_row_can_run_a_stack_and_the_others_are_refused_first"),
        }
        self.assertEqual(self.outcome(contextlib.nullcontext), [])   # the real build passes every check
        for label, (mutate, expected) in mutants.items():
            with self.subTest(mutant=label):
                self.assertIn(expected, self.outcome(mutate))

    def test_s6_a_row_copied_from_s_fails_the_row_check(self):
        old = dict(cw.SHAPES["XS"])
        cw.SHAPES["XS"].update(cw.SHAPES["S"])
        try:
            with self.assertRaises(AssertionError):
                self.test_s2_the_xs_row_and_its_pod()
        finally:
            cw.SHAPES["XS"].clear()
            cw.SHAPES["XS"].update(old)


class Parity(unittest.TestCase):
    """S3: every copy of the shape names or of the default lists exactly the table's."""

    def keys_text(self, text):
        return re.findall(r"\b(XS|S|M|L)\b", text)

    def test_the_switchboard_launch_check_lists_the_table(self):
        self.assertEqual(set(bin_cw.LAUNCHER_SHAPES), set(cw.SHAPES))
        self.assertEqual(set(bin_cw.LAUNCHER_STACK_SHAPES), {k for k, v in cw.SHAPES.items() if v["stack"]})

    def test_the_launch_preflight_refuses_stack_with_each_non_stack_row(self):
        for name, row in cw.SHAPES.items():
            with self.subTest(shape=name):
                refused = name not in bin_cw.LAUNCHER_STACK_SHAPES
                self.assertEqual(refused, not row["stack"])

    def test_the_fake_launcher_and_the_help_texts_read_the_table(self):
        fake = read("bin/test-cloud-worker")
        self.assertIn('if "--shape" in argv else "%s"' % cw.DEFAULT_SHAPE, fake)
        self.assertIn("shape not in (%s)" % ", ".join('"%s"' % k for k in cw.SHAPES), fake)
        help_text = [a for a in cw.build_parser()._subparsers._group_actions[0].choices["create"]._actions if a.dest == "shape"][0].help
        for name in cw.SHAPES:
            self.assertIn(name, help_text)
        self.assertIn("unset: %s" % cw.DEFAULT_SHAPE, help_text)
        launch = re.search(r'launch\.add_argument\("--shape", help="([^"]*)"', read("bin/" + "fl" + "eet-switchboard")).group(1)
        for name, row in cw.SHAPES.items():
            self.assertIn("%s (%s CPU, %s %sB" % (name, row["cpu"], row["memory"][:-2], row["memory"][-2:]), launch)
        self.assertIn("%s (1 CPU, 2 GiB; the launcher's default" % cw.DEFAULT_SHAPE, launch)

    def test_the_docs_list_every_shape_and_say_the_default(self):
        text = read("docs/cloud-operator-quickstart.md")
        for name, row in cw.SHAPES.items():
            self.assertIn("`%s` (%s CPU, %s %sB%s" % (name, row["cpu"], row["memory"][:-2], row["memory"][-2:], ")" if row["memory"].endswith("Gi") else ","), text, name)
        interface = read("docs/cloud-worker-interface.md")
        self.assertIn("`--shape %s`" % "\\|".join(cw.SHAPES), interface)
        self.assertIn("`%s`" % cw.DEFAULT_SHAPE, interface)
        self.assertIn("(`%s` %.2f" % ("XS", cw.SHAPES["XS"]["usd_per_hour"]), read("cloud/launcher/README.md"))

    def test_s7_the_light_shape_section(self):
        text = read("docs/cloud-operator-quickstart.md")
        section = text.split("### The light shape", 1)[1].split("###", 1)[0]
        self.assertRegex(section, r"`XS`.*1 CPU, 2 GiB")
        self.assertIn("default", section)
        belongs, not_belongs = re.search(r"Belongs in `XS`: ([^\n]*)", section).group(1), re.search(r"Does not belong in `XS`: ([^\n]*)", section).group(1)
        for kind in ("reader", "planner", "tester"):
            self.assertIn(kind, belongs)
        for kind in ("nested Docker stack", "image build"):
            self.assertIn(kind, not_belongs)
        self.assertNotRegex(text, r"(?i)default[^.\n]{0,40}`S`|`S`[^.\n]{0,40}own default")   # no remaining sentence says S is the default (except for a stack: worded "Leave it out for `S`")


class OrchestratorDefault(unittest.TestCase):
    def shape(self, **kw):
        a = argparse.Namespace(**kw)
        cw.check_shape(a)
        return a.shape

    def test_orchestrator_default_is_s(self):
        s = self.shape(agent="orchestrator")
        self.assertEqual(s, "O")
        self.assertEqual((cw.SHAPES[s]["cpu"], cw.SHAPES[s]["memory"]), ("250m", "768Mi"))

    def test_worker_default_is_xs(self):
        self.assertEqual(self.shape(agent="worker"), "XS")
        self.assertEqual(self.shape(), "XS")

    def test_stack_default_is_s(self):
        self.assertEqual(self.shape(stack=True), "S")

    def test_orchestrator_explicit_shape_honoured(self):
        self.assertEqual(self.shape(agent="orchestrator", shape="M"), "M")

    def test_mutant_ignoring_agent_fails_test_1(self):
        real = cw.check_shape
        def mutant(a):
            a.agent = None
            return real(a)
        cw.check_shape = mutant
        try:
            with self.assertRaises(AssertionError):
                self.test_orchestrator_default_is_s()
        finally:
            cw.check_shape = real


if __name__ == "__main__":
    unittest.main()
