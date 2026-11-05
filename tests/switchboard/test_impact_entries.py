"""Ask #2206 part 3: the impact matrix is the JSON file plus one file per entry, read through the ONE loader impact_floor.load_matrix."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from support.paths import BIN


class TestImpactEntriesLoader(unittest.TestCase):
    def setUp(self):
        if str(BIN) not in sys.path:
            sys.path.insert(0, str(BIN))
        import impact_floor
        self.mod = impact_floor

    MOVED = (("owner_hold", "top_tier_paths", "credentials"), ("owner_hold", "top_tier_paths", "identity"), ("owner_hold", "top_tier_paths", "authority"),
             ("owner_hold", "floor", "adamkaplan/fleet-kit", "globs"), ("owner_hold", "top_tier_outside_floor"),
             ("owner_hold", "authority_modules", "exempt"), ("owner_hold", "delegated_clear", "closure_exempt"))

    @staticmethod
    def at(tree, keys):
        for k in keys:
            tree = tree[k]
        return tree

    def test_the_loader_result_equals_todays_parsed_json_on_the_real_tree(self):
        """The fixture is the JSON as it was before the move. Every reader of the moved lists matches a path against ANY glob (a set), so the lists compare
        order-normalised; a dict compares as a dict. Entries added later may extend a moved list, never drop one; all else is equal."""
        before = json.loads((Path(__file__).parent / "fixtures" / "impact_matrix_before_2206.json").read_text())
        after = self.mod.load_matrix()
        for keys in self.MOVED:
            was, now = self.at(before, keys), self.at(after, keys)
            if isinstance(was, dict):
                self.assertEqual({k: v for k, v in now.items() if k in was}, was, keys)
            else:
                self.assertEqual(sorted(set(was) - set(now)), [], "an entry of %s was dropped" % "/".join(keys))
                self.assertEqual(len(now), len(set(now)), "a duplicate in %s" % "/".join(keys))
        for keys in self.MOVED:   # blank the moved lists, the rest must be identical
            for tree in (before, after):
                box = self.at(tree, keys[:-1])
                box[keys[-1]] = {} if isinstance(box[keys[-1]], dict) else []
        self.assertEqual(after, before)

    def test_an_entry_file_is_loaded_into_its_list(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "m.json").write_text("{}")
            before = self.mod.load_matrix(root / "m.json")
            (root / "impact-entries" / "top_tier_outside_floor").mkdir(parents=True)
            (root / "impact-entries" / "top_tier_outside_floor" / "a").write_text("x/**\n")
            after = self.mod.load_matrix(root / "m.json")
        self.assertEqual((before, after), ({}, {"owner_hold": {"top_tier_outside_floor": ["x/**"]}}))


def git(cwd, *args, check=True):
    import subprocess
    run = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and run.returncode:
        raise AssertionError("git %s: %s" % (" ".join(args), run.stderr))
    return run


class TestImpactEntriesMerge(unittest.TestCase):
    """S2 (ask #2206 part 3): two branches that each add a different matrix entry merge with no conflict; today's one-list form conflicts."""

    def conflicts(self, tree):
        import shutil
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            git(root, "init", "-q", "-b", "main")
            for rel, text in tree["base"].items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            git(root, "add", "-A")
            git(root, "commit", "-qm", "base")
            for name in ("a", "b"):
                git(root, "checkout", "-q", "-b", name, "main")
                for rel, text in tree[name].items():
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    (root / rel).write_text(text)
                git(root, "add", "-A")
                git(root, "commit", "-qm", name)
            run = git(root, "merge-tree", "--write-tree", "a", "b", check=False)
        return run.stdout.count("CONFLICT")

    def test_s2_two_branches_adding_different_entry_files_merge_with_no_conflict(self):
        real = Path(__file__).resolve().parents[2] / "docs" / "review-laws" / "impact-entries" / "authority_modules.exempt"
        sample = sorted(real.iterdir())[0]
        base = {"docs/review-laws/impact-entries/authority_modules.exempt/" + sample.name: sample.read_text()}
        tree = {"base": base,
                "a": {"docs/review-laws/impact-entries/authority_modules.exempt/new_a": "bin/new_a.py\nreason a\n"},
                "b": {"docs/review-laws/impact-entries/authority_modules.exempt/new_b": "bin/new_b.py\nreason b\n"}}
        self.assertEqual(self.conflicts(tree), 0)

    def test_s2_today_s_form_of_one_list_conflicts_for_the_same_two_additions(self):
        one = '{"exempt": {\n  "bin/x.py": "why"\n}}\n'
        tree = {"base": {"m.json": one},
                "a": {"m.json": one.replace('"why"\n', '"why",\n  "bin/new_a.py": "a"\n')},
                "b": {"m.json": one.replace('"why"\n', '"why",\n  "bin/new_b.py": "b"\n')}}
        self.assertGreaterEqual(self.conflicts(tree), 1)


class TestImpactEntriesMissing(unittest.TestCase):
    """S4 (ask #2206 part 3): a planted missing entry file fails by name through the authority-modules guard."""

    def planted(self, drop):
        import shutil
        src = Path(__file__).resolve().parents[2] / "docs" / "review-laws"
        t = tempfile.TemporaryDirectory()
        root = Path(t.name) / "docs" / "review-laws"
        shutil.copytree(str(src), str(root))
        for f in (root / "impact-entries").glob("*/" + drop):
            f.unlink()
        return t, root / "impact-matrix.json"

    def unlisted(self, matrix_path):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_cases_03 import TestAuthorityModulesGuard
        guard = TestAuthorityModulesGuard("test_the_real_bin_directory_has_no_unlisted_unexempted_authority_module")
        guard.setUp()
        sys.path.insert(0, str(BIN))
        import impact_floor
        return guard.unlisted(BIN, impact_floor.load_matrix(matrix_path)["owner_hold"])

    # bin/cloud_end_alert.py is named by the guard on the integration tip too (before this change): the control records it, the planted gaps add to it
    BASELINE = ["bin/cloud_end_alert.py"]

    def test_s4_the_control_the_untouched_copy_names_only_the_baseline(self):
        t, path = self.planted("no-such-entry")
        with t:
            self.assertEqual(self.unlisted(path), self.BASELINE)

    def test_s4_a_missing_exemption_entry_file_names_its_module(self):
        t, path = self.planted("bin_brief_advisory.py")
        with t:
            self.assertEqual(self.unlisted(path), ["bin/brief_advisory.py"] + self.BASELINE)

    def test_s4_a_missing_top_tier_entry_file_names_its_module(self):
        t, path = self.planted("bin_merge_gate.py")
        with t:
            self.assertIn("bin/merge_gate.py", self.unlisted(path))
