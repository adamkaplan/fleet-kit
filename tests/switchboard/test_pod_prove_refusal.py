"""#1875: `prove` and `verdict` refuse first in a cloud pod (the marker CW_WORKER) and name the pod command (scenarios S2, S3, S4)."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from support.core import Base
from support.loader import REPO, SWITCHBOARD, _load, captured, sb

PROVE = ["prove", "S1", "--pr", "acme/widget#1", "--evidence", "x"]
VERDICT = ["verdict", "S1", "--pr", "acme/widget#1", "--as", "tester", "--result", "pass", "--evidence", "x"]
COMMANDS = (("prove", PROVE), ("verdict", VERDICT))
DOCS = (REPO / "docs" / "cloud-operator-quickstart.md", REPO / "skills" / "fleet-coordination" / "SKILL.md")
MARK = 'if os.environ.get("CW_WORKER"):   # #1875'
SENTENCE = "`prove` and `verdict` of a clone refuse in a pod with one line naming that command"
PHRASES = ("`cw-cloud relay prove|verdict`", SENTENCE)


def without_check(source, name):
    """The program text without the check of one command (the text asserted to occur once, the mutation asserted applied)."""
    line = '        raise LedgerCommandError("%s is not run from a cloud pod: record it with `cw-cloud relay %s`")\n' % (name, name)
    assert source.count(line) == 1, "the check of %s does not occur once" % name
    head = source.index(line)
    start = source.rindex("\n", 0, head - 1) + 1   # the condition line just before the raise, whatever its text
    assert source[start:head].lstrip().startswith("if ") and "#1875" in source[start:head], "the line before the check is not its condition"
    mutated = source[:start] + source[head + len(line):]
    assert mutated != source and len(mutated) < len(source), "the mutation was not applied"
    return mutated


def missing_pod_command(text):
    for phrase in PHRASES:
        if phrase not in text:
            raise AssertionError("the document does not name the pod command")


class Runs(Base):
    def setUp(self):
        super().setUp()
        self.copies = []

    def copy_without(self, name=None):
        """A scratch copy of the entry (every part), without the check of `name` (None: the unmutated control)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        shutil.copytree(str(SWITCHBOARD.parent), str(Path(tmp.name) / "bin"), ignore=shutil.ignore_patterns("__pycache__"))
        if name:
            entry = Path(tmp.name) / "bin" / "fleet-switchboard"
            entry.write_text(without_check(entry.read_text(), name))
        self.copies.append(1)
        return _load("t_pod_copy_%d" % len(self.copies), Path(tmp.name) / "bin" / "fleet-switchboard")

    def run_cmd(self, module, argv, pod, config):
        calls = []

        def runner(*a, **k):
            calls.append(a)
            raise AssertionError("a runner call: %r" % (a[:1],))
        if config is None:
            self.config_file.unlink() if self.config_file.exists() else None
        else:
            self.write_config(config)
        env = {"CW_WORKER": "pod-1"} if pod else {}
        with mock.patch.dict(os.environ, env):
            if not pod:
                os.environ.pop("CW_WORKER", None)
            with captured() as (out, err):
                try:
                    code = module.main(list(argv), runner=runner, now=lambda: 1000.0, wait=lambda s, t: False)
                except SystemExit as stop:
                    code = stop.code
        written = [p for p in self.state_dir.rglob("*") if p.is_file()] if self.state_dir.exists() else []
        return code, out.getvalue(), err.getvalue(), len(calls), len(written)


