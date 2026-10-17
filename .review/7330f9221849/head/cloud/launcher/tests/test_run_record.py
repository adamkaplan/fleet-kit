"""Slice S3 (#638): the run record (S5, U2) and the report path (S6). Offline: the fake kubectl and the stub server."""
import base64
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_driver as td  # noqa: E402  (the module, not its test class: it is not collected twice)
cw = td.cw

PLANTED = td.MODEL_VALUES + [td.GIT]


def planted_forms():
    out = []
    for v in PLANTED:
        raw = v.encode()
        out += [v, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("="), raw.hex(),
                urllib.parse.quote(v, safe=""), json.dumps(v)[1:-1]]
    return out


def messages():
    leak = " ".join(planted_forms())
    return [{"type": "user", "text": "Add a file named hello.txt. " + leak},
            {"type": "assistant", "time": {"created": 1, "completed": 2}, "content": [
                {"type": "text", "text": "echo " + leak},
                {"type": "tool", "tool": "bash", "state": {"output": leak}},
                {"type": "tool", "tool": "bash", "state": {}},
                {"type": "tool", "tool": "edit", "state": {}}]},
            {"type": "assistant", "time": {"created": 3, "completed": 4}, "content": [{"type": "text", "text": "done"}]}]


class Record(td.Driver):
    # A short fixed base: no assertion here may depend on the runner's temp-path shape (a long token-shaped
    # segment, as under macOS /var/folders, is redacted by the launcher's scrub in audit and screen text).
    TMP_BASE = "/tmp" if os.path.isdir("/tmp") else None

    def shown(self, path):
        """The path as audit and screen text carry it: through the launcher's own scrub."""
        return cw.scrub(path, limit=10 ** 6)

    def setUp(self):
        super().setUp()
        self.records = os.path.join(self.tmp.name, "records")
        self.msgfile = os.path.join(self.tmp.name, "messages.json")
        with open(self.msgfile, "w") as f:
            json.dump(messages(), f)
        self.denv.update(CW_RUN_RECORD_DIR=self.records, STUB_MESSAGES=self.msgfile)

    def run_dirs(self):
        return sorted(os.listdir(self.records)) if os.path.isdir(self.records) else []

    def the_record(self):
        dirs = self.run_dirs()
        self.assertEqual(len(dirs), 1, dirs)
        return os.path.join(self.records, dirs[0])

    def all_record_text(self):
        d = self.the_record()
        return "\n".join(self.read(os.path.join(d, n)) for n in sorted(os.listdir(d)))

    def assert_lines_are_json(self, lines):
        """M2: the scrub works on the string values, so every line is still the JSON message it was."""
        parsed = [json.loads(x) for x in lines]   # raises when the scrub broke the structure
        self.assertEqual([m["type"] for m in parsed[:3]], ["user", "assistant", "assistant"])
        self.assertEqual(parsed[1]["content"][1]["tool"], "bash")
        self.assertIn("state", parsed[1]["content"][1])
        self.assertEqual(parsed[1]["time"], {"created": 1, "completed": 2})

    def assert_record_has_no_secret(self):
        text = self.all_record_text()
        for form in planted_forms():
            self.assertNotIn(form, text)
        self.assert_no_secret(text, "the record")

    # -- S5: the record survives the pod --
    def test_s5_after_the_stop_deletes_the_pod_the_record_holds_a_transcript_and_a_tally_with_no_secret(self):
        self.check_s5_after_the_stop()

    def check_s5_after_the_stop(self):
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.st()["pods"], {})
        d = self.the_record()
        self.assertEqual(sorted(os.listdir(d)), ["run.json", "tally.json", "transcript.jsonl"])
        self.assertEqual(os.stat(self.records).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(d).st_mode & 0o777, 0o700)
        for n in os.listdir(d):
            self.assertEqual(os.stat(os.path.join(d, n)).st_mode & 0o777, 0o600, n)
        lines = self.read(os.path.join(d, "transcript.jsonl")).splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("hello.txt", lines[0])
        self.assert_lines_are_json(lines)
        self.assertIn("[redacted]", self.all_record_text())
        tally = json.loads(self.read(os.path.join(d, "tally.json")))
        self.assertEqual(tally, {"messages": 3, "tool_calls": {"bash": 2, "edit": 1}, "turns": 2})
        info = json.loads(self.read(os.path.join(d, "run.json")))
        self.assertEqual((info["transcript"], info["worker"], info["repository"], info["exit_reason"]), ("saved", "coder7", td.REPO, "task done"))
        self.assertTrue(info["start"] and info["end"])
        self.assert_record_has_no_secret()
        self.assert_clean(out)
        self.assertEqual(os.path.dirname(d), self.records)   # in the configured directory, not another
        self.assertEqual(out.count(self.shown(d)), 1, "named once on screen")
        self.assertEqual(self.read(self.env["CW_CLOUD_AUDIT"]).count(self.shown(d)), 1)

    def test_the_head_sha_is_kept_when_the_pod_can_say_it(self):
        self.patch_state(head_sha="a" * 40)
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(json.loads(self.read(os.path.join(self.the_record(), "run.json")))["head_sha"], "a" * 40)

    def test_s5_a_transcript_that_cannot_be_read_still_stops_the_pod_and_says_unavailable(self):
        p, out = self.run_driver(STUB_MODE="messages-down", CW_RECORD_TIMEOUT="3")
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.st()["pods"], {})
        d = self.the_record()
        self.assertEqual(os.listdir(d), ["run.json"])
        info = json.loads(self.read(os.path.join(d, "run.json")))
        self.assertEqual(info["transcript"], "unavailable")
        self.assertIn("status 500", info["reason"])
        self.assert_clean(out)

    def test_s5_an_unreachable_server_is_unavailable_and_the_stop_still_runs(self):
        p, out = self.run_driver(STUB_MODE="never-healthy", CW_HEALTH_TIMEOUT="1", CW_RECORD_TIMEOUT="1")
        self.assertEqual(p.returncode, cw.EXIT_CLOUD, out)
        info = json.loads(self.read(os.path.join(self.the_record(), "run.json")))
        self.assertEqual(info["transcript"], "unavailable")
        self.assert_clean(out)

    def test_a_record_directory_that_cannot_be_made_never_keeps_the_pod(self):
        blocker = os.path.join(self.tmp.name, "blocker")
        open(blocker, "w").close()
        p, out = self.run_driver(CW_RUN_RECORD_DIR=os.path.join(blocker, "x"))
        self.assertEqual(p.returncode, 0, out)
        self.assertEqual(self.st()["pods"], {})
        self.assertNotIn("run record:", out)
        self.assertEqual(os.listdir(self.tmp.name).count("blocker"), 1)
        self.assertTrue(os.path.isfile(blocker))
        self.assert_clean(out)

    def test_s5_an_interrupt_still_writes_the_record(self):
        rc, out = self.interrupt(signal.SIGINT, lambda: "prompt" in self.read(self.stub_log), STUB_MODE="busy-forever")
        self.assertEqual(rc, 130, out)
        d = self.the_record()
        self.assertEqual(sorted(os.listdir(d)), ["run.json", "tally.json", "transcript.jsonl"])
        self.assertEqual(json.loads(self.read(os.path.join(d, "run.json")))["exit_reason"], "driver ended early")
        self.assert_record_has_no_secret()
        self.assert_clean(out)

    def test_a_pod_kept_because_the_hook_failed_still_has_its_record(self):
        self.patch_state(hook_result={"pushed": False, "error": "push refused by the remote"})
        p, out = self.run_driver()
        self.assertEqual(p.returncode, cw.EXIT_HOOK, out)
        self.assertEqual(sorted(os.listdir(self.the_record())), ["run.json", "tally.json", "transcript.jsonl"])
        self.assert_clean(out, kept=True)

    def test_a_transcript_over_the_cap_is_cut_with_a_loud_marker(self):
        p, out = self.run_driver(CW_RECORD_MAX_BYTES="300")
        self.assertEqual(p.returncode, 0, out)
        d = self.the_record()
        lines = self.read(os.path.join(d, "transcript.jsonl")).splitlines()
        self.assertIn("TRANSCRIPT CUT", lines[-1])
        self.assertLessEqual(sum(len(x) + 1 for x in lines[:-1]), 300)
        self.assertTrue(json.loads(self.read(os.path.join(d, "run.json")))["transcript_truncated"])
        self.assert_record_has_no_secret()

    def test_a_message_over_the_line_limit_is_replaced_by_a_marker_and_the_record_says_cut(self):
        msgs = messages()
        msgs[0]["text"] = "x " * 150000
        with open(self.msgfile, "w") as f:
            json.dump(msgs, f)
        p, out = self.run_driver()
        self.assertEqual(p.returncode, 0, out)
        d = self.the_record()
        lines = self.read(os.path.join(d, "transcript.jsonl")).splitlines()
        self.assertIn("MESSAGE CUT", lines[0])
        self.assertTrue(json.loads(self.read(os.path.join(d, "run.json")))["transcript_truncated"])
        self.assertEqual([n for n in os.listdir(d) if n.endswith(".part")], [])

    def test_the_default_record_directory_is_beside_the_audit_file(self):
        env = dict(self.denv)
        del env["CW_RUN_RECORD_DIR"]
        import subprocess
        p = subprocess.run(self.args(), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "runs")))

    def test_each_run_has_its_own_directory(self):
        for _ in range(2):
            self.run_driver()
        self.assertEqual(len(self.run_dirs()), 2)

    # -- M3: the record is the launcher's stop's: retire and a person get it too --
    def stub_env(self, **extra):
        self.env.update(CW_RUN_RECORD_DIR=self.records, STUB_MESSAGES=self.msgfile, STUB_LOG=self.stub_log, CW_RECORD_TIMEOUT="5")
        self.env.update(extra)

    def create_with_secrets(self):
        """A worker made by the launcher with the model entry and the git token as run secrets, as the driver makes it."""
        import subprocess
        pw_r, pw_w = os.pipe()
        fds = []
        args = ["create", "coder7", "--repo", td.REPO, "--branch", "main", "--password-fd", str(pw_w)]
        for name, value in (("model-auth", json.dumps({"github-copilot": td.ENTRY})), ("git-token", td.GIT)):
            r, w = os.pipe()
            os.write(w, value.encode()); os.close(w)
            fds.append(r)
            args += ["--run-secret-fd", "%s=%d" % (name, r)]
        env = dict(os.environ, **self.env)
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "cloud_worker.py")] + args, env=env,
                           capture_output=True, text=True, pass_fds=fds + [pw_w], timeout=120)
        os.close(pw_w)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_s5_the_launchers_stop_alone_writes_the_record_so_retire_gets_one(self):
        self.check_stop_alone()

    def check_stop_alone(self):
        self.stub_env()
        self.create_with_secrets()
        code, out, err = self.run_cli("stop", "coder7", "--reason", "retired")
        self.assertEqual(code, 0, err)
        d = self.the_record()
        self.assertEqual(sorted(os.listdir(d)), ["run.json", "tally.json", "transcript.jsonl"])
        self.assertEqual(self.st()["pods"], {})
        info = json.loads(self.read(os.path.join(d, "run.json")))
        self.assertEqual((info["transcript"], info["exit_reason"], info["repository"]), ("saved", "retired", td.REPO))
        self.assertEqual(os.path.dirname(d), self.records)   # in the configured directory, not another
        self.assertEqual(json.loads(out)["record"], d)
        self.assertEqual(err.count(d), 1)
        self.assertEqual(self.audit_lines()[-1]["reason"], "retired; run record %s" % self.shown(d))
        lines = self.read(os.path.join(d, "transcript.jsonl")).splitlines()
        self.assert_lines_are_json(lines)
        self.assert_record_has_no_secret()   # the launcher learned the secrets from the pod's own mounts

    def test_no_record_and_a_dir_flag_are_honoured(self):
        self.stub_env()
        self.create_with_secrets()
        code, out, err = self.run_cli("stop", "coder7", "--no-record")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_dirs(), [])
        self.create_with_secrets()
        other = os.path.join(self.tmp.name, "elsewhere")
        code, out, err = self.run_cli("stop", "coder7", "--record-dir", other)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(os.listdir(other)), 1)

    # -- the record phase has ONE deadline, and the stop always reaches the delete --
    def check_timed_out(self, started, limit, **env):
        p, out = self.run_driver(**env)
        took = time.monotonic() - started
        self.assertLess(took, limit, "the stop took %.1f s" % took)
        self.assertEqual(self.st()["pods"], {}, "the pod must be deleted")
        info = json.loads(self.read(os.path.join(self.the_record(), "run.json")))
        self.assertEqual(info["transcript"], "unavailable")
        self.assertIn("timed out", info["reason"])
        self.assert_clean(out)

    def test_a_server_that_drips_one_byte_at_a_time_cannot_keep_the_pod_past_the_deadline(self):
        t = time.monotonic()
        self.check_timed_out(t, 25, STUB_DRIP="1", CW_RECORD_SECONDS="3", CW_RECORD_TIMEOUT="30")

    def test_kubectl_reads_that_each_hang_near_their_limit_are_bounded_by_the_one_deadline(self):
        t = time.monotonic()   # ls + cat + git each sleep 2 s: the sum (6 s+) is over the 3 s deadline
        self.check_timed_out(t, 25, FAKE_KUBE_EXEC_SLEEP="2", CW_RECORD_SECONDS="3")

    def test_the_record_deadline_default_maximum_and_bad_values(self):
        rr = cw.run_record
        self.assertEqual(rr.record_seconds({}), 90.0)
        self.assertEqual(rr.record_seconds({"CW_RECORD_SECONDS": "5"}), 5.0)
        self.assertEqual(rr.record_seconds({"CW_RECORD_SECONDS": "100000"}), 240.0)
        for bad in ("0", "-3", "nan", "x"):
            self.assertEqual(rr.record_seconds({"CW_RECORD_SECONDS": bad}), 90.0, bad)

    def test_every_kubectl_call_is_given_only_the_time_that_is_left(self):
        rr = cw.run_record
        now = [100.0]
        seen = []

        class K:
            def exec(self, pod, *cmd, timeout=120, **kw):
                seen.append(timeout)
                now[0] += timeout   # each call hangs to the very limit it was given
                raise cw.CloudError("kubectl failed: timed out")
        rec = rr.Recorder(K(), {}, clock=lambda: now[0])
        rec.deadline = now[0] + 100
        with self.assertRaises(rr.RecordTimeout):
            rec._learn_secrets("p")
        self.assertEqual(seen, [100.0])
        self.assertLessEqual(sum(seen), 100.0)
        rec.deadline = now[0] + 100
        seen.clear()
        self.assertIsNone(rec.head_sha("p"))
        self.assertLessEqual(sum(seen), 100.0)
        self.assertTrue(all(x <= 120 for x in seen))

    # -- M1: a hostile body never raises out of record() and never keeps the pod --
    def hostile(self, body):
        raw = os.path.join(self.tmp.name, "raw.json")
        with open(raw, "wb") as f:
            f.write(body)
        return raw

    def check_hostile(self, body, via_driver):
        raw = self.hostile(body)
        if via_driver:
            p, out = self.run_driver(STUB_MESSAGES_RAW=raw, CW_RECORD_TIMEOUT="5")
            self.assertEqual(p.returncode, 0, out)
        else:
            self.stub_env(STUB_MESSAGES_RAW=raw)
            self.create_with_secrets()
            open(self.stub_log + ".m", "w").write("x")   # the first message read is the driver's poll: the record's is the second
            code, out, err = self.run_cli("stop", "coder7")
            self.assertEqual(code, 0, err)
        self.assertEqual(self.st()["pods"], {}, "the pod must be stopped")
        info = json.loads(self.read(os.path.join(self.the_record(), "run.json")))
        self.assertEqual(info["transcript"], "unavailable")
        self.assertTrue(info["reason"])

    def test_m1_a_body_nested_1100_5000_and_100000_levels_is_unavailable_and_the_pod_is_stopped(self):
        for depth in (1100, 5000, 100000):
            with self.subTest(depth=depth):
                self.setUp()
                self.check_hostile(b'{"data":' + b"[" * depth + b"]" * depth + b"}", via_driver=False)

    def test_m1_the_failure_path_through_the_driver_still_stops_the_pod(self):
        self.check_hostile(b'{"data":' + b"[" * 100000 + b"]" * 100000 + b"}", via_driver=True)

    def test_m1_a_50_megabyte_body_is_refused_unread(self):
        self.check_hostile(b"x" * (50 * 1024 * 1024), via_driver=False)

    def test_m1_depth_is_checked_without_parsing_and_quotes_do_not_fool_it(self):
        rr = cw.run_record
        self.assertTrue(rr.depth_ok(b"[" * 100 + b"]" * 100))
        self.assertFalse(rr.depth_ok(b"[" * 101 + b"]" * 101))
        self.assertTrue(rr.depth_ok(b'{"a": "' + b"[" * 5000 + b'\\", "b": "\"[[[["}'))   # brackets inside strings do not count
        deep = cur = []
        for _ in range(100000):
            nxt = []
            cur.append(nxt)
            cur = nxt
        cur.append("leaf")
        names = []
        out = rr.walk_scrub({"type": "tool", "tool": "bash", "x": deep}, str.upper, names)   # no RecursionError
        self.assertEqual(names, ["bash"])
        self.assertIsInstance(out["X"], list)

    # -- lows --
    def test_a_record_directory_that_is_a_symbolic_link_is_refused_and_not_chmodded(self):
        target = os.path.join(self.tmp.name, "target")
        os.mkdir(target, 0o755)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(target, link)
        for how in ("env", "flag"):
            self.setUp() if how == "flag" else None
            self.stub_env(CW_RUN_RECORD_DIR=link) if how == "env" else self.stub_env()
            self.create_with_secrets()
            args = ["stop", "coder7"] + (["--record-dir", link] if how == "flag" else [])
            code, out, err = self.run_cli(*args)
            self.assertEqual(code, 0, err)
            self.assertEqual(self.st()["pods"], {})
            self.assertEqual(os.listdir(target), [], how)
            self.assertEqual(os.stat(target).st_mode & 0o777, 0o755, how)
            self.assertNotIn("run record:", err)

    def test_the_scrub_output_limit_is_above_the_input_maximum(self):
        self.assertGreater(cw.SCRUB_OUTPUT_MAX, cw.SCRUB_INPUT_MAX)
        secret = "Zk9Wp3Zk9Wp3Zk9Wp3Zk9Wp3"
        grown = (secret[:8] + " ") * (cw.SCRUB_INPUT_MAX // 9)   # every 8-character piece becomes the 10-character marker
        out = cw.scrub(grown, (secret,), limit=cw.SCRUB_OUTPUT_MAX)
        self.assertGreater(len(out), cw.SCRUB_INPUT_MAX)
        self.assertEqual(out.count("[redacted]"), cw.SCRUB_INPUT_MAX // 9)
        self.assertNotIn(secret[:8], out)

    def test_a_key_that_is_a_secret_is_scrubbed_too(self):
        self.stub_env()
        msgs = [{"type": "user", "text": "x", td.MODEL_VALUES[1]: "v"}]
        with open(self.msgfile, "w") as f:
            json.dump(msgs, f)
        self.create_with_secrets()
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 0, err)
        self.assert_record_has_no_secret()

    def test_prune_removes_only_old_per_run_directories_of_this_tool(self):
        root = self.records
        os.makedirs(root, mode=0o700)
        old = time.time() - 40 * 86400

        def make(name, files=("run.json",), age=40):
            p = os.path.join(root, name)
            os.mkdir(p)
            for f in files:
                open(os.path.join(p, f), "w").write("{}")
            t = time.time() - age * 86400
            os.utime(p, (t, t))
            return p
        old_run = make("coder7-20260101T000000Z-abc123", ("run.json", "tally.json", "transcript.jsonl"))
        new_run = make("coder7-20260901T000000Z-def456", age=1)
        foreign = make("my-photos", ("a.jpg",))
        foreign_named = make("coder7-20260101T000000Z-aaaaaa", ("notes.txt",))   # named like a run, holds a foreign file
        stray = os.path.join(root, "coder7-20260101T000000Z-bbbbbb")
        outside = os.path.join(self.tmp.name, "outside")
        os.mkdir(outside)
        os.symlink(outside, stray)
        os.utime(stray, (old, old), follow_symlinks=False)
        open(os.path.join(root, "audit.jsonl"), "w").write("x")
        self.stub_env()
        code, out, err = self.run_cli("prune-records", "--days", "30")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["removed"], ["coder7-20260101T000000Z-abc123"])
        self.assertFalse(os.path.exists(old_run))
        for keep in (new_run, foreign, foreign_named, stray, outside, os.path.join(root, "audit.jsonl")):
            self.assertTrue(os.path.lexists(keep), keep)
        link = os.path.join(self.tmp.name, "rootlink")
        os.symlink(root, link)
        self.stub_env(CW_RUN_RECORD_DIR=link)
        code, out, err = self.run_cli("prune-records")
        self.assertEqual(code, cw.EXIT_USAGE)

    def test_s6_a_report_that_is_a_link_or_not_a_regular_file_is_refused_and_never_read(self):
        self.create("coder7")
        for kind in ("symlink", "dir"):
            self.patch_state(report_fs={self.name: {"type": kind, "target": "SECRET-" + "Qw8Er5" * 6}})
            code, out, err = self.run_cli("report", "coder7")
            self.assertEqual(code, cw.EXIT_CLOUD, kind)
            self.assertIn("not a regular file", err)
            self.assertNotIn("SECRET-", out + err)

    # -- S6: the report, with no switchboard command in the pod --
    def test_s6_report_returns_the_one_line_the_worker_wrote_and_runs_nothing_but_head(self):
        self.create("coder7")
        planted = "ghp_" + "Ab12" * 9   # token-shaped: a standalone report does not know the run's own secrets
        self.patch_state(report={self.name: "Opened PR 12; tests pass.\nsecond line %s\n" % planted})
        code, out, err = self.run_cli("report", "coder7")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "Opened PR 12; tests pass. second line [redacted]")
        self.assertEqual(len(out.strip().splitlines()), 1)
        execs = [json.loads(x) for x in self.read(self.env["FAKE_KUBE_CALLS"]).splitlines() if '"exec"' in x]
        used = [c[c.index("--") + 1] for c in execs]
        self.assertEqual(used, ["sh"])   # one read-only shell script whose only reader is head
        self.assertIn("head -c", execs[0][execs[0].index("-c") + 1])
        self.assertFalse([c for c in execs if "switchboard" in " ".join(c)])
        self.assertEqual(self.audit_lines()[-1]["action"], "report")

    def test_s6_a_long_report_is_cut_to_2000_characters(self):
        self.create("coder7")
        self.patch_state(report={self.name: "word " * 2000})
        code, out, err = self.run_cli("report", "coder7")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.rstrip("\n")), 2000)

    def test_s6_a_missing_or_unknown_worker_says_so(self):
        self.create("coder7")
        code, out, err = self.run_cli("report", "coder7")
        self.assertEqual(code, cw.EXIT_CLOUD)
        self.assertIn("no report yet", err)
        code, out, err = self.run_cli("report", "nobody")
        self.assertEqual(code, cw.EXIT_USAGE)

    def test_s6_a_cluster_error_is_not_called_a_missing_report_and_json_gives_an_object(self):
        self.create("coder7")
        self.patch_state(report={self.name: "done\n"})
        code, out, err = self.run_cli("report", "coder7", "--json")
        self.assertEqual(json.loads(out)["report"], "done")
        self.patch_state(fail_cmd="exec")
        code, out, err = self.run_cli("report", "coder7")
        self.assertEqual(code, cw.EXIT_CLOUD)
        self.assertNotIn("no report yet", err)
        self.assertIn("kubectl", err)

    def test_s6_an_empty_report_file_says_no_report_yet(self):
        self.create("coder7")
        self.patch_state(report={self.name: "  \n\n"})
        code, out, err = self.run_cli("report", "coder7")
        self.assertEqual(code, cw.EXIT_CLOUD)
        self.assertIn("no report yet", err)

    def test_the_driver_brief_tells_the_worker_where_to_write_its_report(self):
        p, out = self.run_driver()
        log = self.read(self.stub_log)
        prompt = log[log.index("prompt "):]   # the brief is multi-line: everything the stub logged from the prompt on
        self.assertTrue(prompt.startswith("prompt " + td.BRIEF), prompt)
        self.assertIn("/home/worker/report", prompt)


