"""Slice S3 (#638): the run record (S5, U2) and the report path (S6). Offline: the fake kubectl and the stub server."""
import base64
import json
import os
import signal
import sys
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

    def assert_record_has_no_secret(self):
        text = self.all_record_text()
        for form in planted_forms():
            self.assertNotIn(form, text)
        self.assert_no_secret(text, "the record")

    # -- S5: the record survives the pod --
    def test_s5_after_the_stop_deletes_the_pod_the_record_holds_a_transcript_and_a_tally_with_no_secret(self):
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
        self.assertIn("[redacted]", self.all_record_text())
        tally = json.loads(self.read(os.path.join(d, "tally.json")))
        self.assertEqual(tally, {"messages": 3, "tool_calls": {"bash": 2, "edit": 1}, "turns": 2})
        info = json.loads(self.read(os.path.join(d, "run.json")))
        self.assertEqual((info["transcript"], info["worker"], info["repository"], info["exit_reason"]), ("saved", "coder7", td.REPO, "task done"))
        self.assertTrue(info["start"] and info["end"])
        self.assert_record_has_no_secret()
        self.assert_clean(out)
        self.assertEqual(out.count(d), 1, "named once on screen")
        self.assertEqual(self.read(self.env["CW_CLOUD_AUDIT"]).count(d), 1)

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
        self.assertEqual(used, ["head"])
        self.assertFalse([c for c in execs if "fleet-switchboard" in " ".join(c)])
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

    def test_the_driver_brief_tells_the_worker_where_to_write_its_report(self):
        p, out = self.run_driver()
        self.assertIn("/home/worker/report", self.read(self.stub_log))


if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=1).result
    sys.exit(0 if result.wasSuccessful() else 1)
