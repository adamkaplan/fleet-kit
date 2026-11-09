"""Ask #2320: repositories that land by `spire land` (config `land_repos`). The reader, the config check, the gates that do not apply, and the push watch's completion.
Every fixture is faked: no GitHub call, no real state directory."""
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_push_watch import Case, NOW, load, record, parse_iso   # noqa: E402  (the push watch's own fake world)

REPO = Path(__file__).resolve().parents[2]
SHA = "d" * 40
OLD_SHA = "e" * 40
LAUNCH = "2027-01-15T08:01:00Z"


def land_module():
    """The part as the daemon runs it: its one outside name is ConfigError."""
    module = types.ModuleType("land_under_test")
    module.ConfigError = type("ConfigError", (Exception,), {})
    exec(compile((REPO / "bin" / "switchboard" / "land.py").read_text(), "land.py", "exec"), module.__dict__)
    return module


class TestReader(unittest.TestCase):
    def setUp(self):
        self.land = land_module()

    def test_s3_the_default_is_off_no_repository_lands_when_the_key_is_absent_or_null(self):
        for config in ({}, None, {"land_repos": None}):
            for repo in ("janus-infra/widget", "acme/other", "adamkaplan/fleet-kit"):
                self.assertFalse(self.land.uses_spire_land(config, repo))

    def test_s3_an_explicit_janus_infra_glob_turns_the_land_path_on_for_that_owner_only(self):
        config = {"land_repos": ["janus-infra/*"]}
        self.assertTrue(self.land.uses_spire_land(config, "janus-infra/widget"))
        self.assertFalse(self.land.uses_spire_land(config, "acme/other"))
        self.assertFalse(self.land.uses_spire_land(config, "adamkaplan/fleet-kit"))

    def test_s4_an_empty_list_puts_every_repository_back_on_pull_requests(self):
        self.assertFalse(self.land.uses_spire_land({"land_repos": []}, "janus-infra/widget"))

    def test_a_list_names_repositories_and_owner_globs_case_blind(self):
        config = {"land_repos": ["Acme/one", "beta/*"]}
        self.assertTrue(self.land.uses_spire_land(config, "acme/ONE"))
        self.assertTrue(self.land.uses_spire_land(config, "beta/anything"))
        self.assertFalse(self.land.uses_spire_land(config, "acme/two"))
        self.assertFalse(self.land.uses_spire_land(config, "janus-infra/widget"), "a repository the list does not name is not on the land path")

    def test_a_glob_or_a_malformed_name_is_never_a_repository(self):
        for repo in ("janus-infra/*", "janus-infra", "janus-infra/widget\n", None, 7, ""):
            self.assertFalse(self.land.uses_spire_land({}, repo), repr(repo))

    def test_a_bad_value_names_the_key(self):
        for bad in ("janus-infra/*", ["nowhere"], [3], {"a": 1}, ["a/b"] * 201):
            with self.assertRaises(self.land.ConfigError) as caught:
                self.land.validate_land_repos({"land_repos": bad}, "config.json")
            self.assertIn("land_repos", str(caught.exception))
        for good in ([], ["a/b", "c/*"]):
            self.land.validate_land_repos({"land_repos": good}, "config.json")


class TestConfigLoader(unittest.TestCase):
    def test_the_daemons_config_check_accepts_the_key_and_refuses_a_bad_one(self):
        sb = SourceFileLoader("sb_land_repos", str(REPO / "bin" / "fleet-switchboard")).load_module()
        self.assertEqual(sb.validate_config({"land_repos": []})["land_repos"], [])
        self.assertNotIn("land_repos", sb.validate_config({"land_repos": None}))
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"land_repos": "janus-infra/*"})
        self.assertIn("land_repos", str(caught.exception))


LAND_FAKE_GH = """#!/usr/bin/env python3
import json, sys
with open(%(log)r, "a") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["pr", "view"]:
    print(json.dumps({"number": 7, "state": "OPEN", "headRefOid": "a" * 40, "baseRefName": "main", "body": "no plan\\n", "title": "t", "author": {"login": "w"},
                      "isDraft": False, "files": [{"path": "a.py"}], "changedFiles": 1, "statusCheckRollup": [], "reviews": [], "labels": [],
                      "mergeable": "MERGEABLE", "baseRefOid": "b" * 40, "comments": []}))
    sys.exit(0)
sys.stderr.write("not found\\n")
sys.exit(1)
"""


