"""#1844: the record of unconsumed steers and delivered reminders (bin/unconsumed_record.py) on a fixture file."""
import importlib.util
import json
import os
import stat
import tempfile
import unittest

BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin")


def _load_record_module(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec, record_json = _load_record_module("unconsumed_record"), _load_record_module("record_json")


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

    def mutant(self, old, new):
        """The module's source with ONE textual replacement (asserted applied), loaded as a module."""
        import types
        with open(os.path.join(BIN, "unconsumed_record.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertEqual(source.count(old), 1, "the mutation did not apply: %r" % old)
        module = types.ModuleType("unconsumed_record_mutant")
        exec(compile(source.replace(old, new), "<mutant>", "exec"), module.__dict__)
        return module

    def cap_case(self, module):
        ids = ["i%d" % n for n in range(501)]
        module.write(self.dir, {"steered": ids, "reminders": {}}, record_json.parse_bounded)
        data, _ = module.read(self.dir, record_json.parse_bounded)
        self.assertEqual((len(data["steered"]), data["steered"][-1], "i0" in data["steered"]), (module.CAP, "i500", False), "the cap of 500 is not enforced")

    def shape_case(self, module):
        self.put(json.dumps({"steered": [], "reminders": {"reminder:a": ["K", "me"], "reminder:b": ["K", "me", None]}}).encode())
        data, _ = module.read(self.dir, record_json.parse_bounded)
        self.assertEqual(list(data["reminders"]), ["reminder:b"], "an entry of the wrong shape was kept")

    def test_the_cap_is_500_after_501_entries_the_oldest_is_gone(self):
        self.cap_case(rec)

    def test_mutant_cap_5000_fails_with_the_anchored_text(self):
        self.cap_case(self.mutant("CAP = 500\n", "CAP = 500\n"))   # control: the identity replacement passes
        with self.assertRaises(AssertionError) as caught:
            self.cap_case(self.mutant("CAP = 500\n", "CAP = 5000\n"))
        self.assertIn("the cap of 500 is not enforced", str(caught.exception))

    def test_an_entry_of_the_wrong_shape_is_ignored(self):
        self.shape_case(rec)

    def test_mutant_without_the_shape_check_fails_with_the_anchored_text(self):
        self.shape_case(self.mutant("CAP = 500\n", "CAP = 500\n"))
        with self.assertRaises(AssertionError) as caught:
            self.shape_case(self.mutant(" and len(ident) == 3", ""))
        self.assertIn("an entry of the wrong shape was kept", str(caught.exception))

    def test_a_symlink_and_a_directory_at_the_record_path_read_as_unreadable(self):
        target = os.path.join(self.dir, "elsewhere.json")
        with open(target, "wb") as handle:
            handle.write(b'{"steered": ["inb_1"], "reminders": {}}')
        os.symlink(target, self.path)
        self.assertEqual(self.read(), ({"steered": [], "reminders": {}}, "unreadable"))
        os.unlink(self.path)
        os.mkdir(self.path)
        self.assertEqual(self.read(), ({"steered": [], "reminders": {}}, "unreadable"))

    def test_mutant_without_the_regular_file_check_a_symlink_is_followed(self):
        os.symlink(os.path.join(self.dir, "elsewhere.json"), self.path)
        with open(os.path.join(self.dir, "elsewhere.json"), "wb") as handle:
            handle.write(b'{"steered": ["inb_1"], "reminders": {}}')
        mutant = self.mutant("if not stat.S_ISREG(os.lstat(str(Path(state_dir) / FILE)).st_mode):", "if False:")
        self.assertEqual(mutant.read(self.dir, record_json.parse_bounded)[0]["steered"], ["inb_1"])   # the mutation is live
        self.assertEqual(self.read()[0]["steered"], [], "a symlink was followed")

    def sender_case(self, module):
        self.put(json.dumps({"steered": [], "reminders": {"reminder:a": ["K", 7, None], "reminder:b": ["K", "me", None]}}).encode())
        data, _ = module.read(self.dir, record_json.parse_bounded)
        self.assertEqual(list(data["reminders"]), ["reminder:b"], "an entry with a sender that is not a string was kept")

    def test_an_entry_whose_sender_is_not_a_string_is_ignored(self):
        self.sender_case(rec)

    def test_mutant_without_the_sender_string_check_fails_with_the_anchored_text(self):
        self.sender_case(self.mutant("CAP = 500\n", "CAP = 500\n"))
        with self.assertRaises(AssertionError) as caught:
            self.sender_case(self.mutant(" and isinstance(ident[1], str)", ""))
        self.assertIn("a sender that is not a string was kept", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
