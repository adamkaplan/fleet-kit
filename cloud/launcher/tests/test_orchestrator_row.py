"""The orchestrator's own small row `O` in the one SHAPES table (ask #1507, slice 1 of ask #1068).

Offline: the fake kubectl records the applied pod. The row, the default, the pod spec, and every document that states the row are
read from the launcher's table; each mutant is an in-memory change that must make the named check fail (and its application is asserted)."""
import argparse, contextlib, datetime as dt, importlib.util, json, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
import test_cloud_stack as stack_tests  # noqa: E402
cw = base.cw

spec = importlib.util.spec_from_file_location("bin_cloud_worker_o", os.path.join(ROOT, "bin", "cloud_worker.py"))
bin_cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bin_cw)

REGISTRY = "cloud-hub-registry"
REPO = ("--repo", "acme-org/widget-service", "--branch", "main")
DOCS = ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md", "docs/cloud-worker-interface.md")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def resources(pod):
    return pod["spec"]["containers"][0]["resources"]


def row_phrase(row):
    return "`O` (%s CPU, %s memory, %.2f USD/hour)" % (row["cpu"], row["memory"], row["usd_per_hour"])


def per_day(row):
    return "24 x %.2f = %.2f USD a day" % (row["usd_per_hour"], 24 * row["usd_per_hour"])


