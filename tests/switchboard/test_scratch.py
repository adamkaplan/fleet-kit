"""The durable-script tests of #1201 (the janitor never reaches a durable file), moved out of bin/test-switchboard (its line ceiling).
build(TestTempJanitor) returns a subclass that adds only these tests: the inherited ones are switched off so they do not run twice."""
import os
import tempfile
from unittest import mock


def build(TestTempJanitor):
    class TestTempJanitorScratch(TestTempJanitor):
        def durable_fixture(self):
            """A shared temp dir (the system one, patched) with a durable fs.sh at its top, its `opencode` folder with one old scratch file, and a named scratch dir under the state area."""
            systmp = self.home / "systmp"
            (systmp / "opencode").mkdir(parents=True)
            scratch = self.home / ".local" / "state" / "fleet-switchboard" / "scratch" / "acme-orch"
            scratch.mkdir(parents=True)
            durable = [systmp / "fs.sh", scratch / "helper.sh"]
            for path in durable + [systmp / "opencode" / "old-scratch.log"]:
                path.write_text("x")
                stamp = self.now - 10 * 86400
                os.utime(str(path), (stamp, stamp))
            return systmp, durable

        def apply_default_janitor(self, systmp):
            """The real code path: the janitor's default directory, then `run` with apply on. Returns the lines."""
            with mock.patch.object(tempfile, "gettempdir", lambda: str(systmp)):
                lines, removed, kept = self.jan.run(self.jan.default_dir(), self.world.state, self.now, self.jan.settings({}), True, lambda *a: None, busy=lambda p: None)
            return lines

        def test_s4_the_janitor_never_reaches_a_durable_file(self):
            systmp, durable = self.durable_fixture()
            lines = self.apply_default_janitor(systmp)
            self.assertTrue(any("removed old-scratch.log" in line for line in lines), lines)   # it did run, and did clean its own folder
            for path in durable:
                self.assertTrue(path.exists(), "%s was removed" % path)

        def test_s4_mutant_a_janitor_scoped_to_the_whole_shared_temp_dir_fails_the_durable_test(self):
            systmp, durable = self.durable_fixture()
            with mock.patch.object(self.jan, "default_dir", lambda: systmp), mock.patch.object(self.jan, "safe_dir", lambda p: (True, "")):
                self.apply_default_janitor(systmp)
            self.assertFalse((systmp / "fs.sh").exists(), "the widened scope must reach fs.sh, or the test above pins nothing")
            with self.assertRaises(AssertionError):
                for path in durable:
                    self.assertTrue(path.exists(), "%s was removed" % path)

    for _name in dir(TestTempJanitor):
        if _name.startswith("test_"):
            setattr(TestTempJanitorScratch, _name, None)
    return TestTempJanitorScratch