class TestPodRefusal(Runs):
    CASES = (None, {})

    def check_refusal(self, module, name, argv, config):
        code, out, err, calls, writes = self.run_cmd(module, argv, True, config)
        if "not configured" in out + err:
            raise AssertionError("a pod command reached the configuration")
        return code, out, err, calls, writes

    def test_s2_refuses_first_with_the_pod_command(self):
        control = self.copy_without()
        self.assertEqual(self.check_refusal(control, "prove", PROVE, None)[0], 2)   # the control first
        for name, argv in COMMANDS:
            for config in self.CASES:
                code, out, err, calls, writes = self.check_refusal(sb, name, argv, config)
                lines = (out + err).strip().splitlines()
                print("S2 %s config=%r: exit %s, lines %d, runner calls %d, ledger writes %d: %s" % (name, config, code, len(lines), calls, writes, lines[:1]))
                self.assertEqual((code, len(lines), calls, writes), (2, 1, 0, 0))
                self.assertIn("`cw-cloud relay %s`" % name, lines[0])
                self.assertNotIn(str(self.home), lines[0])
                self.assertNotIn("not configured", lines[0])

    def test_s2_the_copy_without_the_check_fails(self):
        for name, argv in COMMANDS:
            mutant = self.copy_without(name)
            with self.assertRaises(AssertionError) as raised:
                self.check_refusal(mutant, name, argv, None)
            print("S2 mutant %s: %s" % (name, raised.exception))
            self.assertEqual(str(raised.exception), "a pod command reached the configuration")


    def test_s2_each_command_has_its_own_control_then_its_mutant(self):
        controls = mutants = 0
        for name, argv in COMMANDS:
            code, out, err, calls, writes = self.check_refusal(self.copy_without(), name, argv, None)   # its own control, first
            lines = (out + err).strip().splitlines()
            self.assertEqual((code, len(lines)), (2, 1))
            self.assertIn("`cw-cloud relay %s`" % name, lines[0])
            controls += 1
            with self.assertRaises(AssertionError) as raised:
                self.check_refusal(self.copy_without(name), name, argv, None)
            self.assertEqual(str(raised.exception), "a pod command reached the configuration")
            mutants += 1
        print("S2 control %d of %d, mutants %d of %d with the text" % (controls, len(COMMANDS), mutants, len(COMMANDS)))
        self.assertEqual((controls, mutants), (2, 2))


class TestOutsideAPod(Runs):
    def test_s3_without_the_marker_the_tip_prints_what_it_printed(self):
        for name, argv in COMMANDS:
            tip = self.copy_without(name)   # the program without the check: what the tip does
            for config in (None, {}):
                mine = self.run_cmd(sb, argv, False, config)
                theirs = self.run_cmd(tip, argv, False, config)
                print("S3 %s config=%r: exit %s, %r" % (name, config, mine[0], mine[2].strip()[:60]))
                for run in (mine, theirs):   # the control itself holds the outside-a-pod text
                    self.assertIn("'opencode' is not configured", run[1] + run[2])
                    self.assertNotIn("cw-cloud relay", run[1] + run[2])
                self.assertEqual(mine[:3], theirs[:3])

    def test_s3_the_empty_marker_behaves_as_unset(self):
        cases = 0
        for name, argv in COMMANDS:
            for config in (None, {}):
                unset = self.run_cmd(sb, argv, False, config)
                with mock.patch.dict(os.environ, {"CW_WORKER": ""}):
                    empty = self.run_cmd_keeping_env(argv, config)
                self.assertEqual(empty[:3], unset[:3])
                self.assertIn("'opencode' is not configured", empty[1] + empty[2])
                cases += 1
        print("S3 empty marker: %d of 4 cases the same as unset" % cases)
        self.assertEqual(cases, 4)

    def run_cmd_keeping_env(self, argv, config):
        """run_cmd's run with the environment as the caller set it (the marker present and empty)."""
        calls = []
        if config is None:
            self.config_file.unlink() if self.config_file.exists() else None
        else:
            self.write_config(config)
        with captured() as (out, err):
            try:
                code = sb.main(list(argv), runner=lambda *a, **k: calls.append(a), now=lambda: 1000.0, wait=lambda s, t: False)
            except SystemExit as stop:
                code = stop.code
        self.assertEqual(os.environ.get("CW_WORKER"), "")
        return code, out.getvalue(), err.getvalue(), len(calls)


class TestPodDocuments(Base):
    def found(self, texts):
        return sum(1 for text in texts for phrase in PHRASES if phrase in text)

    def test_s4_the_documents_name_the_pod_command(self):
        texts = [d.read_text() for d in DOCS]
        for text in texts:   # the control first
            missing_pod_command(text)
        self.assertEqual(self.found(texts), 2 * len(PHRASES))
        print("S4 control: %d of %d sentences found in %d documents" % (self.found(texts), 2 * len(PHRASES), len(texts)))
        for text in texts:
            mutant = "\n".join(l for l in text.splitlines() if "cw-cloud relay prove|verdict" not in l)
            self.assertNotEqual(mutant, text)
            with self.assertRaises(AssertionError) as raised:
                missing_pod_command(mutant)
            self.assertEqual(str(raised.exception), "the document does not name the pod command")
        print("S4 mutant: fails with the anchored text")

if __name__ == "__main__":
    unittest.main()
