"""The idle nudge: one fixed line to a stalled builder pod (send_idle_nudges, idle_nudge_due). Stub pods, no cluster."""
import datetime as dt
import inspect
import io
import contextlib
import os
import sys
import textwrap
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_git_token_renewal as tr  # noqa: E402

cw = tr.cw
NOW = dt.datetime(2026, 11, 3, 12, 0, 0, tzinfo=dt.timezone.utc)
CONTROL_SRC = textwrap.dedent(inspect.getsource(cw.Launcher.send_idle_nudges))
SKIP_RESULTS = '            if seen != "no":\n                continue\n'
MARK_CALL = "            self.kube.annotate(name, (pod.get(\"metadata\") or {}).get(\"uid\"), **{IDLE_NUDGE_MARK: \"sent\"})\n"


def pod(role="worker", idle="3600", marked=False):
    a = {"cw/worker": "w1", "cw/idle-seconds": idle, "cw/role": role}
    if marked:
        a["cw/idle-nudge"] = "sent"
    return {"metadata": {"name": "w1-pod", "uid": "u1", "annotations": a}, "status": {"phase": "Running"}}


class Stub:
    def __init__(self, busy=False, idle_minutes=20, results="no"):
        self.sent, self.audits, self.marks, self.execs = [], [], [], []
        self.busy, self.idle_minutes, self.results = busy, idle_minutes, results
        self.kube = types.SimpleNamespace(annotate=lambda n, u, **kv: self.marks.append(kv), exec=self._exec, scrub=lambda e, n=120: str(e)[:n])

    def _exec(self, name, *cmd, **kw):
        self.execs.append(cmd)
        if self.results == "fail":
            raise cw.CloudError("exec failed")
        return self.results + "\n"

    def activity(self, name):
        return self.busy, NOW - dt.timedelta(minutes=self.idle_minutes)

    def send_note(self, name, text):
        self.sent.append(text)

    def audit(self, *a):
        self.audits.append(a)

    def _cost_or_zero(self, p):
        return 0


def run(fn, stub, p):
    with contextlib.redirect_stderr(io.StringIO()):
        fn(stub, [p], NOW)


def mutant(old, new):
    src = CONTROL_SRC.replace(old, new)
    applied = src != CONTROL_SRC
    assert applied, "the mutation was not applied"
    ns = {}
    exec(compile(src, "<mutant>", "exec"), cw.__dict__, ns)
    return ns["send_idle_nudges"]


class TestIdleNudge(unittest.TestCase):
    fn = staticmethod(cw.Launcher.send_idle_nudges)

    def test_due_pod_gets_one_line_one_audit_one_mark_and_no_second(self):
        s = Stub()
        run(self.fn, s, pod())
        self.assertEqual(s.sent, [cw.IDLE_NUDGE])
        self.assertEqual(len(s.audits), 1)
        self.assertEqual(s.audits[0][2], "idle-nudge")
        self.assertEqual(s.marks, [{cw.IDLE_NUDGE_MARK: "sent"}])
        run(self.fn, s, pod(marked=True))   # the next pass sees the annotation
        self.assertEqual(len(s.sent), 1, "%d lines sent where 1 is required" % len(s.sent))

    def test_not_due_cases_send_nothing(self):
        cases = {"results exists": (Stub(results="yes"), pod()), "orchestrator": (Stub(), pod(role="orchestrator")),
                 "busy": (Stub(busy=True), pod()), "idle under 15 minutes": (Stub(idle_minutes=14), pod()),
                 "limit not above 15 minutes": (Stub(), pod(idle="900")), "already marked": (Stub(), pod(marked=True)),
                 "results check cannot run": (Stub(results="fail"), pod())}
        for label, (s, p) in cases.items():
            run(self.fn, s, p)
            self.assertEqual((s.sent, s.audits, s.marks), ([], [], []), "a line was sent: " + label)

    def test_the_text_holds_no_name_and_no_token_form(self):
        self.assertNotRegex(cw.IDLE_NUDGE, r"gh[pousr]_|github_pat_|[A-Za-z0-9]{40}")
        self.assertIn("/work/results.txt", cw.IDLE_NUDGE)

    def test_due_function(self):
        t = NOW - dt.timedelta(minutes=15)
        self.assertTrue(cw.idle_nudge_due(False, t, NOW, 901, "", "worker", False))
        self.assertFalse(cw.idle_nudge_due(False, None, NOW, 901, "", "worker", False))


