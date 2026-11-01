"""#1844: the record of unconsumed steers and delivered reminders (bin/unconsumed_record.py) on a fixture file."""
import importlib.util
import json
import os
import stat
import tempfile
import unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec, record_json = _load("unconsumed_record"), _load("record_json")


class TestUnconsumedRecord(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, rec.FILE)

    def put(self, data):
        with open(self.path, "wb") as handle:
            handle.write(data)

    def read(self):
        return rec.read(self.dir, record_json.parse_bounded)

    def test_missing_reads_as_empty_with_a_reason(self):
        self.assertEqual(self.read(), ({"steered": [], "reminders": {}}, "missing"))

    def test_corrupt_oversized_and_wrong_type_read_as_empty(self):
        for data, why in ((b"{not json", "corrupt"), (b"[1, 2]", "wrong type"), (b" " * ((1 << 20) + 5), "oversized")):
            self.put(data)
            self.assertEqual(self.read(), ({"steered": [], "reminders": {}}, why))

    def test_unreadable_reads_as_empty(self):
        os.mkdir(self.path)
        self.assertEqual(self.read(), ({"steered": [], "reminders": {}}, "unreadable"))

    def test_a_written_record_reads_back_with_mode_0600(self):
        data = {"steered": ["inb_1"], "reminders": {"reminder:a": ["K", "me", "platform"]}}
        self.assertTrue(rec.write(self.dir, data, record_json.parse_bounded))
        self.assertEqual(self.read(), (data, None))
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_unchanged_content_writes_nothing(self):
        data = {"steered": ["inb_1"], "reminders": {}}
        rec.write(self.dir, data, record_json.parse_bounded)
        before = os.stat(self.path).st_mtime_ns
        self.assertFalse(rec.write(self.dir, data, record_json.parse_bounded))
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)
        self.assertEqual(os.listdir(self.dir), [rec.FILE])

    def test_each_map_is_bounded_oldest_dropped(self):
        ids = ["i%d" % n for n in range(rec.CAP + 10)]
        rec.write(self.dir, {"steered": ids, "reminders": {"reminder:%d" % n: ["K", "f", None] for n in range(rec.CAP + 10)}},
                  record_json.parse_bounded)
        data, _ = self.read()
        self.assertEqual((data["steered"][0], len(data["steered"]), len(data["reminders"])), ("i10", rec.CAP, rec.CAP))
        self.assertIn("reminder:%d" % (rec.CAP + 9), data["reminders"])


if __name__ == "__main__":
    unittest.main()
