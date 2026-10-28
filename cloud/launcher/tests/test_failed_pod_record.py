"""A cloud pod that ends Failed leaves its reason and a record (ask #1442): scenarios S1 to S6 and U1.

Offline: the fake kubectl and a fake clock of the other launcher tests. Every fixture is synthetic; the token-shaped string is built at run time.
"""
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PLANT = "ghp_" + "Zq81LmN4pR7tVw2Y9aBc"   # two halves: no complete token-shaped string sits in this file

CASES = {   # worker: (the pod's status, the reason the record must give, the exit code)
    "oomer": ({"phase": "Failed", "containerStatuses": [{"name": "worker", "state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]},
              "OOMKilled (exit 137)", 137),
    "evictd": ({"phase": "Failed", "reason": "Evicted", "message": "The node was low on resource: memory."}, "Evicted", None),
    "exiter": ({"phase": "Failed", "containerStatuses": [{"name": "worker", "state": {"terminated": {"reason": "Error", "exitCode": 1}}}]},
                "Error (exit 1)", 1),
}


class Fixture(base.Base):
    def setUp(self):
        super().setUp()
        self.runs = os.path.join(self.tmp.name, "runs")
        self.env["CW_RUN_RECORD_DIR"] = self.runs

    def fail(self, worker, status, kept=False):
        self.create(worker)
        st = self.st()
        pod = st["pods"][cw.pod_name(worker)]
        pod["status"] = status
        if kept:
            pod["metadata"].setdefault("annotations", {})["cw/kept"] = "true"
        evs = st.setdefault("events", [])
        name = cw.pod_name(worker)
        for i in range(14):
            evs.append({"reason": "Ev%d" % i, "message": "event %d " % i + "x" * 500 + (" " + PLANT if i == 12 else ""),
                        "involvedObject": {"kind": "Pod", "name": name}})
        self.write_state(st)

    def records(self):
        out = []
        if os.path.isdir(self.runs):
            for n in sorted(os.listdir(self.runs)):
                with open(os.path.join(self.runs, n, "run.json")) as f:
                    out.append(json.load(f))
        return out

    def activity_reads(self):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return sum(1 for line in f if "cw-worker-activity" in line)

    def reap(self):
        return cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()


class S2Records(Fixture):
    def test_each_failed_pod_gets_a_record_with_its_reason(self):
        for w, (status, _, _) in CASES.items():
            self.fail(w, status)
        self.reap()
        recs = {r["worker"]: r for r in self.records()}
        self.assertEqual(sorted(recs), sorted(CASES))
        for w, (_, reason, code) in CASES.items():
            r = recs[w]
            self.assertEqual(r["exit_reason"], "pod failed")
            self.assertEqual(r["pod_failed_reason"], reason)
            self.assertEqual(r["exit_code"], code)
            self.assertLessEqual(len(r["events"]), 10)
            self.assertTrue(r["events"])
            self.assertTrue(all(len(e) <= 300 for e in r["events"]))
            self.assertNotIn(PLANT, json.dumps(r))
        for n in os.listdir(self.runs):
            with open(os.path.join(self.runs, n, "run.json")) as f:
                self.assertNotIn(PLANT, f.read())

    def test_a_pod_with_no_container_reason_names_its_last_event(self):
        self.fail("quiet", {"phase": "Failed"})
        self.reap()
        self.assertEqual(self.records()[0]["pod_failed_reason"], "Ev13")


class S3Status(Fixture):
    def test_status_says_pod_failed_with_the_reason(self):
        for w, (status, reason, _) in CASES.items():
            self.fail(w, status)
        for w, (_, reason, _) in CASES.items():
            code, out, err = self.run_cli("status", w)
            self.assertEqual(code, 0, err)
            rec = json.loads(out)[0]
            self.assertEqual(rec["status_line"], "pod failed: " + reason)
            self.assertTrue(rec["status_line"].startswith("pod failed:"))
            self.assertLessEqual(len(rec["status_line"]), 200)
            self.assertNotIn("does not answer", out)
        self.assertEqual(len(self.records()), 3)

    def test_the_reason_is_cut_to_160_characters(self):
        self.fail("longr", {"phase": "Failed", "reason": "slow " * 80})
        code, out, _ = self.run_cli("status", "longr")
        self.assertEqual(len(json.loads(out)[0]["pod_failed"]), 160)

    def test_the_switchboard_line_is_wired(self):
        with open(os.path.join(REPO_ROOT, "bin", "cloud_worker.py")) as f:
            self.assertIn("pod failed: %s", f.read())


class S4Passes(Fixture):
    def test_two_passes_record_once_and_report_once(self):
        self.fail("oomer", CASES["oomer"][0])
        first = self.reap()
        self.assertEqual(first, [("oomer", "pod failed: OOMKilled (exit 137)", 0)])
        reads = self.activity_reads()
        self.assertEqual(reads, 0)
        self.assertEqual(self.reap(), [])
        self.assertEqual(self.activity_reads(), reads)   # 0 activity reads in pass 2
        self.assertEqual(len(self.records()), 1)
        self.assertIsNotNone(self.pod("oomer"))

    def test_deleted_after_ten_minutes_unless_kept(self):
        self.fail("plain", CASES["oomer"][0])
        self.fail("kepto", CASES["oomer"][0], kept=True)
        self.reap()
        self.clock.advance(minutes=9)
        self.reap()
        self.assertIsNotNone(self.pod("plain"))
        self.clock.advance(minutes=2)
        self.assertEqual(self.reap(), [])
        self.assertIsNone(self.pod("plain"))
        self.assertIsNotNone(self.pod("kepto"))
        self.assertEqual(len(self.records()), 2)


class S5Mutant(Fixture):
    def test_a_mutant_that_skips_the_record_fails_the_tests(self):
        applied = []

        def skip(*a, **k):
            applied.append(1)
        for name, cls in (("S2", S2Records), ("S4", S4Passes)):
            with self.subTest(name):
                failed = 0
                with mock.patch.object(cw.run_record, "write_private", skip):
                    for t in unittest.defaultTestLoader.loadTestsFromTestCase(cls):
                        res = unittest.TestResult()
                        t.run(res)
                        failed += len(res.failures) + len(res.errors)
                self.assertGreater(failed, 0)
        self.assertTrue(applied, "the mutation was not applied")


import re  # noqa: E402
import test_cloud_status_summary as ss  # noqa: E402


class S8StatusLine(ss.Base):
    """S8: the switchboard's status line for a launcher answer whose pod_failed field is hostile or not text (ask #1442, proof of the first head)."""
    INPUTS = (("escape", "OOM\x1b[31mred", True), ("bell", "OOM\x07ring", True), ("newline", "OOM\nkilled\tagain", True),
              ("number", 137, False), ("object", {}, False), ("list", ["OOMKilled"], False))

    def line_for(self, value):
        self.record("coder7", hard=ss.ALIVE)
        original = ss.CLOUD.run_launcher

        def fake(runner, command, worker, *rest, **kw):
            if command == "status":
                return 0, {"phase": "Failed", "pod_failed": value, "cost_estimate_usd": 0}, ""
            return original(runner, command, worker, *rest, **kw)
        with mock.patch.object(ss.CLOUD, "run_launcher", fake), mock.patch.object(ss.CLOUD, "_answers", lambda *a, **k: False):
            lines, rows = self.status()
        return [line for line in lines if "coder7" in line], rows

    def test_six_inputs_print_one_clean_line_and_never_raise(self):
        for label, value, is_text in self.INPUTS:
            with self.subTest(label):
                mine, rows = self.line_for(value)
                self.assertEqual(len(mine), 1, mine)
                self.assertNotRegex(mine[0], r"[\x00-\x1f\x7f-\x9f]")
                self.assertEqual(len(rows), 1)
                if is_text:
                    self.assertIn("pod failed: OOM", mine[0])
                    self.assertNotIn("does not answer", mine[0])
                else:   # the line printed for a launcher answer it cannot read
                    self.assertIn("launcher status unavailable", mine[0])
                    self.assertNotIn("pod failed:", mine[0])

    def test_the_text_is_scrubbed_again_and_cut(self):
        mine, _ = self.line_for("note " * 100 + " " + PLANT)
        self.assertNotIn(PLANT, mine[0])
        self.assertIn("pod failed: note", mine[0])
        self.assertLessEqual(len(mine[0].split("pod failed: ")[1].split("; stops")[0]), 160)


class S9Launcher(Fixture):
    """S9: a foreign pod is untouched, an unwritable record is reported once and written when it can be, the stored event text is cut."""

    def setUp(self):
        super().setUp()
        self.told = getattr(cw, "FAILED_UNWRITTEN_TOLD", set())
        self.told.clear()
        self.addCleanup(self.told.clear)

    def test_a_labelled_pod_without_the_worker_annotation_is_never_touched(self):
        self.fail("ownd", CASES["oomer"][0])
        st = self.st()
        name = cw.pod_name("ownd")
        foreign = json.loads(json.dumps(st["pods"][name]))
        foreign["metadata"]["name"] = "foreign-pod"
        foreign["metadata"]["annotations"].pop("cw/worker")
        st["pods"]["foreign-pod"] = foreign
        self.write_state(st)
        for minutes in (0, 11, 11):
            self.clock.advance(minutes=minutes)
            self.reap()
        self.assertIn("foreign-pod", self.st()["pods"])
        self.assertEqual([r["worker"] for r in self.records()], ["ownd"])
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            self.assertNotIn("foreign-pod", f.read())   # never read (no get, no events), never deleted

    def test_an_unwritable_record_keeps_the_pod_is_told_once_and_is_written_when_it_can_be(self):
        self.fail("stuck", CASES["oomer"][0])
        real, calls = cw.run_record.write_private, []

        def refuse(*a, **k):
            calls.append(1)
            if len(calls) <= 3:
                raise OSError("disk full")
            return real(*a, **k)
        passes = []
        with mock.patch.object(cw.run_record, "write_private", refuse):
            for _ in range(4):
                self.clock.advance(minutes=11)
                passes.append(self.reap())
        self.assertEqual([len(t) for t in passes], [1, 0, 0, 1])
        self.assertTrue(passes[0][0][1].startswith("record not written"), passes[0])
        self.assertTrue(passes[3][0][1].startswith("pod failed:"), passes[3])
        self.assertEqual(len(self.records()), 1)
        self.assertIsNotNone(self.pod("stuck"))   # in place for the 3 passes (11 minutes apart) and the 4th

    def test_a_long_event_text_of_words_is_cut_to_300_characters(self):
        self.fail("wordy", {"phase": "Failed"})
        st = self.st()
        for ev in st["events"]:
            ev["message"] = " ".join("word%d" % i for i in range(100))[:500]
        self.write_state(st)
        self.reap()
        events = self.records()[0]["events"]
        self.assertTrue(events)
        self.assertTrue(any(len(e) > 100 for e in events), events)   # words survive the scrub, so the cap is what cuts them
        self.assertTrue(all(len(e) <= 300 for e in events))


DOC_FILES = ("cloud/launcher/README.md", "docs/cloud-worker-interface.md")


def doc(path):
    with open(os.path.join(REPO_ROOT, path)) as f:
        text = f.read()
    return " ".join(text[text.index("A pod that ends Failed (ask #1442)"):].split())


class S1S6Docs(unittest.TestCase):
    def test_the_cause_is_stated(self):   # S1: 4 of 4
        for path in DOC_FILES:
            t = doc(path)
            for phrase in ('"does not answer"', "`activity unreadable`", "two hours", "No run record was written", "`get_pod` and `pod_events`"):
                self.assertIn(phrase, t, path)

    def test_the_record_and_how_a_lane_reads_it(self):   # S6: 3 of 3 and the reading
        for path in DOC_FILES:
            t = doc(path)
            for phrase in ("the reason, the exit code and the events", "exit reason `pod failed`", "`status` prints `pod failed: <reason>`", "ten minutes"):
                self.assertIn(phrase, t, path)

    def test_the_two_limits_of_the_second_head_are_stated(self):   # S8 and S9
        for path in DOC_FILES:
            t = doc(path)
            for phrase in ("every control character removed", "never an error", "carries a worker annotation", "`record not written`"):
                self.assertIn(phrase, t, path)


import hashlib  # noqa: E402
import inspect  # noqa: E402
import textwrap  # noqa: E402


def mutated(cls, name, old, new):
    """A copy of cls.name whose source has `old` replaced by `new` (in the module's own globals); the mutation is asserted applied."""
    src = textwrap.dedent(inspect.getsource(getattr(cls, name)))
    assert old in src, "the mutation did not apply: %r" % old
    ns = {}
    exec(src.replace(old, new, 1), vars(cw), ns)
    return ns[name]


class Tree(Fixture):
    """A record root of its own, a snapshot of everything around it, and a clean told-once set."""

    def setUp(self):
        super().setUp()
        self.runs = os.path.join(self.tmp.name, "box", "runs")
        self.env["CW_RUN_RECORD_DIR"] = self.runs
        os.makedirs(self.runs, mode=0o700)
        cw.FAILED_UNWRITTEN_TOLD.clear()
        self.addCleanup(cw.FAILED_UNWRITTEN_TOLD.clear)

    def snapshot(self):
        out = set()
        for root, dirs, names in os.walk(self.tmp.name):
            dirs[:] = [d for d in dirs if d != "locks"]
            for n in dirs + names:
                if not n.startswith(("kube.json", "calls.jsonl", "audit.jsonl")):
                    out.add(os.path.join(root, n))
        return out

    def calls(self):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return f.read()

    def deletes(self):
        return [line for line in self.calls().splitlines() if "delete" in line]

    def set_annotation(self, worker, key, value):
        st = self.st()
        st["pods"][cw.pod_name(worker)]["metadata"]["annotations"][key] = value
        self.write_state(st)

    def planted(self, worker, annotation, status=None):
        self.fail(worker, status or CASES["oomer"][0])
        self.set_annotation(worker, "cw/worker", annotation)


BAD_NAMES = (("empty", ""), ("space", " "), ("dots", "../.."), ("dotx", "../x"), ("slash", "/"), ("long", "a" * 33), ("upper", "Abc"),
             ("control", "ab\x07c"), ("newline", "abc\n"), ("inner", "x/../y"), ("tab", "\tabc"))


class HNameBeforePath(Tree):
    def run_one(self, annotation):
        self.count = getattr(self, "count", 0) + 1
        self.last = "evil%d" % self.count
        self.planted(self.last, annotation)
        name = cw.pod_name(self.last)
        before = self.snapshot()
        made = len(self.calls().splitlines())   # the creation of the fixture is not a read by the code under test
        reports = []
        for minutes in (0, 11, 11):
            self.clock.advance(minutes=minutes)
            reports += self.reap()
        code, out, err = self.run_cli("status")
        self.assertEqual(code, 0, err)
        pod_reads = sum(1 for line in self.calls().splitlines()[made:] if name in line)
        return reports, before, self.snapshot(), pod_reads, self.deletes()

    def test_a_worker_annotation_that_is_not_a_name_is_skipped_like_no_annotation(self):
        for label, annotation in BAD_NAMES:
            with self.subTest(label):
                reports, before, after, pod_reads, deletes = self.run_one(annotation)
                self.assertEqual(after, before)             # 0 files or directories written under or OUTSIDE the root
                self.assertEqual(os.listdir(self.runs), [])
                self.assertEqual(pod_reads, 0)              # 0 cluster reads of that pod
                self.assertEqual(deletes, [])               # 0 deletes
                self.assertEqual([r for r in reports if "pod failed" in r[1] or "record" in r[1]], [])
                self.assertIsNotNone(self.pod(self.last))

    def test_a_good_name_is_still_recorded(self):
        self.planted("fine", "fine")
        self.reap()
        self.assertEqual([r["worker"] for r in self.records()], ["fine"])

    def test_the_status_path_writes_nothing_for_a_foreign_failed_pod(self):   # L1
        for label, annotation in (("none", None), ("dotx", "../x")):
            with self.subTest(label):
                foreign = "foreign" + label
                self.planted(foreign, "x")
                if annotation is None:
                    st = self.st()
                    st["pods"][cw.pod_name(foreign)]["metadata"]["annotations"].pop("cw/worker")
                    self.write_state(st)
                else:
                    self.set_annotation(foreign, "cw/worker", annotation)
                before = self.snapshot()
                made = len(self.calls().splitlines())
                code, out, err = self.run_cli("status")
                self.assertEqual(code, 0, err)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual([l for l in self.calls().splitlines()[made:] if cw.pod_name(foreign) in l], [])

    def test_a_mutant_that_removes_the_check_fails_the_dotdot_test(self):
        applied = []
        reap = mutated(cw.Launcher, "reap", "if not self._failed_name_fits(pod):", "if False:")
        note = mutated(cw.Launcher, "_failed_note", "if not self._failed_name_fits(pod):", "if False:")
        record = mutated(cw.Launcher, "record", 'and not self._failed_name_fits(pod):', "and False:")
        applied.append(1)
        with mock.patch.object(cw.Launcher, "reap", reap), mock.patch.object(cw.Launcher, "_failed_note", note), \
                mock.patch.object(cw.Launcher, "record", record):
            reports, before, after, pod_reads, deletes = self.run_one("../x")
        self.assertTrue(applied)
        self.assertTrue(pod_reads > 0 or deletes or after != before, "the mutant was not caught")

    def test_the_directory_is_checked_to_be_inside_the_root_as_a_second_line(self):
        self.planted("evil", "../..")
        before = self.snapshot()
        with mock.patch.object(cw.Launcher, "_failed_name_fits", lambda self_, pod: True):
            out = self.reap()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(out and out[0][1].startswith("record not written"), out)
        self.assertIsNotNone(self.pod("evil"))


class M1Safety(Tree):
    def test_a_failed_pod_is_deleted_with_its_uid_and_a_recreated_pod_is_left_whole(self):   # (i)
        self.fail("plain", CASES["oomer"][0])
        uid = self.pod("plain")["metadata"]["uid"]
        self.reap()
        self.clock.advance(minutes=11)
        seen = []
        real = cw.Kube.delete_pod

        def spy(kube, name, u=None):
            seen.append(u)
            return real(kube, name, u)
        with mock.patch.object(cw.Kube, "delete_pod", spy):
            self.assertEqual(self.reap(), [])
        self.assertEqual(seen, [uid])
        self.assertIsNone(self.pod("plain"))

    def recreated(self, mutant=False):
        self.fail("swap", CASES["oomer"][0])
        self.reap()
        self.clock.advance(minutes=11)
        listed = cw.Kube(self.env).list_pods()               # the pods as the pass read them
        st = self.st()
        st["pods"][cw.pod_name("swap")]["metadata"]["uid"] = "uid-recreated"   # same name and run id, a new uid, after the list
        self.write_state(st)
        patches = [mock.patch.object(cw.Kube, "list_pods", lambda kube: listed)]
        if mutant:
            real = cw.Launcher._remove
            patches.append(mock.patch.object(cw.Launcher, "_remove", lambda self_, name, nonce, uid=None: real(self_, name, nonce, None)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return self.reap()

    def test_a_pod_recreated_under_the_same_name_is_left_whole(self):
        out = self.recreated()
        self.assertIsNotNone(self.pod("swap"))
        self.assertTrue(out and out[0][1].startswith("failed pod not deleted"), out)

    def test_mutant_without_the_uid_deletes_the_recreated_pod(self):
        self.recreated(mutant=True)
        self.assertIsNone(self.pod("swap"), "the mutant did not delete: the test cannot tell")

    def test_a_failed_pod_past_its_hard_deadline_takes_the_old_stop_path(self):   # (ii)
        self.fail("late", CASES["oomer"][0])
        noted = []
        real = cw.Launcher._failed_note
        stopped = []
        with mock.patch.object(cw.Launcher, "_failed_note", lambda self_, pod: noted.append(1) or real(self_, pod)), \
                mock.patch.object(cw.Launcher, "stop", lambda self_, a, reason=None: stopped.append(reason) or {}):
            self.clock.advance(hours=3)   # past every hard deadline
            out = self.reap()
        self.assertEqual(stopped, ["hard limit"])
        self.assertEqual(noted, [])
        self.assertEqual(out, [("late", "hard limit", 0)])

    def test_mutant_that_checks_failed_first_takes_the_new_branch(self):
        self.fail("late", CASES["oomer"][0])
        mutant = mutated(cw.Launcher, "reap", "if hard is not None and now >= hard:", "if False:")
        stopped = []
        with mock.patch.object(cw.Launcher, "reap", mutant), \
                mock.patch.object(cw.Launcher, "stop", lambda self_, a, reason=None: stopped.append(reason) or {}):
            self.clock.advance(hours=3)
            self.reap()
        self.assertEqual(stopped, [], "the mutant still took the stop path")

    def lists_that_fail(self):
        return (lambda kube: (_ for _ in ()).throw(cw.CloudError("the server is down")),
                lambda kube: json.loads("{"),
                lambda kube: [{"metadata": None}])

    def touched_after_failed_lists(self):
        """How many of the 3 failed lists left the pod untouched (a delete or a new file counts as touched)."""
        self.fail("quiet", CASES["oomer"][0])
        self.clock.advance(minutes=30)
        before, untouched = self.snapshot(), 0
        for fake in self.lists_that_fail():
            with mock.patch.object(cw.Kube, "list_pods", fake):
                try:
                    self.reap()
                except Exception:   # a pass that cannot read the list may stop with an error; it must touch nothing
                    pass
            if self.snapshot() == before and not self.deletes() and self.pod("quiet") is not None:
                untouched += 1
        return untouched

    def test_a_failed_or_partial_pod_list_touches_nothing(self):   # (iii)
        self.assertEqual(self.touched_after_failed_lists(), 3)

    def test_mutant_that_goes_on_with_another_list_when_the_list_fails_is_caught(self):
        def lister(self_):
            try:
                return self_.kube.list_pods()
            except Exception:
                return self_.kube.list_pods_all()   # the mutation: a failed list is replaced by another read and the pass acts
        mutant = mutated(cw.Launcher, "reap", "for pod in self.kube.list_pods():", "for pod in self._lister():")
        with mock.patch.object(cw.Launcher, "_lister", lister, create=True), mock.patch.object(cw.Launcher, "reap", mutant):
            self.assertLess(self.touched_after_failed_lists(), 3)


class L2L3BDC(Tree):
    def test_a_record_with_a_time_that_cannot_be_read_does_not_stop_the_pass(self):   # L2: the pod is left, said once
        self.fail("broke", CASES["oomer"][0])
        self.fail("fine", CASES["oomer"][0])
        self.reap()
        path = [os.path.join(self.runs, n, "run.json") for n in os.listdir(self.runs) if n.startswith("broke-")][0]
        with open(path) as f:
            info = json.load(f)
        info["recorded_at"] = "not a time"
        with open(path, "w") as f:
            json.dump(info, f)
        self.clock.advance(minutes=11)
        first = self.reap()
        second = self.reap()
        self.assertEqual(len([r for r in first if r[0] == "broke"]), 1)
        self.assertEqual([r for r in second if r[0] == "broke"], [])   # said once
        self.assertIsNotNone(self.pod("broke"))                       # left, not deleted and not rewritten
        self.assertEqual(len(self.records()), 2)
        self.assertIsNone(self.pod("fine"))                           # the healthy pod is still handled

    def test_a_record_of_another_worker_with_the_same_pod_run_id_is_not_accepted(self):   # L3
        self.fail("mine", CASES["oomer"][0])
        key = cw.ann(self.pod("mine"), "run-id") or self.pod("mine")["metadata"]["uid"]
        tail = hashlib.sha1(key.encode()).hexdigest()[:6]
        other = os.path.join(self.runs, "mine-20260101T000000Z-" + tail)
        os.makedirs(other, mode=0o700)
        with open(os.path.join(other, "run.json"), "w") as f:
            json.dump({"worker": "someone", "pod_run_id": key, "exit_reason": "pod failed", "recorded_at": cw.iso(self.clock())}, f)
        self.reap()
        self.assertEqual(sorted(r["worker"] for r in self.records()), ["mine", "someone"])

    def test_an_unreadable_deadline_of_a_failed_pod_is_said_once_and_the_pod_is_recorded(self):   # B
        self.fail("odd", CASES["oomer"][0])
        self.set_annotation("odd", "cw/hard-deadline", "not a time")
        first, second = self.reap(), self.reap()
        self.assertEqual(sum(1 for r in first + second if r[1] == "unreadable deadline"), 1)
        self.assertEqual([r["worker"] for r in self.records()], ["odd"])

    def test_a_failed_pod_with_a_deletion_time_is_recorded_and_deleted_as_before(self):   # C
        self.fail("going", CASES["oomer"][0])
        st = self.st()
        st["pods"][cw.pod_name("going")]["metadata"]["deletionTimestamp"] = cw.iso(self.clock())
        self.write_state(st)
        self.reap()
        self.assertEqual(len(self.records()), 1)
        self.assertIsNotNone(self.pod("going"))
        self.clock.advance(minutes=11)
        self.reap()
        self.assertIsNone(self.pod("going"))   # the code does not look at the deletion time: the delete is sent after ten minutes


class M2L4DDocs(unittest.TestCase):
    def test_the_last_limits_are_stated(self):
        for path in DOC_FILES:
            t = doc(path)
            for phrase in ("A record written when the pod could not be read has, as its reason, the text of the read error", "and is not refreshed",
                           "checked against the worker-name rule", "clears the entry of an unwritable record when that record is written",
                           "leaves a few bytes in the set until the process ends", "not stripped of control characters (the JSON escapes them)",
                           "only the status line prints, and it is stripped"):
                self.assertIn(phrase, t, path)



if __name__ == "__main__":
    unittest.main()