class TestMutants(unittest.TestCase):
    def test_control_passes(self):
        s = Stub(results="yes")
        run(cw.Launcher.send_idle_nudges, s, pod())
        self.assertEqual(s.sent, [])

    def test_no_op_mutation_fails_the_computation(self):
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            mutant("send_idle_nudges", "send_idle_nudges")

    def test_pass_that_ignores_the_results_file_is_caught(self):
        s = Stub(results="yes")
        run(mutant(SKIP_RESULTS, ""), s, pod())
        self.assertEqual(len(s.sent), 1, "the mutant must send where the control sends 0")

    def test_pass_that_sends_without_marking_is_caught(self):
        s = Stub()
        run(mutant(MARK_CALL, ""), s, pod())
        self.assertEqual(s.marks, [], "the mutant must leave the pod unmarked where the control marks it")


# ---- clause 3: the four fixed words of the end notice, from stub run records ----
import json  # noqa: E402
import shutil  # noqa: E402
import tempfile  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "..", "bin"))
import cloud_end_alert as cea  # noqa: E402
import record_json  # noqa: E402

CEA_SOURCE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "bin", "cloud_end_alert.py")
HEAD_LINE = '        return "head is on branch " + name(head) if name(head) else "unknown"\n'
OLD_NO = '        return "work pushed: no"\n'


def words(hook, module=cea):
    d = tempfile.mkdtemp()
    try:
        if hook is not None:
            with open(os.path.join(d, "run.json"), "w") as f:
                json.dump({"end_hook": hook}, f)
        return module.pushed_text(d, record_json.parse_bounded)
    finally:
        shutil.rmtree(d)


def cea_mutant(old, new):
    with open(CEA_SOURCE) as f:
        text = f.read()
    after = text.replace(old, new)
    assert after != text and text.count(old) == 1, "the mutation was not applied"
    module = types.ModuleType("cea_mutant")
    exec(compile(after, "cea_mutant", "exec"), module.__dict__)
    return module


class TestOneActivityReadPerPod(unittest.TestCase):
    def test_a_passed_activity_is_not_read_again(self):
        s = Stub()
        reads = []
        s.activity = lambda name: reads.append(name) or (False, NOW - dt.timedelta(minutes=20))
        acts = {"w1-pod": (False, NOW - dt.timedelta(minutes=20))}
        with contextlib.redirect_stderr(io.StringIO()):
            cw.Launcher.send_idle_nudges(s, [pod()], NOW, acts)
        self.assertEqual(reads, [], "%d activity reads where 0 are required" % len(reads))
        self.assertEqual(s.sent, [cw.IDLE_NUDGE])

    def test_a_pod_not_in_the_dict_falls_back_to_one_read(self):
        s = Stub()
        reads = []
        s.activity = lambda name: reads.append(name) or (False, NOW - dt.timedelta(minutes=20))
        with contextlib.redirect_stderr(io.StringIO()):
            cw.Launcher.send_idle_nudges(s, [pod()], NOW, {})
        self.assertEqual(len(reads), 1)
        self.assertEqual(s.sent, [cw.IDLE_NUDGE])