def doc_states(texts, row):
    """The names of the documents that do not state the row's numbers."""
    bad = [name for name in ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md") if row_phrase(row) not in texts[name]]
    if "%s CPU, %s memory" % (row["cpu"], row["memory"]) not in texts["docs/cloud-worker-interface.md"]:
        bad.append("docs/cloud-worker-interface.md")
    return bad


class Rows(base.Base):
    def setUp(self):
        super().setUp()
        self.hub = dict(secrets={REGISTRY: 1, "cloud-hub-pw": 1}, secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, REGISTRY: {"secrets.json": "{}", "agents.json": "{}"}},
                        hook_result={"pushed": False, "wip_branch": None})
        self.patch_state(**self.hub)

    def calls(self):
        p = self.env["FAKE_KUBE_CALLS"]
        return open(p).read().splitlines() if os.path.exists(p) else []

    def made(self, worker, *extra):
        self.write_state({"pods": {}, **json.loads(json.dumps(self.hub))})
        code, out, err = self.run_cli("create", worker, *REPO, *extra)
        self.assertEqual(code, 0, err)
        return [o for o in self.st()["applied"] if o["kind"] == "Pod"][0]

    def orch(self, *extra):
        return self.made("orch1", "--agent", "orchestrator", "--boss", "boss1", *extra)

    # S2: the row, the default, the explicit name
    def test_s2_the_row_and_the_default(self):
        row = cw.SHAPES["O"]
        self.assertEqual((row["cpu"], row["memory"], row["stack"], row["usd_per_hour"]), ("250m", "768Mi", False, 0.02))
        self.assertEqual(cw.DEFAULT_ORCHESTRATOR_SHAPE, "O")
        pod = self.orch()
        self.assertEqual(pod["metadata"]["annotations"]["cw/shape"], "O")
        self.assertEqual(resources(pod)["limits"], {"cpu": "250m", "memory": "768Mi"})

    def test_s2_an_explicit_name_still_wins(self):
        for name in ("S", "XS", "M"):
            with self.subTest(shape=name):
                pod = self.orch("--shape", name)
                self.assertEqual(pod["metadata"]["annotations"]["cw/shape"], name)
                self.assertEqual(resources(pod)["limits"], {"cpu": cw.SHAPES[name]["cpu"], "memory": cw.SHAPES[name]["memory"]})

    def test_s2_worker_and_stack_defaults_are_unchanged(self):
        self.assertEqual((cw.DEFAULT_SHAPE, cw.DEFAULT_STACK_SHAPE), ("XS", "S"))
        self.assertEqual(self.made("coder7")["metadata"]["annotations"]["cw/shape"], "XS")
        self.assertEqual(self.made("coder7", "--stack")["metadata"]["annotations"]["cw/shape"], "S")

    def test_s2_a_worker_naming_the_row_is_a_usage_error_and_nothing_is_made(self):
        for extra in ((), ("--agent", "worker"), ("--stack",)):
            with self.subTest(extra=extra):
                self.write_state({"pods": {}})
                if os.path.exists(self.env["FAKE_KUBE_CALLS"]):
                    os.remove(self.env["FAKE_KUBE_CALLS"])
                code, out, err = self.run_cli("create", "coder7", *REPO, *extra, "--shape", "O")
                self.assertEqual(code, 2, err)
                self.assertEqual(self.calls(), [])
                self.assertEqual(self.st().get("applied", []), [])
                self.assertIn("--shape O", err)

    # S4: the pod spec
    def test_s4_the_orchestrator_pod_has_equal_small_resources_and_no_nested_docker(self):
        self.assertFalse(cw.SHAPES["O"]["stack"])
        pod = self.orch()
        res = resources(pod)
        self.assertEqual(res.get("requests", res["limits"]), res["limits"])   # no requests key: Kubernetes sets requests = limits
        self.assertEqual(res["limits"], {"cpu": "250m", "memory": "768Mi"})
        for field in ("runtimeClassName", "hostUsers", "nodeSelector"):
            self.assertNotIn(field, pod["spec"])
        self.assertNotIn("cw/stack", pod["metadata"]["annotations"])

    def build(self, shape=None, **kw):
        a = argparse.Namespace(repo="acme-org/widget-service", branch="main", idle="10m", hard="2h", max_hard="8h", shape=shape, image=None,
                               run_id=None, pull_secret=None, **kw)
        cw.check_shape(a)
        names = (cw.CHANNEL_SECRET_KEY,) if kw.get("agent") == "orchestrator" else ()
        return cw.render_pod(a, "coder7", cw.pod_name("coder7"), base.T0, base.T0 + dt.timedelta(hours=2), stack_tests.ENV, names)

    def test_s4_other_launches_build_what_the_table_without_the_row_builds(self):
        cases = {"worker": {}, "stack": {"stack": True}, "orchestrator_s": {"agent": "orchestrator", "boss": "boss1", "shape": "S"}}
        for name, kw in cases.items():
            with self.subTest(case=name):
                with_row = self.build(**kw)
                saved = dict(cw.SHAPES)
                try:
                    cw.SHAPES.pop("O")
                    self.assertNotIn("O", cw.SHAPES)
                    before = self.build(**kw)
                finally:
                    cw.SHAPES.clear()
                    cw.SHAPES.update(saved)
                self.assertEqual(with_row, before)

    def test_s4_the_recorded_pods_are_still_what_they_were(self):
        with open(os.path.join(HERE, "fixtures", "default_pod.json")) as f:
            stored = f.read()
        built = {k: stack_tests.build(a, n) for k, (a, n) in stack_tests.CASES.items()}
        self.assertEqual(json.dumps(built, indent=2, sort_keys=True) + "\n", stored)
        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(stack_tests.S2StackPod).run(result)   # the stack pod's own recorded checks, run against this code
        self.assertEqual((result.failures, result.errors), ([], []))

    # S3: the launcher, the preflight and the documents state the one row
    def test_s3_launcher_preflight_and_documents_agree(self):
        row = cw.SHAPES["O"]
        self.assertIn("O", bin_cw.LAUNCHER_SHAPES)
        self.assertNotIn("O", bin_cw.LAUNCHER_STACK_SHAPES)
        self.assertIn("250m and 768Mi", read("bin/cloud_worker.py"))
        help_text = [a for a in cw.build_parser()._subparsers._group_actions[0].choices["create"]._actions if a.dest == "shape"][0].help
        self.assertIn("O", help_text)
        launch = re.search(r'launch\.add_argument\("--shape", help="([^"]*)"', read("bin/fleet-switchboard")).group(1)
        self.assertIn("O (%s, %s;" % (row["cpu"], row["memory"]), launch)
        texts = {name: read(name) for name in DOCS}
        self.assertEqual(doc_states(texts, row), [])

    def test_s3_a_changed_number_in_a_document_fails_the_comparison(self):
        row = cw.SHAPES["O"]
        for name in DOCS:
            for old, new in (("768Mi", "512Mi"), ("250m", "500m")):
                with self.subTest(doc=name, number=old):
                    texts = {n: read(n) for n in DOCS}
                    mutated = texts[name].replace(old, new)
                    self.assertNotEqual(mutated, texts[name])   # the mutation applied
                    texts[name] = mutated
                    self.assertEqual(doc_states(texts, row), [name])

    # S6: the documents' phrases
    def test_s6_the_documents_state_the_row_the_rule_and_the_cost_per_day(self):
        row = cw.SHAPES["O"]
        for name in ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md"):
            text = read(name)
            self.assertIn(row_phrase(row), text, name)
            self.assertIn("an orchestrator launch with no `--shape`" if name.startswith("docs") else "an orchestrator with no `--shape` gets `O`", text, name)
            self.assertIn(per_day(row), text, name)
            self.assertIn("list-price estimate", text, name)
            self.assertIn("node's own price", text, name)
        self.assertEqual(per_day(row), "24 x 0.02 = 0.48 USD a day")
        self.assertNotRegex(read("docs/cloud-operator-quickstart.md"), r"orchestrator \(`--agent orchestrator`\) gets `S`")
        self.assertNotIn("orchestrator with no `--shape` gets `S`", read("cloud/launcher/README.md"))

    # S5: mutants, each asserted applied and caught by its check
    @contextlib.contextmanager
    def patched(self, name, value):
        old = getattr(cw, name)
        setattr(cw, name, value)
        try:
            self.assertEqual(getattr(cw, name), value)   # the mutation applied
            yield
        finally:
            setattr(cw, name, old)

    def fails(self, check):
        result = unittest.TestResult()
        type(self)(check.__name__).run(result)
        return bool(result.failures or result.errors)

    def test_s5_the_default_staying_s_is_caught_by_the_row_and_default_check(self):
        self.assertFalse(self.fails(self.test_s2_the_row_and_the_default))
        with self.patched("DEFAULT_ORCHESTRATOR_SHAPE", "S"):
            self.assertTrue(self.fails(self.test_s2_the_row_and_the_default))

    def test_s5_a_stack_row_is_caught_by_the_pod_check(self):
        check = self.test_s4_the_orchestrator_pod_has_equal_small_resources_and_no_nested_docker
        self.assertFalse(self.fails(check))
        old = dict(cw.SHAPES["O"])
        cw.SHAPES["O"]["stack"] = True
        try:
            self.assertTrue(cw.SHAPES["O"]["stack"])
            self.assertTrue(self.fails(check))
        finally:
            cw.SHAPES["O"].clear()
            cw.SHAPES["O"].update(old)

    def test_s5_a_nested_docker_runtime_is_caught_by_the_pod_check(self):
        check = self.test_s4_the_orchestrator_pod_has_equal_small_resources_and_no_nested_docker
        real = cw.render_pod

        def with_runtime(a, *rest, **kw):
            pod = real(a, *rest, **kw)
            if a.shape == "O":
                pod["spec"]["runtimeClassName"] = cw.STACK_RUNTIME_CLASS
            return pod
        with self.patched("render_pod", with_runtime):
            self.assertTrue(self.fails(check))


if __name__ == "__main__":
    unittest.main()
