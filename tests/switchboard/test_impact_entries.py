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
