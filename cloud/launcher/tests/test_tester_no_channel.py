"""Ask #1736: a pod with no hub setting says so at launch (S8), and the stop line of a pod names the kind of its last session event (S3, S4, S6).
Fakes only: the fake cluster of test_cloud_worker and the stub launcher of bin/test-cloud-worker. Mutants are counted, each with its control first."""
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as tcw  # noqa: E402
import run_record as rr  # noqa: E402

ROOT = Path(HERE).resolve().parents[2]
cw = tcw.cw
MUTANTS = []   # (name, failed) counted at the end

REFUSAL = "the option --allow-escape-sequences is not available in this client"
PLANTED = ("https://secret.example.test/x", "/home/worker/private/file.txt", "ghp_" + "a" * 36)


def refused_session(command="gh api --allow-escape-sequences repos/o/r/actions/jobs/1/logs"):
    return [{"type": "user", "text": "brief"},
            {"type": "assistant", "content": [{"type": "tool", "tool": "bash", "state": {"status": "error", "input": {"command": command}, "output": REFUSAL}}]}]


def phrase(msgs, module=rr):
    return module.last_event_phrase(msgs, lambda t: cw.scrub(t, limit=160))


def load_launch_base():
    path = ROOT / "bin" / "test-cloud-worker"
    loader = importlib.machinery.SourceFileLoader("cw_for_tester_no_channel", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw_for_tester_no_channel", loader))
    sys.modules["cw_for_tester_no_channel"] = module
    loader.exec_module(module)
    return module


class Launch(load_launch_base().Base):
    """S8: the line on standard error of `launch --cloud`, for three roles, with and without the hub setting."""
    LINE = ("no channel", "run record", "retire")

    def counter(self):
        self.n = getattr(self, "n", 0) + 1
        return self.n

    def launch_role(self, role, hub, command=None):
        brief = self.home / "brief.txt"
        brief.write_text("Build the thing.\n")
        env = {"CW_HUB_CHANNEL_URL": "https://hub.example.invalid"} if hub else {}
        with mock.patch.dict(os.environ, env):
            for key in ("CW_HUB_CHANNEL_URL", "CW_HUB_REGISTRY_SECRET"):
                if key not in env:
                    os.environ.pop(key, None)
            return (command or self.main)("launch", "w-%s-%d" % (role, self.counter()), "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main",
                                          "--role", role, "--brief-file", str(brief), "--cloud")

    def lines(self, err):
        return [l for l in err.splitlines() if "no channel" in l]

    def check_all(self, main=None):
        found = []
        for role in ("coder", "tester", "reviewer"):
            code, out, err = self.launch_role(role, False, main)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(out.strip().splitlines()), 1, out)
            json.loads(out)
            found.append(len(self.lines(err)))
            if self.lines(err):
                self.assertTrue(all(p in self.lines(err)[0] for p in self.LINE), err)
        for role in ("coder", "tester", "reviewer"):
            code, out, err = self.launch_role(role + "x", True, main)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(out.strip().splitlines()), 1, out)
            found.append(len(self.lines(err)) * 10)
        return found

    def test_s8_one_line_with_the_hub_unset_none_with_it_set(self):
        self.assertEqual(self.check_all(), [1, 1, 1, 0, 0, 0])

    def test_s8_no_promise_that_read_reads_a_pod_the_reaper_stopped_and_no_value(self):
        code, out, err = self.launch_role("tester", False)
        text = self.lines(err)[0]
        self.assertNotIn("reaper", text)
        self.assertNotIn("http", text)

    def test_s8_mutant_the_line_with_the_hub_on(self):
        path = ROOT / "bin" / "cloud_worker.py"
        source = path.read_text()
        self.assertEqual(self.check_all(), [1, 1, 1, 0, 0, 0])   # the control: the unmutated command passes
        old = "    if not ((os.environ.get(\"CW_HUB_CHANNEL_URL\")"
        self.assertEqual(source.count(old), 1, "the mutation did not apply")
        mod = types.ModuleType("cloud_worker")
        mod.__file__ = str(path)
        exec(compile(source.replace(old, "    if True or not ((os.environ.get(\"CW_HUB_CHANNEL_URL\")"), str(path), "exec"), mod.__dict__)
        sb = sys.modules["cw_for_tester_no_channel"].sb
        mod._lib = sb.load_module("cloud_worker")._lib   # the library the base already holds with its passing fake preflight
        with mock.patch.dict(sb._MODULES_DONE, {"cloud_worker": mod}), mock.patch.dict(sys.modules, {"cloud_worker": mod}):
            found = self.check_all()
        self.assertNotEqual(found, [1, 1, 1, 0, 0, 0], "the no-channel line with the hub on")
        self.assertEqual(found[3:], [10, 10, 10], "the mutant did not print the line with the hub on")


