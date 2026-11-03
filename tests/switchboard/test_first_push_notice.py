"""The first-push notice (#2057): a cloud pod with no pushed branch and no report by its launch's checkpoint is named once to its orchestrator."""
import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from support.loader import sb
from support.loader import SWITCHBOARD

TOKEN = "tok-SECRET-value-9f3"
BASE = 1_000_000.0
def CW():
    return sb.load_module("cloud_worker").bind(vars(sb))


MAIN, WORK = "a" * 40, "b" * 40


def listing(branch_head, main=MAIN):
    lines = ["%s\tHEAD" % main] + (["%s\trefs/heads/feature" % branch_head] if branch_head else [])
    return subprocess.CompletedProcess([], 0, "\n".join(lines) + "\n", "")


class TestFirstPushNotice(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock, self.sent, self.reads, self.answer = BASE + 1900, [], [], listing(None)

        def runner(argv, **kw):
            self.reads.append(argv)
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        self.engine = types.SimpleNamespace(state_dir=self.dir.name, now=lambda: self.clock, audit=None, actor="t", first_push_runner=runner,
                                            send=lambda frm, to, text: self.sent.append((frm, to, text)))
        CW().record_first_push_watch(self.dir.name, "coder7", "lead", "acme/widget-api", "feature", BASE + 1800, BASE)

    def report(self, at):
        sb._write_small(self.dir.name, sb.LAST_REPORT_FILE, {"coder7": {"at": at}})

    def tick(self):
        return CW().first_push_tick(self.engine)

    def test_a_pod_with_no_push_and_no_report_at_its_checkpoint_is_noticed_once(self):
        self.assertEqual(self.tick(), ["coder7"])
        self.assertEqual(len(self.sent), 1)
        frm, to, text = self.sent[0]
        self.assertEqual(to, "lead")
        self.assertIn("no first push", text)
        self.assertIn("coder7", text)
        self.assertIn(sb.iso(BASE + 1800), text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(self.reads[0][:3], ["git", "ls-remote", "https://github.com/acme/widget-api.git"])
        self.assertEqual(len(self.reads), 1)   # one read of the remote

    def test_before_the_checkpoint_nothing_is_read_or_sent(self):
        self.clock = BASE + 100
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.sent, self.reads), ([], []))

    def test_a_pod_that_pushed_a_pod_that_reported_and_a_pod_already_noticed_get_none_on_a_second_pass(self):
        self.answer = listing(WORK)   # the branch head differs from HEAD: coder7 pushed
        self.assertEqual(self.tick(), [])
        CW().record_first_push_watch(self.dir.name, "coder8", "lead", "acme/widget-api", "feature", BASE + 1800, BASE)
        sb._write_small(self.dir.name, sb.LAST_REPORT_FILE, {"coder8": {"at": BASE + 600}})   # coder8 reported
        self.assertEqual(self.tick(), [])
        self.answer = listing(None)
        CW().record_first_push_watch(self.dir.name, "coder9", "lead", "acme/widget-api", "feature", BASE + 1800, BASE)
        self.assertEqual(self.tick(), ["coder9"])   # the noticed pod
        self.assertEqual(len(self.sent), 1)
        for _ in range(2):   # the second pass: none of the three gets a notice
            self.assertEqual(self.tick(), [])
        self.assertEqual(len(self.sent), 1, "a second pass sends no second notice")

    def test_a_branch_equal_to_head_is_no_push_and_a_report_before_the_launch_is_not_a_report(self):
        self.answer = listing(MAIN)
        self.report(BASE - 50)
        self.assertEqual(self.tick(), ["coder7"])

    def test_a_report_since_the_launch_means_no_notice_and_no_read(self):
        self.report(BASE + 600)
        self.assertEqual(self.tick(), [])
        self.assertEqual((self.sent, self.reads), ([], []))

    def test_a_failed_or_timed_out_read_raises_no_notice_and_is_retried(self):
        for bad in (subprocess.TimeoutExpired(["git"], 20), OSError("x"), subprocess.CompletedProcess([], 128, "", "fatal: " + TOKEN)):
            self.answer = bad
            self.assertEqual(self.tick(), [])
        self.assertEqual(self.sent, [])
        self.answer = listing(None)
        self.assertEqual(self.tick(), ["coder7"])   # the next pass tells it
        self.assertEqual(len(self.sent), 1)

    def test_a_launch_records_its_watch_from_the_checkpoint_flag_or_the_brief(self):
        module = CW()
        args = argparse.Namespace(reports_to="lead", checkpoint="30m")
        module._watch_first_push(self.dir.name, "coder1", args, "acme/widget-api", "feature", "", BASE)
        brief = "CHECKPOINT: report at 20 minutes; overdue at 40 minutes"
        module._watch_first_push(self.dir.name, "coder2", argparse.Namespace(reports_to="lead", checkpoint=None), "acme/widget-api", "feature", brief, BASE)
        module._watch_first_push(self.dir.name, "coder3", argparse.Namespace(reports_to=None, checkpoint="30m"), "acme/widget-api", "feature", "", BASE)
        watch = sb._read_small(self.dir.name, module.FIRST_PUSH_FILE)
        self.assertEqual(watch["coder1"]["at"], BASE + 1800)
        self.assertEqual(watch["coder2"]["at"], BASE + 1200)
        self.assertNotIn("coder3", watch)
        self.assertEqual(watch["coder1"]["boss"], "lead")


