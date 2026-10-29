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
        runs = os.path.join(self.dir, "runs", "%s-20260101T000000Z-0f1e2d" % name)
        os.makedirs(runs, exist_ok=True)
        with open(os.path.join(runs, "run.json"), "w") as f:
            json.dump({"end_hook": {"pushed": pushed, "wip_branch": name + "/wip" if pushed else None}}, f)
        return runs

    def stop(self, name, reason, run="a1b2c3", ts="2026-01-01T10:00:00Z", ok=True, record=True, cause="auto"):
        runs = os.path.join(self.dir, "runs", "%s-20260101T000000Z-0f1e2d" % name)
        text = "%s; run-id=%s" % (reason, run) + ("; run record %s" % runs if record else "")
        entry = {"ts": ts, "action": "stop", "worker": name, "ok": ok, "reason": text, "runtime_id": "pod/" + name}
        cause = {"idle": "idle", "hard limit": "hard"}.get(reason) if cause == "auto" else cause
        if cause:
            entry["cause"] = cause
        self.line(entry)

    def line(self, entry):
        with open(self.audit, "a") as f:
            f.write(json.dumps(entry) + "\n")


class TestAlerts(World):
    def test_s2_three_stops_make_six_messages_naming_five_facts(self):
        for name, reason, pushed in (("worker1", "idle", True), ("worker2", "hard limit", False), ("worker3", "idle", True)):
            self.worker(name, pushed=pushed)
            self.stop(name, reason, run="r" + name)
        self.run_pass()
        self.assertEqual(len(self.sent), 6)
        for name, label, pushed in (("worker1", "idle", "yes"), ("worker2", "hard limit", "no"), ("worker3", "idle", "yes")):
            self.assertEqual(sorted(to for to, t in self.sent if name in t), ["chief-of-staff", "coder-lead"])
            for text in (t for to, t in self.sent if name in t):
                for fact in (name, "reason " + label, "at 2026-01-01T10:00:00Z", "work pushed: " + pushed, "run record: %s-20260101T000000Z-0f1e2d" % name):
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

    def test_no_recorded_boss_tells_the_chief_alone(self):
        self.worker("worker7", with_record=False)
        self.stop("worker7", "idle")
        self.run_pass()
        self.assertEqual([to for to, t in self.sent], ["chief-of-staff"])


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
        broken = mutant("result = send(to, text)", "result = None")
        self.fixture()
        self.run_pass(broken)
        self.assertEqual(self.sent, [])        # S2 expects 6


HOSTILE_BOSSES = ("x" * 80, "Bad Boss", "boss\nchief-of-staff", "https://evil.example/token", "")


