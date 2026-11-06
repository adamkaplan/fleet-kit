"""#2247 S3: the integration tip watch. A merge that turns the tip's CI red raises ONE notice naming the merge, its pull request and the failed jobs, to the Chief of Staff and the
merging lane; the same red on the next head raises none; a new failing job on a red tip raises one. The class borrows the host file's Base, World and fakes (build())."""
import json
import subprocess
import types
from unittest import mock


class TipGh:
    """`gh` for the tip watch: the runs of the tip branch and each run's jobs. Counts every call; anything else is an error."""

    def __init__(self, sb):
        self.sb, self.runs, self.jobs, self.calls, self.fault = sb, [], {}, [], False

    def add(self, now, run_id, head, pr, jobs=(), conclusion=None, age=60, status="completed", branch="integration"):
        failed = [j for j in jobs]
        self.runs.append({"id": run_id, "head_sha": head, "head_branch": branch, "status": status, "event": "workflow_dispatch",
                          "conclusion": conclusion or ("failure" if failed else "success"), "created_at": self.sb.iso(now - age),
                          "head_commit": {"message": "A change (#%d)\n\nbody" % pr if pr else "A direct push"}})
        self.jobs[run_id] = [{"name": n, "conclusion": "failure"} for n in failed] + [{"name": "ok-job", "conclusion": "success"}]

    def __call__(self, argv, **kwargs):
        path = list(argv)[-1]
        self.calls.append(path)
        done = lambda text, code=0: subprocess.CompletedProcess(argv, code, text, "gh: HTTP 500" if code else "")
        if self.fault and "/jobs" in path:
            return done("", 1)
        if path.startswith("repos/acme/widget/actions/runs?branch=integration"):
            return done(json.dumps({"workflow_runs": self.runs}))
        for run_id, jobs in self.jobs.items():
            if path.startswith("repos/acme/widget/actions/runs/%d/jobs" % run_id):
                return done(json.dumps({"jobs": jobs}))
        raise AssertionError("the tip watch reaches for nothing else: %r" % (path,))