def mutant_record(old, new):
    path = Path(rr.__file__)
    text = path.read_text()
    assert text.count(old) == 1, "the mutation did not apply"
    mod = types.ModuleType("run_record_mutant")
    mod.__file__ = str(path)
    exec(compile(text.replace(old, new), str(path), "exec"), mod.__dict__)
    return mod


class Phrase(unittest.TestCase):
    """S3, S4, S6: the phrase, computed from fake sessions."""
    COMMAND = "gh api --allow-escape-sequences repos/o/r/actions/jobs/1/logs"

    def check_names_command(self, module):
        text = phrase(refused_session(self.COMMAND), module)
        self.assertIn("refused: gh", text, "the refused command is not named")
        self.assertNotIn("api", text, "a second word of the command was written")
        self.assertNotIn("repos", text, "a second word of the command was written")
        return text

    def test_s3_no_event_and_last_message(self):
        self.assertEqual(phrase([]), "no session event")
        self.assertEqual(phrase([{"type": "assistant", "content": [{"type": "text", "text": "done"}]}]), "last message")

    def test_s4_the_refused_command_is_named_control_then_mutant(self):
        text = self.check_names_command(rr)   # the control: the unmutated module
        self.assertTrue(text.startswith("last tool call refused: "), text)
        mod = mutant_record('text = "last tool call refused: " + word', 'text = "last tool call refused"')
        with self.assertRaises(AssertionError) as caught:
            self.check_names_command(mod)
        self.assertIn("the refused command is not named", str(caught.exception))

    def test_s4_two_words_mutant_and_hostile_commands(self):
        two = mutant_record('text = "last tool call refused: " + word', 'text = "last tool call refused: " + " ".join(str(cmd).split()[:2])')
        self.check_names_command(rr)   # the control first
        with self.assertRaises(AssertionError) as caught:
            self.check_names_command(two)
        self.assertIn("a second word of the command was written", str(caught.exception))
        hostile = phrase(refused_session("pod-name-x1 10.0.0.5:80"))
        self.assertEqual(hostile, "last tool call refused: another command")
        listed = phrase(refused_session("gh run view 12345 --job x"))
        self.assertEqual(listed, "last tool call refused: gh")
        for bad in ("12345", "run", "--job"):
            self.assertNotIn(bad, listed)
        path = phrase(refused_session("/usr/bin/git status"))
        self.assertEqual(path, "last tool call refused: git")   # the base name of a path first word, on the list
        print("S4 lines: %r | %r | %r" % (hostile, listed, path))

    def test_s6_bounded_and_clean(self):
        long_cmd = "gh " + " ".join(PLANTED) + " " + "x" * 500
        text = phrase(refused_session(long_cmd))
        self.assertLessEqual(len(text), 160)
        for planted in PLANTED:
            self.assertNotIn(planted, text)
        long_message = [{"type": "assistant", "content": [{"type": "text", "text": " ".join(PLANTED) + "y" * 900}]}]
        text = phrase(long_message)
        self.assertEqual(text, "last message")

    def test_s6_a_broken_session_gives_a_word_not_an_error(self):
        self.assertEqual(phrase([object()] * 2), "last message")
        self.assertEqual(rr.last_event_phrase(5, lambda t: t), "session unreadable")


