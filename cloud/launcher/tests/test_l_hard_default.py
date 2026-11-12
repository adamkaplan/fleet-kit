"""Follow-up of ask #2457: a `--shape L` launch with no --hard gets DEFAULT_MAX_HARD (8h), not 2h; each case with a mutant."""
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_cloud_worker as lbase  # noqa: E402
import test_idle_default_note as note  # noqa: E402

cw = lbase.cw
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PATH = os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py")


class Create(lbase.Base):
    def create(self, *extra):
        code, out, err = self.run_cli("create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", *extra)
        return code, out, err

    def hard_seconds(self, *extra):
        code, out, err = self.create(*extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)["hard_seconds"]

    def test_l_without_hard_gets_the_maximum(self):
        self.assertEqual(self.hard_seconds("--shape", "L"), cw.parse_duration(cw.DEFAULT_MAX_HARD))
        self.assertEqual(cw.DEFAULT_MAX_HARD, "8h")

    def test_l_with_hard_keeps_it(self):
        self.assertEqual(self.hard_seconds("--shape", "L", "--hard", "3h"), 3 * 3600)

    def test_m_without_hard_gets_two_hours(self):
        self.assertEqual(self.hard_seconds("--shape", "M"), 7200)

    def test_no_shape_without_hard_gets_two_hours(self):
        self.assertEqual(self.hard_seconds(), 7200)

    def test_l_with_hard_above_the_maximum_still_refuses(self):
        code, _, err = self.create("--shape", "L", "--hard", "20h")
        self.assertEqual(code, cw.EXIT_REFUSED, err)
        self.assertIn("--hard is past --max-hard", err)

    def test_a_mutant_that_ignores_the_l_default_is_caught(self):
        good = 'SHAPE_DEFAULT_HARD = {"L": DEFAULT_MAX_HARD}'
        source = open(PATH).read()
        self.assertEqual(source.count(good), 1)
        mutant = types.ModuleType("mutant_l_hard")
        mutant.__file__ = PATH
        exec(compile(source.replace(good, "SHAPE_DEFAULT_HARD = {}"), PATH, "exec"), mutant.__dict__)
        out = io.StringIO()
        with redirect_stdout(out):
            code = mutant.main(["create", "coder7", "--repo", "acme-org/widget-service", "--branch", "main", "--shape", "L"], self.env, self.clock,
                               lambda s: self.clock.advance(seconds=s))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["hard_seconds"], 7200)   # the control: the mutant is the 2h build
        self.assertNotEqual(7200, cw.parse_duration(cw.DEFAULT_MAX_HARD))


class Note(unittest.TestCase):
    REC = {"idle_deadline": "2099-10-14T14:00:00Z", "hard_deadline": "2099-10-14T20:00:00Z", "idle_seconds": 3600, "hard_seconds": 28800}

    def text(self, **kw):
        module = note.sb_module(self)
        return module._limits_note(types.SimpleNamespace(**dict(dict(idle=None, hard=None, shape=None), **kw)), self.REC)

    def test_l_without_hard_says_the_shape_default(self):
        self.assertEqual(self.text(shape="L"), "launch --cloud: idle limit 1h (default), hard limit 8h (default for shape L), stops at 2099-10-14T14:00:00Z")

    def test_l_with_hard_and_other_shapes_say_what_they_did(self):
        self.assertIn("hard limit 3h, stops", self.text(shape="L", hard="3h"))
        self.assertIn("hard limit 8h (default), stops", self.text(shape="M"))


if __name__ == "__main__":
    unittest.main()