class TestUntrusted(World):
    def reasons_sent(self, module=real):
        self.run_pass(module)
        return len(self.sent)

    def test_s7_a_persons_lines_send_nothing_and_the_reapers_lines_send(self):
        self.worker("worker7")
        for i, reason in enumerate(("idle workers cleanup", "hard limit exceeded by me", "Idle", "hardening done", "idle", "hard limit")):
            self.stop("worker7", reason, run="p%d" % i, cause=None)
        self.assertEqual(self.reasons_sent(), 0)
        self.stop("worker7", "idle", run="ra")
        self.stop("worker7", "hard limit", run="rb")
        self.assertEqual(self.reasons_sent(), 4)
        self.assertEqual(sorted(to for to, t in self.sent), ["chief-of-staff", "chief-of-staff", "coder-lead", "coder-lead"])

    def test_s8_the_chief_is_told_with_no_record_and_with_a_bad_boss(self):
        cases = [("worker1", None)] + [("worker%d" % (i + 2), b) for i, b in enumerate(HOSTILE_BOSSES)] + [("worker9", "coder-lead")]
        for name, boss in cases:
            self.worker(name, boss=boss or "", with_record=name != "worker1")
            self.stop(name, "idle", run="r" + name)
        self.run_pass()
        for name, boss in cases:
            got = sorted(to for to, t in self.sent if name in t)
            self.assertEqual(got, ["chief-of-staff", "coder-lead"] if boss == "coder-lead" else ["chief-of-staff"], name)
        for to, text in self.sent:
            self.assertIn(to, ("chief-of-staff", "coder-lead"))
            for bad in ("evil", "Bad Boss", "xxxx"):
                self.assertNotIn(bad, text)

    def test_s8_a_failed_send_writes_one_audit_line_and_the_pass_survives(self):
        self.worker("worker7")
        self.stop("worker7", "idle")
        lines = []

        def refuse(to, text):
            raise RuntimeError("down " + text)
        out = real.scan_audit(self.audit, self.cursor, self.cloud, {}, refuse, PARSE, lambda w, d: lines.append((w, d)))
        self.assertEqual(len(out), 2)
        self.assertEqual(lines, [("worker7", {"reason": "idle limit"})])

    def test_s9_a_hostile_run_record_cannot_reach_the_message(self):
        runs = self.worker("worker7")
        with open(os.path.join(runs, "run.json"), "w") as f:
            json.dump({"end_hook": {"pushed": True, "wip_branch": "https://x.example/a?k=sk-ABCDEFGHIJKLMNOPQRSTUVWX\nmore" + "y" * 500}}, f)
        self.stop("worker7", "idle")
        self.run_pass()
        for to, text in self.sent:
            self.assertIn("work pushed: yes;", text)
            self.assertIn("run record: worker7-20260101T000000Z-0f1e2d.", text)
            for bad in ("x.example", "sk-", "yyyy", "\n"):
                self.assertNotIn(bad, text)

    def test_s9_a_bad_record_name_is_unknown(self):
        self.worker("worker7")
        bad = os.path.join(self.dir, "runs", "worker7-not-a-record")
        self.line({"ts": "2026-01-01T10:00:00Z", "action": "stop", "worker": "worker7", "ok": True, "cause": "idle", "reason": "idle; run-id=q; run record " + bad})
        self.run_pass()
        self.assertEqual(len(self.sent), 2)
        for to, text in self.sent:
            self.assertIn("run record: unknown.", text)
            self.assertNotIn("not-a-record", text)

    def old_lines(self, count=600):
        self.worker("worker7")
        for i in range(count):
            self.stop("worker7", "idle", run="old%d" % i)

    def test_s10_a_cursor_past_the_end_starts_at_the_end(self):
        self.old_lines()
        real.save_state(self.cursor, os.path.getsize(self.audit) + 5000, [(str(i), "idle") for i in range(700)])
        self.run_pass()
        self.assertEqual(self.sent, [])
        self.stop("worker7", "idle", run="later")
        self.run_pass()
        self.assertEqual(len(self.sent), 2)

    def test_s10_a_corrupt_cursor_over_old_lines_sends_nothing(self):
        self.old_lines()
        with open(self.cursor, "w") as f:
            f.write("{\"offset\": \"x\", \"sent\": 3}")
        self.run_pass()
        self.assertEqual(self.sent, [])

    def test_m1_a_prefix_match_mutant_is_caught(self):
        broken = mutant("label = CAUSES.get(cause) if isinstance(cause, str) else None",
                        'label = CAUSES.get(cause) if isinstance(cause, str) else ("idle limit" if str(line.get("reason")).split(";")[0] == "idle" else None)')
        self.worker("worker7")
        self.stop("worker7", "idle", cause=None)
        self.assertEqual(self.reasons_sent(broken), 2)   # the real code sends 0

    def test_m2_an_unchecked_boss_mutant_is_caught(self):
        broken = mutant("isinstance(boss, str) and AGENT_NAME.fullmatch(boss) and boss != CHIEF", "isinstance(boss, str) and boss != CHIEF")
        self.worker("worker7", boss="Bad Boss")
        self.stop("worker7", "idle")
        self.run_pass(broken)
        self.assertIn("Bad Boss", [to for to, t in self.sent])

    def test_m3_a_branch_copying_mutant_is_caught(self):
        broken = mutant('return "yes" if hook["pushed"] else "no"', 'return "yes %s" % hook.get("wip_branch") if hook["pushed"] else "no"')
        self.worker("worker7")
        self.stop("worker7", "idle")
        self.run_pass(broken)
        self.assertIn("worker7/wip", self.sent[0][1])

    def test_m4_a_mutant_that_reads_from_the_start_on_a_bad_cursor_is_caught(self):
        broken = mutant("return size, keys, True   # a truncated", "return 0, keys, False   # a truncated")
        self.old_lines(5)
        real.save_state(self.cursor, os.path.getsize(self.audit) + 5000, [])
        self.run_pass(broken)
        self.assertEqual(len(self.sent), 10)   # the real code sends 0