CW_SOURCE = sb.load_module("cloud_worker").__file__
SB_SOURCE = SWITCHBOARD


def mutated_module(source):
    """The cloud worker module executed from `source` (a textual variant), bound to the switchboard's names."""
    mod = types.ModuleType("cw_first_push_mutant")
    mod.__dict__["__file__"] = CW_SOURCE
    sys.modules["cw_first_push_mutant"] = mod
    try:
        exec(compile(source, "cw_first_push_mutant", "exec"), mod.__dict__)
        return mod.bind(vars(sb))
    finally:
        sys.modules.pop("cw_first_push_mutant", None)


class FakeCluster:
    def __init__(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def add(self, secret):
        pass
    def clean(self, text, limit=0):
        return text


class FakeV2:
    def session_create(self, *a, **k):
        return {"id": "ses1"}
    def prompt(self, *a, **k):
        pass


def drive_launch(module, state):
    """The real `_launch` of `module` with the cluster, the launcher and the tunnel stubbed; returns the exit code."""
    args = argparse.Namespace(name="coder5", agent="worker", reports_to="lead", checkpoint="30m", resume=False, headless=False, dir=None, shape=None,
                              stack=False, brief_file=None, idle=None, hard=None, role="coder", issue=None, charter=None, model=None, reasoning=None, brief=None, branch="feature", repo="acme/widget-api")
    engine = types.SimpleNamespace(now=lambda: BASE, discover=lambda at: types.SimpleNamespace(agents={}), config={"call_timeout_seconds": 5}, audit=None, actor="t")
    lib = types.SimpleNamespace(check_repo_preflight=lambda repo, env: "ok", CloudError=Exception, AGENTS=("worker",))
    stubs = dict(use_settings=lambda: "test", check_allowed=lambda repo: None, _lib=lambda: lib, launcher_argv=lambda: ["x"], VaultRecovery=lambda *a: None,
                 Cluster=FakeCluster, model_entry=lambda: "m", git_token=lambda *a, **k: "t", run_launcher=lambda *a, **k: (0, {}, ""),
                 _read_password=lambda fd: "pw", _record_from_create=lambda n, b: {"runtime_id": "r1", "url": "https://pod.example"},
                 _write_private=lambda *a: None, save_record=lambda *a: None, load_record=lambda *a: None, _audit=lambda *a, **k: None,
                 tunnel=lambda cluster, record: contextlib.nullcontext(object()), remote_v2=lambda *a: FakeV2(), _registry_note=lambda *a, **k: None)
    heads = {"launch_repo": lambda a: "acme/widget-api", "build_engine": lambda *a: (engine, state, None), "read_brief": lambda *a: "",
             "fleet_record": lambda *a: {}}
    with contextlib.ExitStack() as stack:
        for key, value in stubs.items():
            stack.enter_context(mock.patch.object(module, key, value, create=True))
        stack.enter_context(mock.patch.dict(module.H, heads))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        return module._launch(args, None, lambda: BASE, lambda state: None)


def drive_pass(finish_pass, state):
    """One pass of `finish_pass` with a due watch in the state; the other ticks are quiet or fail into engine._failed. Returns what was sent."""
    sent = []
    CW().record_first_push_watch(state, "coder7", "lead", "acme/widget-api", "feature", BASE + 1800, BASE)
    engine = types.SimpleNamespace(state_dir=state, now=lambda: BASE + 1900, audit=None, actor="t", first_push_runner=lambda argv, **kw: listing(None),
                                   send=lambda frm, to, text: sent.append((to, text)), _failed=lambda *a: None)
    quiet = lambda *a, **k: None
    with mock.patch.multiple(sb, health_write=quiet, prune_delivered_reminders=quiet, friction_tick=quiet, load_tick=quiet, prune_report_riders=quiet,
                             publish_decisions=quiet, write_catching_up=quiet, schedule_judge_warm=quiet, cloud_watch_tick=quiet):
        finish_pass(engine, None, {"errors": []})
    return sent


def finish_pass_variant(old, new):
    """`finish_pass` compiled from the daemon's text with `old` replaced by `new` (a copy of the names; the real one is untouched)."""
    text = SB_SOURCE.read_text()
    start = text.index("\ndef finish_pass(") + 1
    body = text[start:text.index("\ndef ", start + 10)]
    assert body.count(old) == 1, "the anchored text is in finish_pass once"
    space = dict(vars(sb))
    exec(compile(body.replace(old, new), "finish_pass_variant", "exec"), space)
    return space["finish_pass"]


class TestFirstPushWiring(unittest.TestCase):
    """The two inserted calls are tested through their entries: `_launch` writes the watch, `finish_pass` raises the notice."""
    module = None
    finish = None

    def test_the_launch_writes_its_watch_record(self):
        with tempfile.TemporaryDirectory() as state:
            module = self.module or CW()
            self.assertEqual(drive_launch(module, state), sb.EXIT_OK)
            watch = sb._read_small(state, module.FIRST_PUSH_FILE)
            self.assertEqual(watch.get("coder5", {}).get("boss"), "lead", "the launch wrote its first-push watch")
            self.assertEqual(watch["coder5"]["at"], BASE + 1800)

    def test_the_real_pass_raises_the_first_push_notice(self):
        with tempfile.TemporaryDirectory() as state:
            sent = drive_pass(self.finish or sb.finish_pass, state)
            self.assertEqual([to for to, text in sent], ["lead"], "the pass raised the first-push notice")
            self.assertIn("no first push", sent[0][1])


WIRING = ("test_the_launch_writes_its_watch_record", "test_the_real_pass_raises_the_first_push_notice")
PUSHED = "test_a_pod_that_pushed_a_pod_that_reported_and_a_pod_already_noticed_get_none_on_a_second_pass"


class TestFirstPushMutants(unittest.TestCase):
    """Each mutant is a textual replacement in a copy; the control (no change) passes first; the anchored text is in the source once; the named test fails."""

    def run_named(self, case, name, **attrs):
        class Variant(case):
            pass
        for key, value in attrs.items():
            setattr(Variant, key, staticmethod(value) if callable(value) else value)
        result = unittest.TestResult()
        Variant(name).run(result)
        return result

    def test_deleting_the_launch_call_fails_the_launch_test(self):
        old = "    _watch_first_push(state, name, args, repo, branch, brief, now())"
        self.check_launch(old, "    pass")

    def check_launch(self, old, new):
        text = Path(CW_SOURCE).read_text()
        self.assertEqual(text.count(old), 1, "anchored text")
        name = WIRING[0]
        control = self.run_named(TestFirstPushWiring, name, module=mutated_module(text))
        self.assertEqual((control.failures, control.errors), ([], []), "control passes")
        result = self.run_named(TestFirstPushWiring, name, module=mutated_module(text.replace(old, new)))
        self.assertEqual(len(result.failures), 1)
        self.assertIn(name, str(result.failures[0][0]))

    def test_deleting_the_pass_call_fails_the_pass_test(self):
        old = "load_module(\"cloud_worker\").bind(globals()).first_push_tick(engine, discovery)"
        name = WIRING[1]
        control = self.run_named(TestFirstPushWiring, name, finish=finish_pass_variant(old, old))
        self.assertEqual((control.failures, control.errors), ([], []), "control passes")
        result = self.run_named(TestFirstPushWiring, name, finish=finish_pass_variant(old, "None"))
        self.assertEqual(len(result.failures), 1)
        self.assertIn(name, str(result.failures[0][0]))

    def notice_variant(self, old, new, name):
        text = Path(CW_SOURCE).read_text()
        self.assertEqual(text.count(old), 1, "anchored text")
        for source, passes in ((text, True), (text.replace(old, new), False)):
            module = mutated_module(source)
            with mock.patch.dict(globals(), {"CW": lambda: module}):
                result = self.run_named(TestFirstPushNotice, name)
            if passes:
                self.assertEqual((result.failures, result.errors), ([], []), "control passes")
            else:
                self.assertEqual(len(result.failures), 1)
                self.assertIn(name, str(result.failures[0][0]))

    def test_a_mutant_that_notices_a_pod_that_pushed_fails(self):
        self.notice_variant("        if pushed:\n            close(pod, \"pushed\")\n            continue\n", "        if False:\n            close(pod, \"pushed\")\n            continue\n", PUSHED)

    def test_a_mutant_with_no_once_per_pod_record_fails(self):
        self.notice_variant("        close(pod, \"noticed\")\n", "        pass\n", PUSHED)


if __name__ == "__main__":
    unittest.main()
