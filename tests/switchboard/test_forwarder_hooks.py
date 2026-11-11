"""The forwarder's cli webhooks (#2397): stale ones are removed before a forwarder starts, its own when it stops, a failure never stops the start. A fake runner and a fake spawn: no real GitHub call."""
import json
import subprocess
from unittest import mock
from support.core import Base
from support.loader import sb

REPO = "acme/widgets"
CAP = 20   # GitHub's webhook cap per repository
URL = "https://webhook-forwarder.github.com/hook"


def hook(number, name="cli", url=URL, active=True):
    return {"id": number, "name": name, "active": active, "config": {"url": url}}


class FakeProc:
    def __init__(self, argv):
        self.argv, self.code, self.pid = list(argv), None, 424242

    def poll(self):
        return self.code

    def terminate(self):
        self.code = -15


class HooksRunner:
    """`gh api` over a table of hooks: list, delete; a failure to list or delete on demand. Every call is recorded."""

    def __init__(self, hooks, list_error=None, delete_error=None):
        self.hooks, self.list_error, self.delete_error, self.calls = list(hooks), list_error, delete_error, []
        self.forwarders_started = 0

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        path = next((a for a in argv if a.startswith("repos/")), "")
        if "--method" in argv and argv[argv.index("--method") + 1] == "DELETE":
            if self.delete_error:
                return subprocess.CompletedProcess(argv, 1, "", self.delete_error)
            number = int(path.rsplit("/", 1)[1])
            found = [h for h in self.hooks if h["id"] == number]
            if not found:
                return subprocess.CompletedProcess(argv, 1, "", "gh: Not Found (HTTP 404)")
            self.hooks.remove(found[0])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if path.startswith("repos/%s/hooks" % REPO):
            if self.list_error:
                return subprocess.CompletedProcess(argv, 1, "", self.list_error)
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.hooks), "")
        return subprocess.CompletedProcess(argv, 1, "", "HTTP 404")

    def deleted(self):
        return [int(c[-1].rsplit("/", 1)[1]) for c in self.calls if "--method" in c]