class Stop(tcw.Base):
    """S3, S4, S6, U1 on the fake cluster: the reaper's idle stop writes the phrase after what it wrote before."""

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(cw.Launcher, "run_hook", lambda launcher, name: {"pushed": False, "wip_branch": None})
        patch.start()
        self.addCleanup(patch.stop)

    def idle_stop(self, answer):
        self.create()
        self.set_activity("coder7", False, self.clock())
        self.clock.advance(minutes=12)
        with mock.patch.object(rr.Recorder, "read_messages", lambda recorder, pod: answer):
            cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()
        (line,) = [l for l in self.audit_lines() if l["action"] == "stop"]
        return line["reason"]

    def test_s3_the_stop_line_names_no_session_event(self):
        reason = self.idle_stop(([], None))
        self.assertTrue(reason.startswith("idle"), reason)
        self.assertIn("run record ", reason)
        self.assertTrue(reason.endswith("; no session event"), reason)

    def test_s3_last_message(self):
        reason = self.idle_stop(([{"type": "assistant", "content": [{"type": "text", "text": "done"}]}], None))
        self.assertTrue(reason.endswith("; last message"), reason)

    def test_s4_the_stop_line_names_the_refused_command(self):
        reason = self.idle_stop((refused_session(), None))
        self.assertTrue(reason.endswith("; last tool call refused: gh"), reason)
        self.assertLessEqual(len(reason.rsplit("; ", 1)[1]), 160)

    def absent_from_stop_and_json(self, command, absent):
        reason = self.idle_stop((refused_session(command), None))
        texts = [reason] + [f.read_text() for f in Path(self.tmp.name).rglob("run.json")]
        self.assertGreaterEqual(len(texts), 2, "no run.json was read")
        for text in texts:
            for a in absent:
                self.assertNotIn(a, text)

    def test_s4_hostile_name_and_address_absent_from_stop_line_and_run_json(self):
        self.absent_from_stop_and_json("pod-name-x1 10.0.0.5:80", ("pod-name-x1", "10.0.0.5"))

    def test_s4_listed_command_arguments_absent_from_stop_line_and_run_json(self):
        self.absent_from_stop_and_json("gh run view 12345 --job x", ("12345", "--job"))

    def test_s6_an_unreadable_session_adds_those_words_and_the_stop_goes_on(self):
        reason = self.idle_stop((None, "the pod's server could not be reached"))
        self.assertTrue(reason.endswith("; session unreadable"), reason)
        self.assertEqual(self.st()["pods"], {})


class Documents(unittest.TestCase):
    """S5: each of two documents holds the paragraph on cloud testers; a copy without it fails."""
    PHRASES = ("git", "python3", "gh run logs <run id>", "cw-cloud", "final message")
    FILES = ("cloud/README.md", "docs/cloud-worker-interface.md")

    def check(self, name, text):
        text = text.lower().replace("cloud testers:", "cloud testers")
        self.assertIn("cloud testers", text, "%s: no paragraph on cloud testers" % name)
        paragraph = text[text.index("cloud testers"):]
        missing = [p for p in self.PHRASES if p not in paragraph]
        self.assertEqual(missing, [], "%s lacks %s" % (name, missing))

    def test_s5_both_documents_say_it(self):
        for name in self.FILES:
            self.check(name, (ROOT / name).read_text())

    def test_s5_a_copy_without_the_paragraph_fails(self):
        text = (ROOT / self.FILES[0]).read_text()
        self.check(self.FILES[0], text)   # the control
        cut = text[:text.index("## Cloud testers")]
        with self.assertRaises(AssertionError) as caught:
            self.check("copy", cut)
        self.assertIn("cloud testers", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