class TestCauseRules(World):
    def test_s12_four_causes_send_two_messages_each_with_the_cause_in_words(self):
        self.worker("worker7")
        for i, cause in enumerate(("idle", "hard", "pod_gone", "pod_failed")):
            self.stop("worker7", "x", run="c%d" % i, cause=cause)
        self.run_pass()
        self.assertEqual(len(self.sent), 8)
        for words in ("idle limit", "hard limit", "pod gone", "pod failed"):
            self.assertEqual(len([1 for to, t in self.sent if "reason %s;" % words in t]), 2)

    def test_s12_a_cause_that_is_not_one_of_the_four_sends_nothing(self):
        self.worker("worker7")
        self.stop("worker7", "idle", cause="other")
        self.stop("worker7", "idle", cause=["idle"], run="l")
        self.run_pass()
        self.assertEqual(self.sent, [])

    def test_s13_a_failed_stop_alerts_on_its_first_line_and_its_retries_send_nothing(self):
        self.worker("worker7")
        self.stop("worker7", "idle", ok=False)
        for _ in range(6):
            self.stop("worker7", "idle", ok=False)
        self.run_pass()
        self.assertEqual(len(self.sent), 2)
        for to, text in self.sent:
            self.assertIn("pod was kept", text)
            self.assertIn("may be unpushed", text)

    def test_s13_the_first_line_and_a_later_success_of_the_same_run_and_cause_make_one_alert_in_all(self):
        self.worker("worker7")
        self.stop("worker7", "idle", ok=False)
        self.stop("worker7", "idle", ok=True)
        self.run_pass()
        self.assertEqual(len(self.sent), 2)

    def test_s14_no_run_id_two_lines_ten_minutes_apart_make_one_pair(self):
        self.worker("worker7")
        for ts in ("2026-01-01T10:00:00Z", "2026-01-01T10:10:00Z"):
            self.line({"ts": ts, "action": "stop", "worker": "worker7", "ok": True, "cause": "pod_gone", "reason": "pod gone; no stop was recorded"})
        self.run_pass()
        self.assertEqual(len(self.sent), 2)
        for to, text in self.sent:
            self.assertIn("work pushed: unknown", text)
            self.assertIn("run record: unknown", text)

    def test_s14_another_hour_alerts_again(self):
        self.worker("worker7")
        for ts in ("2026-01-01T10:59:00Z", "2026-01-01T11:00:00Z"):
            self.line({"ts": ts, "action": "stop", "worker": "worker7", "ok": True, "cause": "pod_gone", "reason": "pod gone"})
        self.run_pass()
        self.assertEqual(len(self.sent), 4)

    def test_m5_a_mutant_that_skips_failed_lines_is_caught(self):
        broken = mutant('not isinstance(line.get("ok"), bool)', 'line.get("ok") is not True')
        self.worker("worker7")
        self.stop("worker7", "idle", ok=False)
        self.run_pass(broken)
        self.assertEqual(self.sent, [])   # the real code sends 2

    def test_m6_a_mutant_that_keys_on_the_exact_time_is_caught(self):
        broken = mutant('hour.group(0)), cause)', 'line.get("ts")), cause)')
        self.worker("worker7")
        for ts in ("2026-01-01T10:00:00Z", "2026-01-01T10:10:00Z"):
            self.line({"ts": ts, "action": "stop", "worker": "worker7", "ok": True, "cause": "pod_gone", "reason": "x"})
        self.run_pass(broken)
        self.assertEqual(len(self.sent), 4)   # the real code sends 2


