"""Ask #1480: a cloud worker the launcher ended alerts its boss and the Chief of Staff, once per run and reason. Offline: a scratch state
directory, a fixture audit file, a recording send function. Mutants are copies of the module's text in memory, never a file."""
import json, os, shutil, sys, tempfile, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.join(ROOT, "bin"))
import record_json  # noqa: E402
import cloud_end_alert as real  # noqa: E402

PARSE = record_json.parse_bounded
SOURCE = os.path.join(ROOT, "bin", "cloud_end_alert.py")
DOC = os.path.join(ROOT, "docs", "cloud-worker-interface.md")


def mutant(old, new):
    """The module with `old` replaced by `new` (asserted applied, once)."""
    with open(SOURCE) as f:
        text = f.read()
    assert text.count(old) == 1, "the mutation did not apply: %r" % old
    module = types.ModuleType("cloud_end_alert_mutant")
    exec(compile(text.replace(old, new), "mutant", "exec"), module.__dict__)
    return module


class World(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="end-alert-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.audit = os.path.join(self.dir, "audit.jsonl")
        self.cursor = os.path.join(self.dir, "cloud-end-alert.json")
        self.cloud = os.path.join(self.dir, "cloud")
        os.makedirs(self.cloud)
        self.sent = []
        open(self.audit, "w").close()
        self.run_pass()   # the first pass over an empty file stores the cursor at its end

    def send(self, to, text):
        self.sent.append((to, text))

    def run_pass(self, module=real, reports=None):
        return module.scan_audit(self.audit, self.cursor, self.cloud, reports or {}, self.send, PARSE)

    def worker(self, name, boss="coder-lead", pushed=True, with_record=True):
        if with_record:
            with open(os.path.join(self.cloud, name + ".json"), "w") as f:
                json.dump({"worker": name, "reports_to": boss}, f)
        runs = os.path.join(self.dir, "runs", "%s-20260101T000000Z" % name)
        os.makedirs(runs, exist_ok=True)
        with open(os.path.join(runs, "run.json"), "w") as f:
            json.dump({"end_hook": {"pushed": pushed, "wip_branch": name + "/wip" if pushed else None}}, f)
        return runs

    def stop(self, name, reason, run="a1b2c3", ts="2026-01-01T10:00:00Z", ok=True, record=True):
        runs = os.path.join(self.dir, "runs", "%s-20260101T000000Z" % name)
        text = "%s; run-id=%s" % (reason, run) + ("; run record %s" % runs if record else "")
        self.line({"ts": ts, "action": "stop", "worker": name, "ok": ok, "reason": text, "runtime_id": "pod/" + name})

    def line(self, entry):
        with open(self.audit, "a") as f:
            f.write(json.dumps(entry) + "\n")


class TestAlerts(World):
    def test_s2_three_stops_make_six_messages_naming_five_facts(self):
        for name, reason, pushed in (("worker1", "idle", True), ("worker2", "hard limit", False), ("worker3", "pod gone", True)):
            self.worker(name, pushed=pushed)
            self.stop(name, reason, run="r" + name)
        self.run_pass()
        self.assertEqual(len(self.sent), 6)
        for name, label, pushed in (("worker1", "idle", "yes (branch worker1/wip)"), ("worker2", "hard limit", "no"), ("worker3", "pod gone", "yes (branch worker3/wip)")):
            self.assertEqual(sorted(to for to, t in self.sent if name in t), ["chief-of-staff", "coder-lead"])
            for text in (t for to, t in self.sent if name in t):
                for fact in (name, "reason " + label, "at 2026-01-01T10:00:00Z", "work pushed: " + pushed, "run record: %s-20260101T000000Z" % name):
                    self.assertIn(fact, text)
                self.assertNotIn(self.dir, text)
                self.assertNotIn("a1b2c3", text)

    def test_s3_seven_lines_of_one_run_and_three_passes_send_two_messages(self):
        self.worker("worker7")
        for _ in range(7):
            self.stop("worker7", "idle")
        self.run_pass()
        self.assertEqual(len(self.sent), 2)
        self.run_pass()
        self.run_pass()
        self.assertEqual(len(self.sent), 2)

    def test_s3_a_new_run_or_reason_alerts_again(self):
        self.worker("worker7")
        self.stop("worker7", "idle")
        self.stop("worker7", "hard limit")
        self.stop("worker7", "idle", run="other")
        self.run_pass()
        self.assertEqual(len(self.sent), 6)

    def test_s3_the_sent_keys_are_bounded_and_the_state_is_private(self):
        real.save_state(self.cursor, 0, [(str(i), "idle") for i in range(700)])
        self.assertEqual(len(PARSE(open(self.cursor).read())["sent"]), 500)
        self.assertEqual(oct(os.stat(self.cursor).st_mode & 0o777), "0o600")

    def test_s3_a_corrupt_cursor_starts_at_the_end(self):
        self.worker("worker7")
        self.stop("worker7", "idle")
        with open(self.cursor, "w") as f:
            f.write("{not json")
        self.run_pass()
        self.assertEqual(self.sent, [])
        self.stop("worker7", "idle", run="later")
        self.run_pass()
        self.assertEqual(len(self.sent), 2)

    def test_boss_is_the_chief_alone_gets_one_message(self):
        self.worker("worker7", boss="chief-of-staff")
        self.stop("worker7", "idle")
        self.run_pass()
        self.assertEqual([to for to, t in self.sent], ["chief-of-staff"])

    def test_no_recorded_boss_sends_nothing(self):
        self.worker("worker7", with_record=False)
        self.stop("worker7", "idle")
        self.run_pass()
        self.assertEqual(self.sent, [])


class TestQuiet(World):
    def test_s4_a_worker_that_reported_done_sends_nothing(self):
        self.worker("worker7")
        self.stop("worker7", "idle")
        self.run_pass(reports={"worker7": {"state": "done", "at": 1}})
        self.assertEqual(self.sent, [])

    def test_s4_a_stop_by_name_sends_nothing(self):
        self.worker("worker7")
        for i, reason in enumerate(("done", "finished", "ended", "pushed the branch", "launch interrupted", "comment read", "stopped by hand", "")):
            self.stop("worker7", reason, run="r%d" % i)
        self.stop("worker7", "idle", ok=False, run="failed-stop")
        self.run_pass()
        self.assertEqual(self.sent, [])

    def test_a_line_that_is_not_json_is_skipped_and_the_next_still_counts(self):
        self.worker("worker7")
        with open(self.audit, "a") as f:
            f.write("not json\n[1]\n")
        self.stop("worker7", "idle")
        self.run_pass()
        self.assertEqual(len(self.sent), 2)


class TestMutants(World):
    def fixture(self):
        self.worker("worker7")
        for _ in range(7):
            self.stop("worker7", "idle")

    def test_s5_a_mutant_that_sends_on_every_line_breaks_the_once_rule(self):
        broken = mutant("if key in keys:", "if False:")
        self.fixture()
        self.run_pass(broken)
        self.assertEqual(len(self.sent), 14)   # S3 expects 2

    def test_s5_a_mutant_that_sends_nothing_breaks_the_alert(self):
        broken = mutant("            send(to, text)", "            pass")
        self.fixture()
        self.run_pass(broken)
        self.assertEqual(self.sent, [])        # S2 expects 6


class TestDocs(unittest.TestCase):
    def test_s6_the_interface_doc_says_what_is_sent_to_whom_and_when(self):
        with open(DOC, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        for phrase in ("ONE message to that recorded boss and ONE to the Chief of Staff", "within one pass of the launcher's stop",
                       "once per run id and reason", "the worker, the reason, the time, whether the work was pushed", "the run record's name"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
