"""Ask #2305: a create with no --idle gives a coder pod 60 minutes (the orchestrator keeps 30). A mutant is a copy of the code in memory."""
import io, json, os, sys, types, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw
REPO = ("--repo", "acme-org/widget-service", "--branch", "main")
LAUNCHER = os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py")


def idle_problem(got, want):
    return None if got == str(want) else "idle-seconds is not %s: %s" % (want, got)


class IdleDefault(base.Base):
    def setUp(self):
        super().setUp()
        self.hub = dict(secrets={"cloud-hub-registry": 1, "cloud-hub-pw": 1}, secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, "cloud-hub-registry": {"secrets.json": "{}", "agents.json": "{}"}},
                        hook_result={"pushed": False, "wip_branch": None})
        self.patch_state(**self.hub)

    def idle_of(self, worker, *extra, module=None):
        self.write_state({"pods": {}, **json.loads(json.dumps(self.hub))})
        with redirect_stdout(io.StringIO()):
            code = (module or cw).main(["create", worker, *REPO, *extra], self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        self.assertEqual(code, 0)
        return self.pod(worker)["metadata"]["annotations"]["cw/idle-seconds"]

    def test_a_coder_launch_without_idle_is_3600(self):
        self.assertEqual(self.idle_of("coder7"), "3600")

    def test_an_explicit_idle_keeps_working(self):
        self.assertEqual(self.idle_of("coder7", "--idle", "20m"), "1200")

    def test_an_orchestrator_launch_keeps_the_older_default(self):
        self.assertEqual(self.idle_of("orch1", "--agent", "orchestrator", "--boss", "boss1"), "1800")

    def hard_seconds_of(self, worker, *extra):
        self.write_state({"pods": {}, **json.loads(json.dumps(self.hub))})
        with redirect_stdout(io.StringIO()):
            code = cw.main(["create", worker, *REPO, *extra], self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        self.assertEqual(code, 0)
        ann = self.pod(worker)["metadata"]["annotations"]
        return int((cw.parse_iso(ann["cw/hard-deadline"]) - cw.parse_iso(ann["cw/started"])).total_seconds())

    def test_the_hard_default_is_unchanged(self):  # a launch without --shape L keeps 2h
        self.assertEqual(self.hard_seconds_of("coder7"), 7200)
        self.assertEqual(self.hard_seconds_of("coder8", "--shape", "M"), 7200)

    def test_the_L_shape_default_hard_is_8h(self):  # the one per-shape exception
        self.assertEqual(self.hard_seconds_of("coder9", "--shape", "L"), 28800)

    def test_the_hard_default_of_a_shape_that_is_not_L_is_2h(self):
        for shape in ("XS", "S", "M", "O"):
            self.assertEqual(cw.SHAPE_DEFAULT_HARD.get(shape, cw.DEFAULT_HARD), "2h", shape)

    def test_the_L_shape_is_the_one_exception_with_the_maximum_hard_limit(self):
        self.assertEqual(cw.SHAPE_DEFAULT_HARD, {"L": cw.DEFAULT_MAX_HARD})
        self.assertEqual(cw.DEFAULT_MAX_HARD, "8h")

    def parse_default(self, name):
        return cw.build_parser().parse_args(["create", "w", *REPO]).__dict__[name]

    def test_a_mutant_without_the_change_fails_with_an_anchored_text_control_first(self):
        control = idle_problem(self.idle_of("coder7"), 3600)
        self.assertIsNone(control)
        with open(LAUNCHER) as f:
            source = f.read()
        old = 'DEFAULT_IDLE = "60m"'
        self.assertEqual(source.count(old), 1)
        mutant = types.ModuleType("mutant_cloud_worker")
        mutant.__file__ = LAUNCHER
        exec(compile(source.replace(old, 'DEFAULT_IDLE = "30m"'), LAUNCHER, "exec"), mutant.__dict__)
        self.setUp()
        problem = idle_problem(self.idle_of("coder7", module=mutant), 3600)
        self.assertEqual(problem, "idle-seconds is not 3600: 1800")


if __name__ == "__main__":
    unittest.main()