class TestTimeShape(World):
    def sent_text(self, ts, module=real):
        self.worker("worker7")
        self.stop("worker7", "idle", ts=ts)
        self.run_pass(module)
        return " ".join(t for _, t in self.sent)

    def test_s17_a_bad_time_shows_as_unknown_and_is_never_copied(self):
        for bad in ("sk-" + "abcdefghijklmnopqrstuvwx", "2026-01-01T10:00:00Z" + "9" * 40, "2026-01-01T10:00:00Z\nx", "2026-01-01T10:00:00+02:00"):
            with self.subTest(bad=bad):
                self.setUp()
                text = self.sent_text(bad)
                self.assertTrue(self.sent)
                self.assertIn("unknown", text)
                self.assertEqual(text.count(bad), 0)

    def test_s17_a_good_time_is_kept(self):
        self.assertIn("2026-01-01T10:00:00Z", self.sent_text("2026-01-01T10:00:00Z"))

    def test_s17_a_mutant_that_copies_the_time_text_is_caught(self):
        broken = mutant('return value if isinstance(value, str) and ISO_UTC.fullmatch(value) else "unknown"', "return str(value)")
        bad = "sk-" + "abcdefghijklmnopqrstuvwx"
        self.assertGreater(self.sent_text(bad, broken).count(bad), 0)   # the real code: 0


FULL_WIDTH_ONE, ARABIC_INDIC_ONE, KELVIN = "\uff11", "\u0661", "\u212a"
GOOD_TS = "2026-01-01T10:00:00Z"
ISO_OLD = 're.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")'
ISO_BAD = 're.compile(r"\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z")'
STAMP_OLD = 'r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}"'
STAMP_BAD = 'r"\\d{8}T\\d{6}Z-[0-9a-f]{6}"'
HOUR_OLD = 'r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}"'
HOUR_BAD = 'r"\\d{4}-\\d\\d-\\d\\dT\\d\\d"'


