"""#2108: the shards of bin/test-switchboard --shard I/N (tools/test-shards): every class in exactly one shard, balanced by the committed table, and the shards together run the ids of a full run."""
import importlib.machinery
import importlib.util
import types
import unittest

from support.paths import REPO


def _load_shard_tool():
    path = str(REPO / "tools" / "test-shards")
    loader = importlib.machinery.SourceFileLoader("test_shards_tool", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _fixture_module():
    mod = types.ModuleType("fixture_shard_module")
    for name in ("Alpha", "Beta", "Gamma", "Delta", "Epsilon", "Zeta", "Eta", "Theta", "Iota", "Kappa"):
        def make(n):
            body = {"test_%s_%d" % (n.lower(), k): (lambda self: None) for k in range(3)}
            return type(n, (unittest.TestCase,), body)
        cls = make(name)
        cls.__module__ = mod.__name__
        setattr(mod, name, cls)
    return mod


class TestShards(unittest.TestCase):
    N = 6
    BOUND = 0.10   # the heaviest shard may exceed the mean shard by at most 10% of the mean (and the longest class is the floor of what greedy can do)

    def setUp(self):
        self.tool = _load_shard_tool()
        self.times = self.tool.load_table()

    def test_every_class_of_the_table_is_in_exactly_one_shard(self):
        shards = self.tool.partition(list(self.times), self.times, self.N)
        self.assertEqual(self.tool.check(shards, list(self.times)), [])
        self.assertEqual(sum(len(s) for s in shards), len(self.times))

    def test_a_planted_table_with_a_missing_and_a_doubled_class_fails_naming_both(self):
        classes = ["A", "B", "C", "D"]
        shards = [["A", "B"], ["B", "C"]]   # B doubled, D missing
        problems = self.tool.check(shards, classes)
        self.assertGreaterEqual(len(problems), 2, problems)
        self.assertTrue(any("B" in p and "2 shards" in p for p in problems), problems)
        self.assertTrue(any("D" in p and "no shard" in p for p in problems), problems)

    def test_a_class_missing_from_the_table_goes_by_a_stable_hash_and_still_lands_once(self):
        classes = list(self.times) + ["TestBrandNewClass", "TestAnotherNewClass"]
        first = self.tool.partition(classes, self.times, self.N)
        second = self.tool.partition(list(reversed(classes)), self.times, self.N)
        self.assertEqual(self.tool.check(first, classes), [])
        where = lambda shards, c: [k for k, s in enumerate(shards) if c in s]
        for c in ("TestBrandNewClass", "TestAnotherNewClass"):
            self.assertEqual(where(first, c), where(second, c))

    def test_the_shards_table_times_are_within_the_stated_bound(self):
        shards = self.tool.partition(list(self.times), self.times, self.N)
        sums = [sum(self.times[c] for c in s) for s in shards]
        mean = sum(self.times.values()) / self.N
        self.assertLessEqual(max(sums), max(mean * (1 + self.BOUND), max(self.times.values())), sums)
        self.assertLessEqual(max(sums) - min(sums), max(mean * self.BOUND, max(self.times.values())), sums)

    def test_the_union_of_the_shard_runs_ids_equals_a_full_run_ids_on_a_fixture(self):
        mod = _fixture_module()
        full = unittest.TestLoader().loadTestsFromModule(mod)
        full_ids = sorted(c.id() for c in self.tool.cases_of(full))
        self.assertGreater(len(full_ids), 0)   # the fixture has tests; the union below equals them all
        seen = []
        for i in range(1, self.N + 1):
            suite = self.tool.select(unittest.TestLoader().loadTestsFromModule(mod), i, self.N, times={})
            seen += [c.id() for c in self.tool.cases_of(suite)]   # before the run: a run empties the suite
            result = unittest.TestResult()
            suite.run(result)
            self.assertTrue(result.wasSuccessful())
        self.assertEqual(sorted(seen), full_ids)

    def test_the_refresh_reads_class_seconds_from_a_log(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            table = Path(d) / "t.json"
            table.write_text(json.dumps({"A": 1.0, "B": 2.0}))
            log = Path(d) / "log"
            log.write_text("noise\nCLASS B 4.26\nCLASS C 0.5\n")
            times = self.tool.refresh(log, table)
            self.assertEqual(times, {"A": 1.0, "B": 4.3, "C": 0.5})
            self.assertEqual(json.loads(table.read_text()), times)


if __name__ == "__main__":
    unittest.main()
