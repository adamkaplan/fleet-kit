#!/usr/bin/env python3
"""Scenarios S2..S9 and U1 of ask #1750: `read NAME` for a worker the reaper stopped shows the saved last message of its run record.

The fixtures are those of bin/test-cloud-retired (loaded from there, nothing copied). The tunnel and the cluster snapshot are fakes; the mutants are
the real functions with one line changed, asserted applied, each beside its control.
"""
import ast
import contextlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[3] / "bin"
_loader = importlib.machinery.SourceFileLoader("cw_tests_of_the_retired", str(BIN / "test-cloud-retired"))
rt = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw_tests_of_the_retired", _loader))
sys.modules["cw_tests_of_the_retired"] = rt
_loader.exec_module(rt)

DOCS = BIN.parent / "docs" / "switchboard.md"
RESULTS = {}


def count(key, ok=True):
    if ok:
        RESULTS[key] = RESULTS.get(key, 0) + 1


class FakeCluster:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRemote:
    def __init__(self, messages):
        self.messages = messages

    def message_list(self, session, limit, order):
        return self.messages


class Stopped(rt.Retired):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.tunnel_works = False
        self.snapshot_pods = []
        self.snapshot_fails = False
        self.live = [rt.msg("assistant", "LIVE text")]
        self.armed = False

    def arm(self):
        """The fakes of the cluster, put on after the launch (which uses the real stub launcher)."""
        if not self.armed:
            self.armed = True
            self.patch("tunnel", self.fake_tunnel)
            self.patch("Cluster", lambda *a, **k: FakeCluster())
            self.patch("_cluster_snapshot", self.fake_snapshot)
            self.patch("remote_v2", lambda state, live, timeout: FakeRemote(self.live))

    def patch(self, name, value):
        p = mock.patch.object(self.cw, name, value)
        p.start()
        self.addCleanup(p.stop)

    @contextlib.contextmanager
    def fake_tunnel(self, cluster, record):
        if not self.tunnel_works:
            raise self.cw.CloudError("the tunnel to %s did not open: pod not found" % record["worker"])
        yield "live"

    def fake_snapshot(self, runner, cluster, timeout):
        self.calls.append("snapshot")
        if self.snapshot_fails:
            raise self.cw.CloudError("the cluster was not read (down)")
        return {"pods": self.snapshot_pods}

    def stopped_record(self, text="THE SAVED TEXT", reason="idle", ended=None, lines=None):
        self.assertEqual(self.launch("coder7")[0], 0)   # the cloud record stays: the reaper does not remove it
        ended = ended or rt.stamp(-5)
        directory = self.run_record("coder7", ended, lines if lines is not None else [rt.msg("assistant", text)])
        info = json.loads((directory / "run.json").read_text())
        info["exit_reason"] = reason
        (directory / "run.json").write_text(json.dumps(info) + "\n")
        return directory, ended

    def read(self, kind=None):
        self.arm()
        return self.as_caller(kind or rt.KIND_BOSS, "read", "coder7")

    def mutant(self, module, function, old, new):
        text = Path(module.__file__).read_text()
        node = [n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == function][0]
        source = "\n".join(text.splitlines()[node.lineno - 1:node.end_lineno]) + "\n"
        self.assertIn(old, source, "the mutation was not applied")
        scope = dict(vars(module))
        exec(compile(source.replace(old, new), "<mutant>", "exec"), scope)
        p = mock.patch.object(module, function, scope[function])
        p.start()
        self.addCleanup(p.stop)

    def check_s2(self):
        directory, ended = self.stopped_record()
        code, out, err = self.read()
        if code != 0:
            raise AssertionError("the record fallback is missing: %s" % (out + err).strip()[:120])
        lines = out.splitlines()
        self.assertIn("a run stopped by the reaper (reason: idle)", lines[0])
        self.assertIn(ended, lines[0])
        self.assertEqual(lines[1:], ["THE SAVED TEXT"])


