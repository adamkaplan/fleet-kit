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

    def test_the_loader_result_equals_the_parsed_json_on_the_real_tree(self):
        parsed = json.loads(self.mod.MATRIX_PATH.read_text())
        self.assertEqual(self.mod.load_matrix(), parsed)

    def test_an_entry_file_is_loaded_into_its_list(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root / "m.json").write_text("{}")
            before = self.mod.load_matrix(root / "m.json")
            (root / "impact-entries" / "top_tier_outside_floor").mkdir(parents=True)
            (root / "impact-entries" / "top_tier_outside_floor" / "a").write_text("x/**\n")
            after = self.mod.load_matrix(root / "m.json")
        self.assertEqual((before, after), ({}, {"owner_hold": {"top_tier_outside_floor": ["x/**"]}}))
