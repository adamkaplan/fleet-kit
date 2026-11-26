"""The .spire/box.json pre-check format: valid files load, invalid ones are rejected, the eslint step is guarded."""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("box_precheck", REPO / "tools" / "box_precheck.py")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)

def runs(step, cwd):
    """The box tool's rule (docs/BOX.md): a step runs when its `when` test exits 0."""
    return subprocess.run(["bash", "-c", step["when"]], cwd=cwd).returncode == 0


GOOD = {"box": 1, "rewriteFlags": ["--fix"], "setup": [{"name": "eslint-fix", "when": "true", "run": "npx eslint --fix a.js"}]}


class TestBoxPrecheck(unittest.TestCase):
    def test_valid_accepted(self):
        self.assertEqual(bp.problems(GOOD), [])
        self.assertEqual(bp.problems({"box": 1}), [])

    def test_repository_file_valid(self):
        self.assertEqual(bp.load(REPO / ".spire" / "box.json")["box"], 1)

    def test_invalid_rejected(self):
        bad = [
            [],
            {"box": 2},
            {"box": 1, "setup": {}},
            {"box": 1, "setup": ["x"]},
            {"box": 1, "setup": [{"name": "", "run": "true"}]},
            {"box": 1, "rewriteFlags": "--fix"},
            {"box": 1, "setup": [{"name": "a"}]},
            {"box": 1, "setup": [{"name": "a", "run": "true", "refresh": "yes"}]},
            {"box": 1, "setup": [{"name": "a", "run": "true", "when": ""}]},
            {"box": 1, "setup": [{"name": "a", "run": "true"}, {"name": "a", "run": "true"}]},
        ]
        for doc in bad:
            self.assertTrue(bp.problems(doc), doc)

    def test_fixer_without_guard_rejected(self):
        doc = {"box": 1, "setup": [{"name": "eslint-fix", "run": "npx eslint --fix ."}]}
        self.assertEqual(bp.problems(doc), [])  # no rewriteFlags declared: nothing to guard
        doc["rewriteFlags"] = ["--fix", "--write"]
        self.assertTrue(any("when" in p for p in bp.problems(doc)))
        doc["setup"][0]["run"] = "npx prettier --write ."
        self.assertTrue(any("when" in p for p in bp.problems(doc)))
        doc["setup"][0]["when"] = "true"
        self.assertEqual(bp.problems(doc), [])

    def test_load_rejects_bad_json(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "box.json"
            p.write_text("{nope")
            with self.assertRaises(bp.Invalid):
                bp.load(p)
            p.write_text(json.dumps({"box": 3}))
            with self.assertRaises(bp.Invalid):
                bp.load(p)

    def test_eslint_step_skips_without_changed_files_and_runs_with(self):
        step = bp.load(REPO / ".spire" / "box.json")["setup"][0]
        git = lambda d, *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)
        with tempfile.TemporaryDirectory() as d:
            git(d, "init", "-q")
            git(d, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "i")
            (Path(d) / "a.js").write_text("1\n")
            (Path(d) / "n.txt").write_text("1\n")
            git(d, "add", ".")
            git(d, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "c")
            self.assertFalse(runs(step, d), "nothing changed: skip")
            (Path(d) / "n.txt").write_text("2\n")
            self.assertFalse(runs(step, d), "only a non-JS file changed: skip")
            (Path(d) / "a.js").write_text("2\n")
            self.assertTrue(runs(step, d), "a JS file changed: run")
            git(d, "checkout", "-q", "--", ".")
            (Path(d) / "new.ts").write_text("1\n")
            self.assertTrue(runs(step, d), "an untracked TS file: run")


if __name__ == "__main__":
    unittest.main()