class TestStoppedRead(Stopped):
    def test_s2_a_stopped_and_a_retired_record_are_read(self):
        self.check_s2()
        count("s2")

    def test_s2_the_retired_read_is_unchanged(self):
        self.retire(name="coder8")
        self.run_record("coder8", rt.stamp(-5), [rt.msg("assistant", "RETIRED TEXT")])
        self.arm()
        code, out, err = self.as_caller(rt.KIND_BOSS, "read", "coder8")
        self.assertEqual(code, 0, err)
        self.assertIn("record of a retired run", out.splitlines()[0])
        self.assertEqual(out.splitlines()[1:], ["RETIRED TEXT"])
        self.assertNotIn("stopped by the reaper", out)
        count("s2")

    def test_s2_a_record_without_a_reaper_reason_keeps_the_retired_words(self):
        self.stopped_record(reason="retired")
        code, out, err = self.read()
        self.assertEqual(code, 0, err)
        self.assertIn("record of a retired run", out.splitlines()[0])

    def test_s3_a_running_worker_is_read_live_and_takes_no_snapshot(self):
        self.assertEqual(self.launch("coder7")[0], 0)
        self.tunnel_works = True
        code, out, err = self.read()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, self.cw.message_line(self.live[0]) + "\n")
        self.assertEqual(self.calls, [])
        count("s3")
        count("s9")

    def test_s3_a_pod_still_present_raises_the_tunnel_error_with_a_clause(self):
        self.stopped_record()
        self.snapshot_pods = [{"name": "coder7", "worker": "coder7"}]
        code, out, err = self.read()
        self.assertNotEqual(code, 0)
        self.assertIn("did not open", out + err)
        self.assertIn("the pod is still there", out + err)
        self.assertNotIn("THE SAVED TEXT", out)
        count("s3")

    def test_s3_an_unreadable_snapshot_raises_the_tunnel_error_with_a_clause(self):
        self.stopped_record()
        self.snapshot_fails = True
        code, out, err = self.read()
        self.assertNotEqual(code, 0)
        self.assertIn("did not open", out + err)
        self.assertIn("the cluster could not be read", out + err)
        self.assertNotIn("THE SAVED TEXT", out)
        count("s3")

    def test_s3_a_name_with_nothing_refuses_in_one_plain_line(self):
        code, out, err = self.as_caller(rt.KIND_BOSS, "read", "ghost")
        self.assertNotEqual(code, 0)
        self.assertIn("is not a fl" + "eet agent this switchboard has seen", out + err)
        count("s3")

    def test_s3_a_pod_gone_with_no_run_record_says_no_record_exists(self):
        self.assertEqual(self.launch("coder7")[0], 0)
        code, out, err = self.read()
        self.assertEqual(code, 1)
        self.assertIn("no run record exists for coder7: its pod is gone", out + err)
        self.assertEqual(len((out + err).strip().splitlines()), 1)

    def test_s3_control_the_unmutated_fallback_passes(self):
        self.check_s2()
        count("s3c")

    def test_s3_a_copy_without_the_fallback_fails_with_the_anchored_text(self):
        self.mutant(self.cw, "_read_stopped_run", "retired = _retired().retired_run(name)", "retired = None")
        with self.assertRaises(AssertionError) as caught:
            self.check_s2()
        self.assertIn("the record fallback", str(caught.exception))
        count("s3m")

    def test_s5_the_scrub_hides_a_token_and_keeps_a_path_and_an_address(self):
        text = "token %s in /home/someone/work/file.txt at https://host.example.internal:8443/x" % rt.TOKEN
        self.stopped_record(text=text)
        code, out, err = self.read()
        self.assertEqual(code, 0, err)
        self.assertNotIn(rt.TOKEN, out)
        self.assertIn("/home/someone/work/file.txt", out)
        self.assertIn("https://host.example.internal:8443/x", out)
        count("s5")

    def test_s5_the_documentation_states_the_limit(self):
        self.assertTrue("a path or an address in a saved text is printed as it was saved" in DOCS.read_text())

    def test_s6_the_callers_rule(self):
        self.stopped_record()
        for kind in (rt.KIND_BOSS, rt.KIND_PERSON, rt.KIND_COS):
            code, out, err = self.read(kind)
            self.assertEqual((code, out.splitlines()[1:]), (0, ["THE SAVED TEXT"]), err)
            count("s6")
        code, out, err = self.read(rt.KIND_OTHER_LANE)
        self.assertNotEqual(code, 0)
        self.assertIn("refused: only the operator, the Chief of Staff or coder7's own boss may read it", out + err)
        self.assertNotIn("THE SAVED TEXT", out)
        count("s6r")

    def test_s6_control_and_a_copy_that_ignores_the_boss_of_the_record(self):
        self.stopped_record()
        self.assertEqual(self.read()[0], 0)   # the control
        self.mutant(self.part, "read_run_record", 'boss = record.get("reports_to")', "pass")
        code, out, err = self.read()
        if code == 0:
            self.fail("the boss of the record was ignored but the read passed")
        self.assertIn("refused", out + err)   # the boss of the record is what lets its own boss read
        count("s6m")

    def test_s9_one_snapshot_for_the_stopped_worker_and_no_write(self):
        self.stopped_record()
        before = self.snapshot()
        code, out, err = self.read()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls, ["snapshot"])
        self.assertNotIn(str(self.runs), out + err)
        self.assertEqual(self.snapshot(), before)
        count("s9")