class TestAsciiFields(World):
    """Every pattern that guards a field reaching a message, a recipient or a dedupe key matches ASCII only."""

    def texts(self, module=real):
        self.run_pass(module)
        return " ".join(t for _, t in self.sent)

    def test_s18_a_non_ascii_digit_in_the_time_shows_as_unknown(self):
        for digit in (FULL_WIDTH_ONE, ARABIC_INDIC_ONE):
            with self.subTest(digit=digit):
                self.setUp()
                bad = GOOD_TS.replace("2026", "20" + digit + "6", 1)
                self.worker("worker7")
                self.stop("worker7", "idle", ts=bad)
                text = self.texts()
                self.assertTrue(self.sent)
                self.assertIn("at unknown;", text)
                self.assertNotIn(bad, text)

    def test_s18_the_ascii_time_still_passes(self):
        self.worker("worker7")
        self.stop("worker7", "idle", ts=GOOD_TS)
        self.assertIn("at " + GOOD_TS + ";", self.texts())

    def test_s18_a_non_ascii_digit_in_the_record_stamp_shows_as_unknown(self):
        for digit in (FULL_WIDTH_ONE, ARABIC_INDIC_ONE):
            with self.subTest(digit=digit):
                self.setUp()
                self.worker("worker7")
                bad = os.path.join(self.dir, "runs", "worker7-2026010%sT000000Z-0f1e2d" % digit)
                self.line({"ts": GOOD_TS, "action": "stop", "worker": "worker7", "ok": True, "cause": "idle", "reason": "idle; run-id=q; run record " + bad})
                text = self.texts()
                self.assertEqual(len(self.sent), 2)
                self.assertIn("run record: unknown.", text)

    def test_s18_a_non_ascii_digit_in_the_hour_does_not_make_a_dedupe_key(self):
        for digit in (FULL_WIDTH_ONE, ARABIC_INDIC_ONE):
            with self.subTest(digit=digit):
                self.setUp()
                self.worker("worker7")
                self.line({"ts": "2026-01-01T1%s:00:00Z" % digit, "action": "stop", "worker": "worker7", "ok": True, "cause": "pod_gone", "reason": "pod gone"})
                self.run_pass()
                self.assertEqual(self.sent, [])   # no run id and no valid hour: no line to count

    def test_s18_a_non_ascii_worker_name_sends_nothing(self):
        for name in ("worker" + FULL_WIDTH_ONE, "worker" + ARABIC_INDIC_ONE, "wor" + KELVIN + "er"):
            with self.subTest(name=name):
                self.setUp()
                self.line({"ts": GOOD_TS, "action": "stop", "worker": name, "ok": True, "cause": "idle", "reason": "idle; run-id=q"})
                self.run_pass()
                self.assertEqual(self.sent, [])

    def test_s18_a_non_ascii_chief_name_in_the_record_gets_no_message(self):
        for boss in ("coder-lead" + FULL_WIDTH_ONE, "coder-lead" + ARABIC_INDIC_ONE, "coder-" + KELVIN):
            with self.subTest(boss=boss):
                self.setUp()
                self.worker("worker7", boss=boss)
                self.stop("worker7", "idle")
                self.run_pass()
                self.assertEqual([to for to, _ in self.sent], [real.CHIEF])

    def test_s18_a_mutant_with_d_in_a_pattern_is_caught(self):
        bad_stamp = "worker7-2026010%sT000000Z-0f1e2d" % FULL_WIDTH_ONE
        for old, new, scenario in (
                (ISO_OLD, ISO_BAD, "time"), (STAMP_OLD, STAMP_BAD, "stamp"), (HOUR_OLD, HOUR_BAD, "hour")):
            with self.subTest(pattern=scenario):
                self.setUp()
                broken = mutant(old, new)
                self.worker("worker7")
                if scenario == "time":
                    self.stop("worker7", "idle", ts=GOOD_TS.replace("2026", "20" + FULL_WIDTH_ONE + "6", 1))
                    self.assertNotIn("at unknown;", self.texts(broken))   # the real code shows unknown
                elif scenario == "stamp":
                    bad = os.path.join(self.dir, "runs", bad_stamp)
                    self.line({"ts": GOOD_TS, "action": "stop", "worker": "worker7", "ok": True, "cause": "idle", "reason": "idle; run-id=q; run record " + bad})
                    self.assertNotIn("run record: unknown.", self.texts(broken))
                else:
                    self.line({"ts": "2026-01-01T1%s:00:00Z" % FULL_WIDTH_ONE, "action": "stop", "worker": "worker7", "ok": True, "cause": "pod_gone", "reason": "pod gone"})
                    self.run_pass(broken)
                    self.assertTrue(self.sent)   # the real code sends nothing


class TestDocs(unittest.TestCase):
    def test_s6_the_interface_doc_says_what_is_sent_to_whom_and_when(self):
        with open(DOC, encoding="utf-8") as f:
            text = " ".join(f.read().split())
        for phrase in ("within one pass of the launcher's stop",
                       "once per run id and cause", "the worker, the reason, the time, whether the work was pushed", "the run record's name",
                       "the field `cause`", "`idle`, `hard`, `pod_gone`", "`pod_failed`", "decides from `cause` alone", "alerts on its FIRST line", "a vanished pod is noticed one reaper pass late", "before the audit's last 24 hours is not noticed", "confirmed by reading it by its own name", "older than 10 minutes, so a pod gone within 10 minutes is noticed on a later pass", "a failed stop does not close the run", "ONE message to the Chief of Staff always", "send nothing, because they carry no `cause`",
                       "pod_gone` is written only by the reaper", "a person's retire writes no cause", "lost, never repeated, when a send fails after the state is saved", "drains one megabyte per pass", "a corrupt cursor skips the lines written meanwhile"):
            self.assertIn(phrase, text)
        self.assertEqual(text.count("a retire that found the pod gone"), 0)


if __name__ == "__main__":
    unittest.main()