class TestForwarderHooks(Base):
    def build(self, runner, clear=True):
        self.runner, self.procs, self.at = runner, [], [1000.0]
        runner_ref = self

        def spawn(argv, log, **kwargs):
            # the forwarder asks GitHub for a hook of its own as it starts: the stale ones are gone by now when they were removed
            if hasattr(runner, "hooks"):
                runner_ref.hooks_when_started = [h["id"] for h in runner.hooks]
                runner_ref.refused = len(runner.hooks) >= CAP   # GitHub: a 21st webhook is HTTP 422
                if not runner_ref.refused:
                    runner.hooks.append(hook(900))
            proc = FakeProc(argv)
            runner_ref.procs.append(proc)
            return proc

        (self.home / "state").mkdir(exist_ok=True)
        self.audit = []
        audit = mock.Mock()
        audit.append = lambda actor, event, subject, detail: self.audit.append((event, subject, detail))
        hub = sb.GitHubHub(sb.validate_config({"github_repos": [REPO], "gh": "gh"}), self.home / "state", audit, runner, spawn, lambda: self.at[0])
        hub.port = 4711
        if not clear:   # today's code: nothing is listed or removed
            for name in ("_clear_stale_hooks", "_release_hook", "_claim_hook"):
                patcher = mock.patch.object(hub, name, lambda *a, **k: None)
                patcher.start()
                self.addCleanup(patcher.stop)
        return hub

    def stale_world(self):
        stale = [hook(i, active=bool(i % 4)) for i in range(1, CAP - 2)]   # with the three below: the cap of 20
        return stale + [hook(100, name="web"), hook(101, url="https://example.invalid/hook"), hook(102, name="cli", url="")]

    def start(self, runner, clear=True):
        hub = self.build(runner, clear)
        hub.tick()
        return hub

    def test_s2_stale_cli_hooks_are_removed_before_the_start_and_others_stay(self):
        runner = HooksRunner(self.stale_world())
        hub = self.start(runner)
        self.assertEqual(len(self.procs), 1)
        self.assertFalse(self.refused, "the repo was not full at the start")
        stale = set(range(1, CAP - 2))
        self.assertEqual(set(runner.deleted()) - stale, set(), "a hook that is not a stale cli forwarder hook was removed")
        self.assertEqual(stale & set(self.hooks_when_started), set(), "a stale hook was still there when the forwarder started")
        self.assertTrue({100, 101, 102} <= set(self.hooks_when_started))
        self.assertIsNone(hub.watches[REPO].hook_note)

    def test_s3_a_stop_removes_only_the_hook_the_forwarder_made(self):
        runner = HooksRunner(self.stale_world())
        hub = self.start(runner)
        self.at[0] += sb.GITHUB_CATCHUP_DELAY + 1
        runner.hooks.append(hook(950))   # another forwarder's hook, made after ours: it is two new hooks, so none is ours to claim for sure
        runner.calls.clear()
        hub.stop()
        self.assertEqual(runner.deleted(), [])
        # one new hook: it is ours
        runner = HooksRunner(self.stale_world())
        hub = self.start(runner)
        self.at[0] += sb.GITHUB_CATCHUP_DELAY + 1   # the forwarder has connected
        runner.calls.clear()
        hub.stop()
        self.assertEqual(runner.deleted(), [900])
        self.assertTrue({100, 101, 102} <= {h["id"] for h in runner.hooks})

    def test_s3_a_hook_that_is_gone_already_is_no_failure(self):
        runner = HooksRunner([])
        hub = self.start(runner)
        self.at[0] += sb.GITHUB_CATCHUP_DELAY + 1
        hub.tick()   # claimed: it is the one new hook
        real = runner.__call__
        def vanishing(argv, **kwargs):
            if "--method" in argv:   # it is gone when the delete arrives
                runner.hooks.clear()
            return real(argv, **kwargs)
        hub.runner = vanishing
        hub.stop()
        self.assertEqual(runner.deleted(), [900], "the delete was asked")
        self.assertIsNone(hub.watches[REPO].hook_note)
        self.assertEqual([a for a in self.audit if a[0] == "github.hooks"], [])

    def test_s4_a_failing_list_still_starts_with_one_audit_line_and_a_note(self):
        scope = 'gh: This API operation needs the "admin:repo_hook" scope. (HTTP 403)'
        runner = HooksRunner(self.stale_world(), list_error=scope)
        hub = self.start(runner)
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(runner.deleted(), [])
        for _ in range(3):
            hub.watches[REPO].proc.code = 1
            hub.tick()   # it exited: backoff
            hub.watches[REPO].next_start = 0
            hub.tick()   # started again
        lines = [a for a in self.audit if a[0] == "github.hooks"]
        self.assertEqual(len(lines), 1)
        self.assertIn("admin:repo_hook", lines[0][2]["error"])
        self.assertIn("admin:repo_hook", hub.watches[REPO].to_json()["hook_note"])
        report = {"repos": [REPO], "watches": [hub.watches[REPO].to_json()], "daemon": True, "lookback_hours": 1, "excluded": [],
                  "detail": [{"repo": REPO, "how": "forwarder", "account": "a", "agents": [], "why": hub.watches[REPO].to_json()["hook_note"], "read_age": None, "health": None}]}
        self.assertIn("admin:repo_hook", "\n".join(sb.format_github(report)))

    def test_s4_a_failing_removal_still_starts_with_one_audit_line(self):
        runner = HooksRunner(self.stale_world(), delete_error="gh: boom (HTTP 500)")
        hub = self.start(runner)
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(len(runner.deleted()), len(set(runner.deleted())), "a failed removal is not retried")
        self.assertEqual(len(runner.deleted()), 1, "the cleanup stops at the first failed removal")
        self.assertEqual(len([a for a in self.audit if a[0] == "github.hooks"]), 1)
        self.assertIn("hook cleanup failed", hub.watches[REPO].hook_note)

    def test_the_status_is_compared_exactly_not_as_text(self):
        for said in ("gh: boom (HTTP 500) see 404", "gh: HTTP 4040", "gh: Not Found (HTTP 1404)"):
            with self.subTest(said=said):
                runner = HooksRunner([hook(1)], delete_error=said)
                hub = self.start(runner)
                self.assertIn("hook cleanup failed", hub.watches[REPO].hook_note, "a failure that only mentions 404 is a failure")
        hub = self.build(HooksRunner([]))
        for said, status in (("gh: Not Found (HTTP 404)", 404), ("gh: boom (HTTP 500) see 404", 500), ("gh: no code here", None)):
            with self.subTest(said=said):
                hub.runner = lambda argv, said=said, **kw: subprocess.CompletedProcess(argv, 1, "", said)
                with self.assertRaises(sb.GitHubUnavailable) as caught:
                    hub._hook_api(REPO, None, ["repos/%s/hooks" % REPO])
                self.assertEqual(caught.exception.status, status)

    def test_a_list_answering_404_names_the_scope(self):
        hub = self.start(HooksRunner([hook(1)], list_error="gh: Not Found (HTTP 404)"))
        self.assertIn("admin:repo_hook", hub.watches[REPO].hook_note)
        self.assertEqual(len(self.procs), 1)

    def test_a_dot_segment_in_the_repository_name_is_refused(self):
        hub = self.build(HooksRunner([]))
        for repo in ("o/.", "./r", "o/..", "../r"):
            with self.subTest(repo=repo):
                with self.assertRaises(sb.GitHubUnavailable):
                    hub._hook_api(repo, None, ["x"])

    def test_a_runner_that_raises_never_stops_the_start(self):
        def broken(argv, **kwargs):
            if any(a.startswith("repos/") for a in argv):
                raise subprocess.TimeoutExpired(argv, 1)
            return subprocess.CompletedProcess(argv, 1, "", "no")
        hub = self.start(broken)
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(len([a for a in self.audit if a[0] == "github.hooks"]), 1)

    def test_s5_controls_on_todays_code_leave_the_stale_hooks_and_the_note_out(self):
        runner = HooksRunner(self.stale_world())
        hub = self.start(runner, clear=False)
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(runner.deleted(), [])
        self.assertTrue(self.refused, "S2 control: the repo is full when the forwarder starts (GitHub answers 422 to its hook)")
        self.assertEqual(len(set(range(1, CAP - 2)) & set(self.hooks_when_started)), len(range(1, CAP - 2)), "S2 control: the stale hooks stay")
        roomy = HooksRunner([hook(1)])   # room for the forwarder's own hook, as in the S3 test
        hub = self.start(roomy, clear=False)
        self.at[0] += sb.GITHUB_CATCHUP_DELAY + 1   # the forwarder has connected
        roomy.calls.clear()
        hub.stop()
        self.assertEqual(roomy.deleted(), [], "S3 control: a stop removes none")
        self.assertIn(900, [h["id"] for h in roomy.hooks], "S3 control: the forwarder's own hook stays")
        failing = HooksRunner(self.stale_world(), list_error="HTTP 403")
        hub = self.start(failing, clear=False)
        self.assertEqual([a for a in self.audit if a[0] == "github.hooks"], [], "S4 control: no audit line, no note")
        self.assertIsNone(hub.watches[REPO].hook_note)
        removing = HooksRunner(self.stale_world(), delete_error="HTTP 500")
        hub = self.start(removing, clear=False)
        self.assertEqual(removing.deleted(), [], "S4 control (removal): none is tried, so none fails")
        self.assertEqual([a for a in self.audit if a[0] == "github.hooks"], [])

    def test_a_start_after_a_conflict_removes_inactive_hooks_only(self):
        runner = HooksRunner(self.stale_world())
        hub = self.build(runner)
        hub.watches[REPO].state = "conflict"
        hub.tick()
        inactive = {h["id"] for h in self.stale_world() if not h["active"] and h["name"] == "cli" and h["config"]["url"] == URL}
        self.assertTrue(inactive)
        self.assertEqual(set(runner.deleted()), inactive, "another forwarder's live (active) hook looks stale: after a conflict only inactive hooks go")

    def test_a_stop_before_the_hook_exists_removes_nothing_and_a_gone_claim_is_not_deleted(self):
        runner = HooksRunner([hook(1)])
        hub = self.start(runner)
        self.at[0] += sb.GITHUB_CATCHUP_DELAY + 1
        runner.hooks[:] = []   # no hook of the forwarder exists at the stop
        runner.calls.clear()
        hub.stop()
        self.assertEqual(runner.deleted(), [])
        runner = HooksRunner([hook(1)])
        hub = self.start(runner)
        watch = hub.watches[REPO]
        watch.hook_id = 900
        runner.hooks[:] = [hook(900, name="web")]   # that id is now a hook of another kind
        runner.calls.clear()
        hub.stop()
        self.assertEqual(runner.deleted(), [], "an id that is no longer a cli forwarder hook is never deleted")

    def test_a_repo_name_with_a_dot_segment_is_never_put_in_a_path(self):
        runner = HooksRunner([])
        hub = self.build(runner)
        with self.assertRaises(sb.GitHubUnavailable):
            hub._hook_api("../x", None, ["repos/../x/hooks"])
        self.assertEqual(runner.calls, [])

    def test_a_hook_that_appears_after_the_forwarder_died_is_never_claimed(self):
        runner = HooksRunner([])
        hub = self.start(runner)
        hub.watches[REPO].proc.code = 1
        hub.tick()   # it exited
        runner.hooks[:] = [hook(777)]   # another party's forwarder starts on the repo
        runner.calls.clear()
        hub.stop()
        self.assertEqual(runner.deleted(), [])

    def test_a_second_claim_is_never_tried(self):
        runner = HooksRunner([])
        hub = self.start(runner)
        runner.hooks[:] = []   # the connect moment passes with no hook of ours listed
        self.at[0] += sb.GITHUB_CATCHUP_DELAY + 1
        hub.tick()
        runner.hooks.append(hook(778))   # someone else's, later
        runner.calls.clear()
        hub.stop()
        self.assertEqual(runner.deleted(), [])

    def test_s4_the_status_note_reaches_the_status_report(self):
        runner = HooksRunner(self.stale_world(), list_error='gh: This API operation needs the "admin:repo_hook" scope. (HTTP 403)')
        hub = self.start(runner)
        (self.home / "state").mkdir(exist_ok=True)
        snapshot = json.loads((self.home / "state" / sb.WATCH_FILE).read_text())
        self.assertIn("admin:repo_hook", snapshot["watches"][0]["hook_note"])
        config = sb.validate_config({"github_repos": [REPO], "gh": "gh"})
        report = sb.github_status(config, self.home / "state", {})
        self.assertIn("admin:repo_hook", "\n".join(sb.format_github(report)))

    def test_a_stop_before_the_forwarder_has_connected_claims_nothing(self):
        runner = HooksRunner([])
        hub = self.start(runner)
        runner.calls.clear()
        hub.stop()   # no time passed: a hook seen now could be anyone's
        self.assertEqual(runner.deleted(), [])

    def test_a_conflict_is_remembered_over_a_failed_retry(self):
        runner = HooksRunner(self.stale_world())
        hub = self.build(runner)
        watch = hub.watches[REPO]
        watch.state, watch.conflicted = "backoff", True   # a conflict, then a retry that failed for another reason
        hub.tick()
        self.assertTrue(all(not h["active"] for h in self.stale_world() if h["id"] in runner.deleted()), "no active hook after a conflict")
        self.assertTrue(runner.deleted())
        hub.tick()
        self.at[0] += sb.GITHUB_CATCHUP_DELAY + 1
        hub.tick()   # our child ran past the connect delay: nobody else holds the hook
        self.assertFalse(watch.conflicted)
