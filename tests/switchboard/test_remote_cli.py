"""Tests of the remote-worker command line (remote_worker/cli.py): configuration, the token command, the worker record and the five commands,
through the real Floor carrier over an in-memory storage fake. No network, no real credential."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRESENT = (ROOT / "remote_worker").is_dir()   # absent in the mutation copies of test_brief_integration (bin, docs and tests only): the classes skip there
only_with_package = unittest.skipUnless(PRESENT, "remote_worker is not in this copy of the tree")
sys.path.insert(0, str(ROOT))

from support.core import Base
from test_remote_floor_carrier import FakeStore, Clock, sas_file, QSAS, CSAS, SHA

if PRESENT:
    from remote_worker import cli
    from remote_worker.floor import FloorTransport
    from remote_worker.launch import validate_spec

TOKEN = "tok_ShortLived0123456789"
TASK = "Fix the parser."
LAND = "acme-land --now"


class Done:
    def __init__(self, code=0, out=TOKEN + "\n"):
        self.returncode, self.stdout = code, out


@only_with_package
class TestRemoteCli(Base):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        d = self.dir.name
        self.store, self.clock = FakeStore(), Clock()
        self.state_dir = os.path.join(d, "state")
        self.cfg = {"backend": "floor", "land_command": LAND,
                    "floor": {"account": "examplestore", "floor_id": "f1", "caller_id": "fleet", "git_base": "https://git.example",
                              "queue_sas_file": sas_file(d, "q", QSAS), "container_sas_file": sas_file(d, "c", CSAS)},
                    "repo_token_command": ["mint-token", "--repo", "acme/widget-api"], "repo_token_env": "REPO_TOKEN", "state_dir": self.state_dir}
        self.calls = []
        self.token_result = Done()

    def runner(self, argv, **kw):
        self.calls.append((argv, kw))
        if isinstance(self.token_result, Exception):
            raise self.token_result
        return self.token_result

    def backends(self):
        def make(s):
            return FloorTransport(account=s["account"], floor_id=s["floor_id"], caller_id=s["caller_id"], queue_sas_file=s["queue_sas_file"],
                                  container_sas_file=s["container_sas_file"], git_base=s["git_base"], http=self.store, clock=self.clock,
                                  wall=self.clock, min_read_seconds=0)
        return {"floor": make}

    def write_cfg(self, cfg=None, name="config.json"):
        p = os.path.join(self.dir.name, name)
        with open(p, "w") as f:
            json.dump(self.cfg if cfg is None else cfg, f)
        return p

    def call(self, *argv, stdin="", cfg=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["--config", self.write_cfg(cfg), *argv], environ={}, stdin=io.StringIO(stdin), stdout=out, stderr=err,
                        backends=self.backends(), runner=self.runner)
        return code, out.getvalue(), err.getvalue()

    def launch(self, name="widget-1"):
        task = os.path.join(self.dir.name, "task.txt")
        with open(task, "w") as f:
            f.write(TASK)
        return self.call("launch", name, "--repo", "acme/widget-api", "--branch", "work/widget-1", "--task-file", task)

    # ---- launch ----
    def test_launch_sends_the_launch_command_with_the_token_in_env_only(self):
        code, out, err = self.launch()
        self.assertEqual((code, err), (0, ""))
        result = json.loads(out)
        self.assertEqual((result["name"], result["state"]), ("widget-1", "queued"))
        cmd = self.store.messages[0]
        self.assertEqual(cmd["env"], {"REPO_TOKEN": TOKEN})
        self.assertIn(TASK, cmd["brief"])
        self.assertIn(LAND, cmd["brief"])
        self.assertNotIn(TOKEN, cmd["brief"])
        self.assertNotIn(TOKEN, out + err)

    def test_the_token_is_never_stored_in_the_record_or_the_state_directory(self):
        self.launch()
        for entry in os.listdir(self.state_dir):
            self.assertNotIn(TOKEN, open(os.path.join(self.state_dir, entry)).read())
        rec = json.load(open(os.path.join(self.state_dir, "widget-1.json")))
        self.assertEqual((rec["next_seq"], rec["repo"], rec["branch"]), (2, "acme/widget-api", "work/widget-1"))
        self.assertEqual(oct(os.stat(os.path.join(self.state_dir, "widget-1.json")).st_mode & 0o777), "0o600")

    def test_the_token_command_runs_once_without_a_shell_and_with_a_limit(self):
        self.launch()
        (argv, kw), = self.calls
        self.assertEqual(argv, ["mint-token", "--repo", "acme/widget-api"])
        self.assertIs(kw["shell"], False)
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertGreater(kw["timeout"], 0)

    def test_a_config_without_a_token_command_sends_no_env(self):
        cfg = {k: v for k, v in self.cfg.items() if k != "repo_token_command"}
        task = os.path.join(self.dir.name, "t.txt")
        open(task, "w").write(TASK)
        code, _, _ = self.call("launch", "widget-1", "--repo", "acme/widget-api", "--branch", "b", "--task-file", task, cfg=cfg)
        self.assertEqual(code, 0)
        self.assertNotIn("env", self.store.messages[0])
        self.assertEqual(self.calls, [])

    def test_a_failing_token_command_refuses_the_launch_before_any_call_and_names_no_output(self):
        for result in (Done(1, TOKEN), Done(0, ""), Done(0, "has space in it"), Done(0, "x" * 5000), subprocess.TimeoutExpired("x", 30), FileNotFoundError(TOKEN)):
            with self.subTest(result=type(result).__name__):
                self.token_result = result
                self.store.requests.clear()
                code, out, err = self.launch()
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertNotIn(TOKEN, err)
                self.assertEqual(self.store.requests, [])

    def test_stdin_task_and_the_size_limit(self):
        code, _, _ = self.call("launch", "widget-1", "--repo", "acme/widget-api", "--branch", "b", "--task-file", "-", stdin=TASK)
        self.assertEqual(code, 0)
        self.assertIn(TASK, self.store.messages[0]["brief"])
        code, _, err = self.call("launch", "widget-2", "--repo", "acme/widget-api", "--branch", "b", "--task-file", "-", stdin="x" * 100_001)
        self.assertEqual(code, 1)

    def test_bad_inputs_are_refused_with_one_line_and_no_call(self):
        task = os.path.join(self.dir.name, "t.txt")
        open(task, "w").write(TASK)
        for extra in (["--idle", "abc"], ["--hard", "-1"]):
            code, _, err = self.call("launch", "widget-1", "--repo", "acme/widget-api", "--branch", "b", "--task-file", task, *extra)
            self.assertEqual(code, 1, extra)
        code, _, err = self.call("launch", "Bad Name", "--repo", "acme/widget-api", "--branch", "b", "--task-file", task)
        self.assertEqual(code, 1)
        code, _, err = self.call("launch", "widget-1", "--repo", "nope", "--branch", "b", "--task-file", task)
        self.assertEqual(code, 1)
        self.assertEqual(self.store.requests, [])

    # ---- the other commands, each in its own process ----
    def test_status_last_message_send_and_retire_use_the_recorded_numbering(self):
        self.launch()
        sid = json.load(open(os.path.join(self.state_dir, "widget-1.json")))["worker_id"]
        self.store.publish({sid: {"state": "running", "busy": True, "last_update": "2026-11-12T09:30:12Z"}}, written_at="2026-11-12T09:30:12Z")
        code, out, _ = self.call("status", "widget-1")
        self.assertEqual(code, 0)
        st = json.loads(out)
        self.assertEqual((st["state"], st["busy"], st["last_update"]), ("running", True, "2026-11-12T09:30:12Z"))
        self.store.blobs["msg/%s.json" % sid] = json.dumps({"v": 1, "text": "landed " + SHA}).encode()
        self.assertEqual(json.loads(self.call("last-message", "widget-1")[1])["text"], "landed " + SHA)
        self.assertEqual(self.call("send", "widget-1", "--text-file", "-", stdin="please also update the README")[0], 0)
        self.assertEqual(self.call("retire", "widget-1")[0], 0)
        self.assertEqual([(c["op"], c["seq"]) for c in self.store.messages], [("launch", 1), ("send", 2), ("retire", 3)])
        self.assertEqual(json.load(open(os.path.join(self.state_dir, "widget-1.json")))["next_seq"], 4)

    def test_a_failed_send_records_nothing_and_its_number_is_reused(self):
        self.launch()
        self.store.fail = [503]
        code, _, err = self.call("send", "widget-1", "--text-file", "-", stdin="x")
        self.assertEqual(code, 1)
        self.assertIn("retryable", err)
        self.assertEqual(json.load(open(os.path.join(self.state_dir, "widget-1.json")))["next_seq"], 2)   # nothing was recorded: the number is reused
        self.assertEqual(self.call("send", "widget-1", "--text-file", "-", stdin="x")[0], 0)
        self.assertEqual([c["seq"] for c in self.store.messages], [1, 2])

    def test_a_repeated_launch_keeps_the_recorded_numbering(self):
        self.launch()
        self.call("send", "widget-1", "--text-file", "-", stdin="x")
        self.launch()
        self.assertEqual(json.load(open(os.path.join(self.state_dir, "widget-1.json")))["next_seq"], 3)
        a, c = self.store.messages[0], self.store.messages[-1]
        self.assertEqual((a["cmd_id"], a["seq"]), (c["cmd_id"], c["seq"]))   # the repeat is the same launch: the service answers `duplicate`

    def test_a_worker_with_no_record_is_refused(self):
        for command in (["status"], ["last-message"], ["retire"], ["send", "--text-file", "-"]):
            code, out, err = self.call(*command[:1], "ghost", *command[1:], stdin="x")
            self.assertEqual((code, out), (1, ""), command)
            self.assertIn("no record", err)

    def test_the_record_rules(self):
        self.launch()
        path = os.path.join(self.state_dir, "widget-1.json")
        for text in ("not json", json.dumps({"name": "other", "worker_id": "ses_0123456789abcdef", "next_seq": 2}),
                     json.dumps({"name": "widget-1", "worker_id": "ses_0123456789abcdef", "next_seq": 1}),
                     json.dumps({"name": "widget-1", "worker_id": "ses_0123456789abcdef", "next_seq": True}), "x" * 5000):
            open(path, "w").write(text)
            self.assertEqual(self.call("status", "widget-1")[0], 1, text[:20])
        os.unlink(path)
        os.symlink(os.path.join(self.dir.name, "config.json"), path)
        self.assertEqual(self.call("status", "widget-1")[0], 1)

    # ---- configuration ----
    def test_configuration_rules(self):
        base = json.loads(json.dumps(self.cfg))
        def without(*keys):
            return {k: v for k, v in base.items() if k not in keys}
        bad = [without("backend"), without("land_command"), {**base, "land_command": "  "}, {**base, "land_command": "a\nb"}, without("floor"),
               {**base, "floor": {**base["floor"], "account": ""}}, {**base, "repo_token_command": []}, {**base, "repo_token_command": "mint"},
               {**base, "repo_token_command": ["a", 3]}, {**base, "repo_token_env": "lower"}, {**base, "state_dir": "relative"}, {**base, "backend": "nope"}]
        for cfg in bad:
            with self.subTest(cfg=sorted(cfg)):
                code, out, err = self.call("status", "widget-1", cfg=cfg)
                self.assertEqual((code, out), (1, ""))
                self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(self.store.requests, [])

    def test_no_configuration_or_an_unreadable_one(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(cli.main(["status", "widget-1"], environ={}, stdout=out, stderr=err, stdin=io.StringIO(), backends={}), 1)
        self.assertIn(cli.ENV_CONFIG, err.getvalue())
        p = os.path.join(self.dir.name, "junk.json")
        open(p, "w").write("{")
        err = io.StringIO()
        self.assertEqual(cli.main(["status", "widget-1"], environ={cli.ENV_CONFIG: p}, stdout=out, stderr=err, stdin=io.StringIO(), backends={}), 1)
        self.assertEqual(cli.main(["status", "widget-1"], environ={cli.ENV_CONFIG: p + ".missing"}, stdout=out, stderr=io.StringIO(), stdin=io.StringIO(), backends={}), 1)

    def test_usage_error_exits_2(self):
        self.assertEqual(cli.main(["nonsense"], environ={}, stdout=io.StringIO(), stderr=io.StringIO(), stdin=io.StringIO()), 2)

    def test_the_land_command_comes_only_from_configuration(self):
        self.launch()
        self.assertIn(LAND, self.store.messages[0]["brief"])
        cfg = dict(self.cfg, land_command="other-land --go")
        task = os.path.join(self.dir.name, "t.txt")
        open(task, "w").write(TASK)
        self.call("launch", "widget-2", "--repo", "acme/widget-api", "--branch", "b", "--task-file", task, cfg=cfg)
        self.assertIn("other-land --go", self.store.messages[-1]["brief"])
        self.assertNotIn(LAND, self.store.messages[-1]["brief"])

    def test_the_env_of_a_spec_is_never_in_its_repr(self):
        spec = validate_spec("widget-1", "acme/widget-api", "b", "t", LAND, env=(("REPO_TOKEN", TOKEN),))
        self.assertNotIn(TOKEN, repr(spec))
        self.assertEqual(spec.env, (("REPO_TOKEN", TOKEN),))

    def test_env_rules(self):
        from remote_worker.launch import SpecError
        for env in ((("lower", "x"),), (("A", ""),), (("A", "a\nb"),), (("A", "x"), ("A", "y")), tuple(("V%d" % i, "x") for i in range(17)), (("A", "x" * 5000),)):
            with self.subTest(env=env[:1]):
                with self.assertRaises(SpecError) as cm:
                    validate_spec("widget-1", "acme/widget-api", "b", "t", LAND, env=env)
                self.assertNotIn("x" * 50, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