class TestNoticeWords(unittest.TestCase):
    def test_four_words(self):
        self.assertEqual(words({"pushed": False, "wip_branch": None, "head_on": "feat/a-1"}), "head is on branch feat/a-1")
        self.assertEqual(words({"pushed": True, "wip_branch": "w1/wip", "head_on": None}), "unpushed work saved to w1/wip")
        self.assertEqual(words({"pushed": False, "wip_branch": None, "head_on": None}), "nothing unpushed")
        self.assertEqual(words(None), "unknown")
        self.assertEqual(words({"head_on": "x"}), "unknown")

    def test_never_work_pushed_no_for_a_head_on_the_remote(self):
        self.assertNotIn("work pushed", words({"pushed": False, "wip_branch": None, "head_on": "main2"}))

    def test_unknown_head_and_error_are_unknown(self):
        self.assertEqual(words({"pushed": False, "wip_branch": None, "head_on": "unknown"}), "unknown")
        self.assertEqual(words({"pushed": False, "wip_branch": None, "head_on": None, "error": "x"}), "unknown")

    def test_no_token_form_in_a_notice(self):
        for bad in ("ghp_" + "a" * 36, "sk-" + "b" * 20, "a" * 40, "x y", "a;b"):
            for hook in ({"pushed": False, "wip_branch": None, "head_on": bad}, {"pushed": True, "wip_branch": bad, "head_on": None}):
                self.assertEqual(words(hook), "unknown", bad)

    def test_longest_notice_is_short(self):
        self.assertLessEqual(len(words({"pushed": False, "wip_branch": None, "head_on": "a" * 29 + "/" + "b" * 29})), len("head is on branch ") + 100)


EH_PATH = os.path.join(HERE, "..", "end_hook.py")
EH_OLD = '    lines = [x for x in lines if "->" not in x and x != "origin/HEAD"]\n'


def eh_module(mutate=None):
    text = open(EH_PATH).read()
    after = text.replace(EH_OLD, "") if mutate else text
    if mutate:
        assert after != text and text.count(EH_OLD) == 1, "the mutation was not applied"
    module = types.ModuleType("eh_mutant")
    module.__file__ = EH_PATH
    exec(compile(after, "eh_mutant", "exec"), module.__dict__)
    return module


class TestHeadBranch(unittest.TestCase):
    def test_origin_head_line_is_skipped(self):
        self.assertEqual(eh_module().head_branch("  origin/HEAD -> origin/main\n  origin/main"), "main")

    def test_only_an_arrow_line_is_unknown(self):
        self.assertEqual(eh_module().head_branch("  origin/HEAD -> origin/main"), "unknown")

    def test_existing_cases(self):
        self.assertEqual(eh_module().head_branch("  origin/main2"), "main2")
        self.assertEqual(eh_module().head_branch(""), "unknown")

    def test_old_first_line_rule_is_caught(self):
        got = eh_module(True).head_branch("  origin/HEAD -> origin/main\n  origin/main")
        self.assertEqual(got, "HEAD")  # the mutant says HEAD
        self.assertNotEqual(got, "main", "the mutant must show HEAD where main is required")

    def test_no_op_mutation_fails_the_computation(self):
        text = open(EH_PATH).read()
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            assert text.replace("zzz", "") != text and text.count(EH_OLD) == 1, "the mutation was not applied"

    def test_unknown_head_unpushed_and_long_run(self):
        self.assertEqual(words({"pushed": False, "wip_branch": None, "head_on": "unknown"}), "unknown")
        self.assertEqual(words({"pushed": False, "wip_branch": None, "head_on": "feat/" + "a1" * 15}), "unknown")


class TestNoticeMutant(unittest.TestCase):
    def test_control_passes(self):
        self.assertEqual(words({"pushed": False, "wip_branch": None, "head_on": "b1"}), "head is on branch b1")

    def test_no_op_mutation_fails_the_computation(self):
        with self.assertRaisesRegex(AssertionError, "the mutation was not applied"):
            cea_mutant(HEAD_LINE, HEAD_LINE)

    def test_notice_that_keeps_no_for_a_head_on_the_remote_is_caught(self):
        m = cea_mutant(HEAD_LINE, OLD_NO)
        got = words({"pushed": False, "wip_branch": None, "head_on": "b1"}, m)
        self.assertEqual(got, "work pushed: no")  # the mutant says the old words
        self.assertNotEqual(got, "head is on branch b1", "the mutant must show the old words where `head is on branch` is required")


if __name__ == "__main__":
    unittest.main()