class RecordUnderATokenShapedPath(Record):
    """The key checks again with a 49-character token-shaped temp-path segment (as under macOS /var/folders):
    they must not depend on the shape of the runner's temp path."""
    @classmethod
    def setUpClass(cls):
        cls.base = tempfile.mkdtemp(prefix="cwlong")
        seg = os.path.join(cls.base, "q7Zx9LmN3vB8kT2pR5wY1cD4fG6hJ0sA9eU3iO7nM5aXbVc"[:49])
        os.mkdir(seg)
        cls.TMP_BASE = seg

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)

    def test_the_temp_path_really_is_token_shaped(self):
        self.assertNotEqual(self.shown(self.records), self.records)

    def test_s5_after_the_stop_deletes_the_pod_the_record_holds_a_transcript_and_a_tally_with_no_secret(self):
        self.check_s5_after_the_stop()

    def test_s5_the_launchers_stop_alone_writes_the_record_so_retire_gets_one(self):
        self.check_stop_alone()


for _n in [n for n in dir(Record) if n.startswith("test_") and n not in
           ("test_s5_after_the_stop_deletes_the_pod_the_record_holds_a_transcript_and_a_tally_with_no_secret",
            "test_s5_the_launchers_stop_alone_writes_the_record_so_retire_gets_one")]:
    setattr(RecordUnderATokenShapedPath, _n, None)   # only the key checks run under the long path


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