LAND_ON = ["janus-infra/*"]   # the explicit switch: the code default is off
ABSENT = object()


class TestGatesSkipped(unittest.TestCase):
    """gates-check and merge-gate ask no gate for a land repository, and the same command on another repository, or with land_repos [], reads the pull request."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="land-repos-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(self.tmp), ignore_errors=True))
        self.log = self.tmp / "gh.log"
        gh = self.tmp / "gh"
        gh.write_text(LAND_FAKE_GH % {"log": str(self.log)})
        gh.chmod(0o755)
        self.gh = gh
        (self.tmp / "state").mkdir()

    def run_cmd(self, command, repo, land_repos=LAND_ON, *extra):
        config = {"gh": str(self.gh), "opencode": "/bin/true",
                  "plan_gates": {"repos": {"janus-infra/widget": {"mode": "shadow"}, "acme/other": {"mode": "shadow"}}}}
        if land_repos is not ABSENT:
            config["land_repos"] = land_repos
        cfg = self.tmp / "config"
        (cfg / "fleet-switchboard").mkdir(parents=True, exist_ok=True)
        (cfg / "fleet-switchboard" / "config.json").write_text(json.dumps(config))
        env = dict(os.environ, HOME=str(self.tmp), XDG_CONFIG_HOME=str(cfg), XDG_DATA_HOME=str(self.tmp / "data"), FLEET_SWITCHBOARD_STATE=str(self.tmp / "state"))
        if self.log.exists():
            self.log.unlink()
        args = [command, "%s#7" % repo] + (["--for", "merge"] if command == "gates-check" else []) + list(extra)
        done = subprocess.run([sys.executable, str(REPO / "bin" / "fleet-switchboard")] + args, capture_output=True, text=True, env=env, timeout=90)
        calls = [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []
        return done, calls

    def test_s3_gates_check_of_a_land_repository_allows_and_reads_nothing(self):
        done, calls = self.run_cmd("gates-check", "janus-infra/widget", LAND_ON, "--json")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(json.loads(done.stdout)["verdict"], "allow")
        self.assertIn("land_repos", json.loads(done.stdout)["reason"])
        self.assertEqual(calls, [], "no gate reads the pull request")

    def test_s3_control_the_same_check_of_another_repository_reads_the_pull_request(self):
        done, calls = self.run_cmd("gates-check", "acme/other", LAND_ON, "--json")
        self.assertTrue(any(c[:2] == ["pr", "view"] for c in calls), (calls, done.stdout, done.stderr))

    def test_s3_with_the_key_absent_the_repository_is_on_the_gates(self):
        done, calls = self.run_cmd("gates-check", "janus-infra/widget", ABSENT, "--json")
        self.assertTrue(any(c[:2] == ["pr", "view"] for c in calls), (calls, done.stdout, done.stderr))

    def test_s3_with_the_key_null_the_repository_is_on_the_gates(self):
        done, calls = self.run_cmd("gates-check", "janus-infra/widget", None, "--json")
        self.assertTrue(any(c[:2] == ["pr", "view"] for c in calls), (calls, done.stdout, done.stderr))

    def test_s4_with_land_repos_empty_the_repository_is_back_on_the_gates(self):
        done, calls = self.run_cmd("gates-check", "janus-infra/widget", [], "--json")
        self.assertTrue(any(c[:2] == ["pr", "view"] for c in calls), (calls, done.stdout, done.stderr))

    def test_s3_the_merge_door_of_a_land_repository_asks_nothing_and_another_repository_still_reads(self):
        done, calls = self.run_cmd("merge-gate", "janus-infra/widget")
        self.assertEqual((done.returncode, calls), (0, []), done.stdout + done.stderr)
        done, calls = self.run_cmd("merge-gate", "acme/other")
        self.assertTrue(calls, "the control repository's merge door reads GitHub")
        done, calls = self.run_cmd("merge-gate", "janus-infra/widget", [])
        self.assertTrue(calls, "land_repos [] puts it back")

    def test_merge_gate_pass_of_a_land_repository_is_refused_not_reported_as_done(self):
        done, calls = self.run_cmd("merge-gate", "janus-infra/widget", LAND_ON, "--pass", "--merge")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(calls, [])


class TestLandCompletion(Case):
    """The push watch: a Land-trailer commit on the default branch of a land repository completes the worker with the sha."""

    def setUp(self):
        super().setUp()
        self.reports, self.main = [], []
        self.engine.config = {"land_repos": ["janus-infra/*"]}
        self.engine.report = lambda sender, state, text, *a, **k: (self.reports.append((sender, state, text)), {"sent": True})[1]
        self.ns.is_land_repo = land_module().is_land_repo
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a", "janus-infra/widget")]}
        gh = self.world.__call__
        self.paths = []

        def world(path):
            self.paths.append(path)
            if "/commits?sha=" in path:
                return self.main
            return gh(path)
        self.world = world
        self.launcher_line(worker="w-a", action="create")   # LAUNCH

    @staticmethod
    def land(sha, message, date="2027-01-15T09:00:00Z"):
        return {"sha": sha, "commit": {"message": message, "committer": {"date": date}}}

    def test_s3_a_land_trailer_push_completes_the_worker_with_the_sha_and_no_gate_is_asked(self):
        self.main = [self.land(SHA, "Add the widget\n\nLanded-by: spire land")]
        self.run_pass(NOW)
        self.assertEqual(len(self.reports), 1)
        sender, state, text = self.reports[0]
        self.assertEqual((sender, state), ("w-a", "done"))
        self.assertIn(SHA[:12], text)
        self.assertFalse([p for p in self.paths if "/pulls" in p or "gate" in p], "no pull request or gate is read")
        self.run_pass(NOW + 60)
        self.assertEqual(len(self.reports), 1, "told once per sha")

    def test_s3_control_a_push_without_a_land_trailer_or_made_before_the_launch_completes_nothing(self):
        self.main = [self.land(SHA, "Add the widget\n\nSigned-off-by: someone"), self.land(OLD_SHA, "Old\n\nLanded-by: spire land", "2027-01-15T07:00:00Z")]
        self.run_pass(NOW)
        self.assertEqual(self.reports, [])

    def test_s3_the_same_push_in_another_repository_behaves_as_today(self):
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a", "acme/other")]}
        self.main = [self.land(SHA, "Add the widget\n\nLanded-by: spire land")]
        self.run_pass(NOW)
        self.assertEqual(self.reports, [])
        self.assertFalse([p for p in self.paths if "/commits?sha=" in p], "the default branch of a pull-request repository is not read")

    def test_s4_land_repos_empty_puts_the_repository_back_on_pull_requests(self):
        self.engine.config = {"land_repos": []}
        self.main = [self.land(SHA, "Add the widget\n\nLanded-by: spire land")]
        self.run_pass(NOW)
        self.assertEqual((self.reports, [p for p in self.paths if "/commits?sha=" in p]), ([], []))

    def test_u1_two_live_workers_in_one_repository_complete_only_the_one_the_message_names(self):
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a", "janus-infra/widget"), record("w-b", "lane-b", "janus-infra/widget")]}
        self.launcher_line(worker="w-b", action="create")
        self.main = [self.land(SHA, "Add the widget\n\nLand-worker: w-b")]
        self.run_pass(NOW)
        self.assertEqual([r[0] for r in self.reports], ["w-b"])
        self.assertTrue([l for l in self.engine.audit.lines if l[0] == "push_watch.land_ambiguous"])

    def test_a_failed_report_is_not_marked_and_is_tried_again(self):
        self.engine.report = lambda *a, **k: {"sent": False}
        self.main = [self.land(SHA, "x\n\nLanded-by: spire land")]
        self.run_pass(NOW)
        self.engine.report = lambda sender, state, text, *a, **k: (self.reports.append(sender), {"sent": True})[1]
        self.run_pass(NOW + 60)
        self.assertEqual(self.reports, ["w-a"])

    def test_an_unreadable_branch_is_audited_once(self):
        def down(path):
            if "/commits?sha=" in path:
                raise OSError("down")
            return []
        self.world = down
        self.run_pass(NOW)
        self.run_pass(NOW + 60)
        self.assertEqual(len([l for l in self.engine.audit.lines if l[0] == "push_watch.read_failed"]), 1)


    def test_a_trailer_quoted_in_the_body_is_not_a_landing(self):
        self.main = [self.land(SHA, "Discuss\n\nLanded-by: quoted\n\nSigned-off-by: someone")]
        self.run_pass(NOW)
        self.assertEqual(self.reports, [])

    def test_two_land_commits_of_one_read_are_one_completion(self):
        self.main = [self.land(SHA, "b\n\nLanded-by: x"), self.land("f" * 40, "a\n\nLanded-by: x")]
        self.run_pass(NOW)
        self.run_pass(NOW + 60)
        self.assertEqual(len(self.reports), 1)

    def test_a_worker_name_inside_a_longer_name_is_not_a_match(self):
        self.engine.cloud_watch = {"records": [record("w-a", "lane-a", "janus-infra/widget"), record("w-ab", "lane-b", "janus-infra/widget")]}
        self.launcher_line(worker="w-ab", action="create")
        self.main = [self.land(SHA, "x\n\nLand-worker: w-ab")]
        self.run_pass(NOW)
        self.assertEqual([r[0] for r in self.reports], ["w-ab"])

    def test_the_daemons_own_tick_gives_the_push_watch_the_land_reader(self):
        sb = SourceFileLoader("sb_land_tick", str(REPO / "bin" / "fleet-switchboard")).load_module()
        seen = []
        engine = types.SimpleNamespace(state_dir=None, hub=types.SimpleNamespace(gh_api=lambda *a, **k: []), config={"land_repos": ["janus-infra/*"]}, now=lambda: NOW, audit=None, actor="switchboard")
        real = SourceFileLoader("pw_probe", str(REPO / "bin" / "push_watch.py")).load_module()
        real_run = real.run_pass
        import importlib.util
        orig = importlib.util.spec_from_file_location

        def spy(name, path, *a, **k):
            spec = orig(name, path, *a, **k)
            if name == "push_watch":
                loader_exec = spec.loader.exec_module
                def exec_module(module):
                    loader_exec(module)
                    inner = module.run_pass
                    module.run_pass = lambda ns, *x, **y: (seen.append(ns), [])[1]
                spec.loader.exec_module = exec_module
            return spec
        importlib.util.spec_from_file_location = spy
        try:
            sb.push_watch_tick(engine, None)
        finally:
            importlib.util.spec_from_file_location = orig
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].is_land_repo({"land_repos": ["janus-infra/*"]}, "janus-infra/widget"))
        self.assertFalse(seen[0].is_land_repo({"land_repos": ["janus-infra/*"]}, "acme/other"))


class TestSweepSkip(unittest.TestCase):
    def test_sweep_refresh_does_not_read_a_land_repository_and_reads_another(self):
        sb = SourceFileLoader("sb_land_sweep", str(REPO / "bin" / "fleet-switchboard")).load_module()
        read = []
        sb.sweep_read_repo = lambda hub, repo, *a, **k: (read.append(repo), {"at": 0, "items": {}, "truncated": False, "error": None})[1]
        for config, expected in (({"land_repos": ["janus-infra/*"]}, ["acme/other"]), ({"land_repos": []}, ["acme/other", "janus-infra/widget"]), ({}, ["acme/other", "janus-infra/widget"])):
            read[:] = []
            engine = types.SimpleNamespace(hub=object(), config=config, now=lambda: NOW)
            state = types.SimpleNamespace(repos={}, started=NOW, strong=None)
            sb.sweep_refresh(engine, state, ["janus-infra/widget", "acme/other"], NOW, {"comment_window_days": 7}, None, [50])
            self.assertEqual(sorted(read), expected)


if __name__ == "__main__":
    unittest.main()