class TestFailureModes(Stopped):
    def gone(self):
        self.assertEqual(self.launch("coder7")[0], 0)

    def answer(self):
        before = self.snapshot()
        code, out, err = self.read()
        text = out + err
        self.assertEqual(code, 1, text)
        self.assertEqual(len(text.strip().splitlines()), 1, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn(str(self.runs), text)
        self.assertEqual(self.snapshot(), before)
        count("s4")
        return text

    def test_the_run_record_is_missing(self):
        self.gone()
        self.assertIn("no run record exists", self.answer())

    def test_the_transcript_is_missing(self):
        self.gone()
        self.run_record("coder7", rt.stamp(-5), None)
        self.assertIn("holds no transcript", self.answer())

    def test_the_transcript_has_no_text_from_the_worker(self):
        self.gone()
        self.run_record("coder7", rt.stamp(-5), [rt.msg("user", "only the user")])
        self.assertIn("holds no text from the worker", self.answer())

    def test_a_truncated_last_line_prints_the_last_complete_text(self):
        self.gone()
        raw = json.dumps(rt.msg("assistant", "COMPLETE")) + "\n" + '{"type": "assistant", "content": [{"type": "te'
        self.run_record("coder7", rt.stamp(-5), None, raw=raw)
        code, out, err = self.read()
        self.assertEqual((code, out.splitlines()[1:]), (0, ["COMPLETE"]), err)
        self.assertNotIn("Traceback", out + err)
        count("s4")

    def test_a_transcript_larger_than_the_bound_is_read_only_up_to_the_bound_plus_one(self):
        self.gone()
        limit = self.part.TRANSCRIPT_READ_MAX
        self.run_record("coder7", rt.stamp(-5), None, raw="x" * (limit + 4096))
        total, peak, real = {}, [0], os.read

        def counting(fd, size):
            data = real(fd, size)
            total[fd] = 0 if not data else total.get(fd, 0) + len(data)   # an end of file closes that read: a number is reused
            peak[0] = max(peak[0], total[fd])
            return data
        with mock.patch.object(self.part.os, "read", counting):
            text = self.answer()
        self.assertIn("too large", text)
        self.assertLessEqual(peak[0], limit + 1)

    def test_a_run_directory_that_is_a_symbolic_link(self):
        self.gone()
        real = self.home / "elsewhere"
        real.mkdir()
        (real / "run.json").write_text("{}")
        (self.runs / ("coder7-%s-aaaaaa" % rt.stamp(-5).replace("-", "").replace(":", ""))).symlink_to(real)
        self.assertIn("no run record exists", self.answer())

    def test_a_transcript_that_cannot_be_read(self):
        self.gone()
        directory = self.run_record("coder7", rt.stamp(-5), None)
        os.mkfifo(str(directory / "transcript.jsonl"))   # not a regular file: refused without a block
        self.assertIn("cannot be read", self.answer())


class TestDocumentation(unittest.TestCase):
    PHRASES = ("three states", "the live messages", "the text of the record of a retired run", "the text of the record of a run stopped by the reaper",
               "writes nothing and prints no path", "a path or an address in a saved text is printed as it was saved")

    def section(self, text):
        start = text.index("**`read` of a retired cloud worker (#1546).**")
        mark = text.index("(#1750).**", start)
        return text[start:text.index("\n\n", mark) if "\n\n" in text[mark:] else len(text)]

    def test_s7_the_section_says_each_state(self):
        section = self.section(DOCS.read_text())
        found = [p for p in self.PHRASES if p in section]
        self.assertEqual(found, list(self.PHRASES), "three states")
        count("s7")

    def test_s7_a_copy_without_the_text_fails(self):
        copy = self.section(DOCS.read_text()).replace("three states", "")
        with self.assertRaises(AssertionError) as caught:
            self.assertEqual([p for p in self.PHRASES if p in copy], list(self.PHRASES), "three states")
        self.assertIn("three states", str(caught.exception))


class TestWholeFileMutants(Stopped):
    """S10 and S11 (#1750): the decorator of cloud-clear and the Refusal of the cluster step of read, each pinned by a scratch copy of bin/cloud_worker.py."""

    def scratch(self, old=None, new=None):
        text = (BIN / "cloud_worker.py").read_text()
        if old is not None:
            self.assertEqual(text.count(old), 1, "the mutated text does not occur once")
            mutated = text.replace(old, new)
            self.assertNotEqual(mutated, text, "the mutation was not applied")
            text = mutated
        scope = dict(vars(self.cw))
        exec(compile(text, "<scratch cloud_worker>", "exec"), scope)
        scope["H"] = self.cw.H
        return scope

    def put(self, scope, name):
        p = mock.patch.object(self.cw, name, scope[name])
        p.start()
        self.addCleanup(p.stop)

    def check_s10(self, scope):
        def records(state):
            raise scope["Refusal"]("the records were refused")
        scope["list_records"] = records
        self.put(scope, "cmd_cloud_clear")
        self.assertEqual(self.launch("coder7")[0], 0)
        try:
            code, out, err = self.as_caller(rt.KIND_BOSS, "cloud-clear")
        except Exception as raised:
            raise AssertionError("cloud-clear no longer turns a Refusal into exit 2 (%s escaped)" % type(raised).__name__)
        if code != 2 or len(err.strip().splitlines()) != 1:
            raise AssertionError("cloud-clear no longer turns a Refusal into exit 2 (exit %s)" % code)

    def check_s11(self, scope):
        reads = []

        @contextlib.contextmanager
        def tunnel(cluster, record):
            raise scope["Refusal"]("the vault pre-check refused")
            yield
        scope["tunnel"] = tunnel
        scope["Cluster"] = lambda *a, **k: FakeCluster()
        scope["_cluster_snapshot"] = self.fake_snapshot
        scope["_retired"] = lambda: reads.append("read") or self.part
        self.put(scope, "cmd_read")
        self.stopped_record()
        code, out, err = self.as_caller(rt.KIND_BOSS, "read", "coder7")
        if code != 2 or len(err.strip().splitlines()) != 1 or reads:
            raise AssertionError("read turned a Refusal into the stopped-run fallback (exit %s)" % code)

    def test_s10_cloud_clear_still_turns_a_refusal_into_exit_2_with_one_line(self):
        self.check_s10(self.scratch())
        RESULTS["s10c"] = 1

    def test_s10_a_copy_without_the_decorator_fails_with_the_anchored_text(self):
        with self.assertRaises(AssertionError) as caught:
            self.check_s10(self.scratch("@refusals\ndef cmd_cloud_clear", "def cmd_cloud_clear"))
        self.assertIn("cloud-clear no longer turns a Refusal into exit 2", str(caught.exception))
        RESULTS["s10m"] = 1

    def test_s11_a_refusal_of_the_cluster_step_exits_2_and_reads_no_record(self):
        self.check_s11(self.scratch())
        RESULTS["s11c"] = 1

    def test_s11_a_copy_without_the_two_lines_fails_with_the_anchored_text(self):
        with self.assertRaises(AssertionError) as caught:
            self.check_s11(self.scratch("        except Refusal:\n            raise\n        except CloudError as err:", "        except CloudError as err:"))
        self.assertIn("read turned a Refusal into the stopped-run fallback", str(caught.exception))
        RESULTS["s11m"] = 1


class TestDiff(unittest.TestCase):
    def test_s8_no_test_line_is_removed(self):
        base = subprocess.run(["git", "merge-base", "HEAD", "origin/integration"], cwd=str(BIN.parent), capture_output=True, text=True)
        if base.returncode != 0:
            self.skipTest("no integration branch in this checkout")
        diff = subprocess.run(["git", "diff", base.stdout.strip(), "--", "bin/cloud_worker.py", "bin/cloud_retired.py", "cloud/launcher/tests", "bin/test-cloud-retired"],
                              cwd=str(BIN.parent), capture_output=True, text=True).stdout
        removed = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
        self.assertEqual(len(removed), 4, removed)   # 2 + 1 + 1: the session line of the first group is unchanged context
        count("s8")


def tearDownModule():
    print("S10: cloud-clear refusal exits 2 with 1 line, control passes %d of 1, mutant fails %d of 1 with the anchored text, mutation applied True"
          % (RESULTS.get("s10c", 0), RESULTS.get("s10m", 0)), file=sys.stderr)
    print("S11: a refusal of the cluster step exits 2 with 1 line and reads no record, control passes %d of 1, mutant fails %d of 1 with the anchored text, mutation applied True"
          % (RESULTS.get("s11c", 0), RESULTS.get("s11m", 0)), file=sys.stderr)
    print("S2: %d of 2; S3: reads %d of 4, control %d, mutant %d; S5: %d of 1; S6: allowed %d of 3, refused %d, mutant %d; S7: %d; S8: %d; S9: %d; S4: %d of 7"
          % tuple(RESULTS.get(k, 0) for k in ("s2", "s3", "s3c", "s3m", "s5", "s6", "s6r", "s6m", "s7", "s8", "s9", "s4")), file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