def build(Base, World, FAKE_GH, EjectionHub, sb):
    class TestTipWatch(Base):
        REPO = "acme/widget"

        def setUp(self):
            super().setUp()
            self.world = World(self, agents=(), config={"gh": FAKE_GH, "github_repos": [self.REPO], "excluded_repos": ["acme/secret"]})
            self.engine = self.world.new_engine()
            self.engine.discovery, self.engine.hub, self.sent = sb.Discovery(), EjectionHub(owner="lane"), []
            self.gh = TipGh(sb)
            self.engine.gh_runner = self.gh
            for target, kw in ((self.engine, {"side_effect": lambda sender, to, text, **k: self.sent.append((to, text)) or {"sent": True}}),):
                patcher = mock.patch.object(target, "send", **kw)
                patcher.start()
                self.addCleanup(patcher.stop)
            chief = mock.patch.object(sb, "chiefs_of_staff", return_value=["chief"])
            chief.start()
            self.addCleanup(chief.stop)
            self.module = sb.load_module("tip_watch")

        def go(self, calls=60, wait=None):
            self.world.tick(self.module.LIST_SECONDS + 1 if wait is None else wait)
            ns = types.SimpleNamespace(**{n: getattr(sb, n) for n in self.module.NEEDS})
            return self.module.run_pass(ns, self.engine, self.world.clock.now(), calls)

        def add(self, run_id, head, pr, jobs=(), **kw):
            self.gh.add(self.world.clock.now(), run_id, head, pr, jobs, **kw)

        def test_a_green_tip_raises_nothing_and_costs_one_list(self):   # control first
            self.add(1, "a" * 40, 11)
            self.add(2, "b" * 40, 12, age=30)
            self.go()
            self.assertEqual(self.sent, [])
            self.assertEqual([c for c in self.gh.calls if "/jobs" in c], [], "a passing run needs no job read")

        def test_s3_a_merge_turning_green_to_red_raises_one_notice_to_the_chief_and_the_lane(self):
            self.add(1, "a" * 40, 11, age=300)
            self.add(2, "b" * 40, 12, ["unit tests", "lint"], age=30)
            self.go()
            self.assertEqual(sorted(to for to, _ in self.sent), ["chief", "lane"])
            line = self.sent[0][1]
            for want in ("bbbbbbbb", "#12", "unit tests", "lint", self.REPO):
                self.assertIn(want, line)
            self.go()
            self.assertEqual(len(self.sent), 2, "once per head, a restart-free second pass tells nobody again")
            self.engine.__dict__.pop("tip_watch")   # the daemon restarted: the file remembers
            self.go()
            self.assertEqual(len(self.sent), 2)

        def test_the_same_red_on_the_next_head_raises_none(self):
            self.add(1, "a" * 40, 11, age=300)
            self.add(2, "b" * 40, 12, ["unit tests"], age=200)
            self.go()
            self.assertEqual(len(self.sent), 2)
            self.add(3, "c" * 40, 13, ["unit tests"], age=30)
            self.go()
            self.assertEqual(len(self.sent), 2, "the same red repeated is not news")

        def test_a_new_failing_job_on_a_red_tip_raises_one_naming_only_the_new_job(self):
            self.add(1, "a" * 40, 11, ["unit tests"], age=300)
            self.go()
            self.sent.clear()   # the tip was red already (the first red seen: unknown before it counts as green)
            self.add(2, "b" * 40, 12, ["unit tests", "lint"], age=30)
            self.go()
            self.assertEqual(sorted(to for to, _ in self.sent), ["chief", "lane"])
            line = self.sent[0][1]
            self.assertIn("lint", line)
            self.assertNotIn("unit tests", line)
            self.assertIn("already red", line)

        def test_a_red_head_with_no_pull_request_tells_the_chief_alone(self):
            self.add(2, "b" * 40, 0, ["unit tests"], age=30)
            self.go()
            self.assertEqual([to for to, _ in self.sent], ["chief"])
            self.assertIn("not known", self.sent[0][1])

        def test_history_a_cancelled_run_a_run_still_going_and_another_branch_tell_nobody(self):
            self.add(1, "a" * 40, 11, ["unit tests"], age=self.module.WAKE_WINDOW + 500)
            self.add(2, "b" * 40, 12, conclusion="cancelled", age=200)
            self.add(3, "c" * 40, 13, ["x"], status="in_progress", conclusion="failure", age=100)
            self.add(4, "d" * 40, 14, ["x"], branch="main", age=50)
            self.go()
            self.assertEqual(self.sent, [])

        def test_a_failed_job_read_tells_nobody_and_is_retried_after_the_back_off(self):
            self.add(2, "b" * 40, 12, ["unit tests"], age=30)
            self.gh.fault = True
            self.go()
            self.assertEqual(self.sent, [])
            self.gh.fault = False
            self.go(wait=self.module.RETRY_SECONDS - 100)
            self.assertEqual(self.sent, [], "inside the back-off")
            self.go(wait=self.module.RETRY_SECONDS)
            self.assertEqual(len(self.sent), 2)

        def test_job_names_are_data_not_commands(self):
            self.add(2, "b" * 40, 12, ["@all /deploy ghp_" + "Ab1" * 10], age=30)
            self.go()
            for bad in ("@", "ghp_", "://"):
                self.assertNotIn(bad, self.sent[0][1])

        def test_no_chief_of_staff_tells_nobody_and_later_tells_once(self):
            self.add(2, "b" * 40, 12, ["unit tests"], age=30)
            with mock.patch.object(sb, "chiefs_of_staff", return_value=[]):
                self.go()
            self.assertEqual(self.sent, [])
            self.world.tick(self.module.RETRY_SECONDS + 1)
            self.go()
            self.assertEqual(len(self.sent), 2)

    return TestTipWatch
