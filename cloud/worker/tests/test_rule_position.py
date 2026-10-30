#!/usr/bin/env python3
"""#1562: in both blocks of cloud/worker/opencode-worker.json every allow row comes before every other row (the matcher takes the LAST
matching rule). The file equals the output of the one generator (test_push_rules.write_conf). Mutants are scratch copies outside the repository."""
import copy, json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import stub_support  # noqa: E402
import test_push_rules as P  # noqa: E402

ROOT = P.ROOT
DOC = os.path.join(ROOT, "docs", "cloud-worker-interface.md")


def order_failures(conf):
    """Empty when both blocks equal the generated order and no later row precedes an allow row."""
    bad = []
    gen = P.write_conf(copy.deepcopy(conf))
    for i, (b, g) in enumerate(zip(P.blocks(conf), P.blocks(gen))):
        if list(b.items()) != list(g.items()):
            bad.append((i, "differs from the generated order"))
        v = list(b.values())
        if (v.count("allow"), v.count("deny")) != (11, 2051):
            bad.append((i, "differs: a rule was added or dropped"))
        last = max(k for k, x in enumerate(v) if x == "allow")
        if any(x != "allow" for x in v[:last]):
            bad.append((i, "a later row before an allow row"))
    return bad


class Position(unittest.TestCase):
    def setUp(self):
        self.conf = P.load()

    def test_both_blocks_equal_the_generated_order(self):
        self.assertEqual(order_failures(self.conf), [])
        top, inner = P.blocks(self.conf)
        self.assertEqual(list(top.items()), list(inner.items()))
        self.assertEqual(list(top.items())[0], ("*", "allow"))
        for b in (top, inner):
            self.assertEqual(set(b.values()), {"allow", "deny"})

    def test_one_generator_and_no_second_copy(self):
        defs = []
        for n in sorted(os.listdir(HERE)):
            if n.endswith(".py"):
                for m in re.finditer(r"^def (write_conf|order_rows)\(", open(os.path.join(HERE, n)).read(), re.M):
                    defs.append((n, m.group(1)))
        self.assertEqual(sorted(defs), [("test_push_rules.py", "order_rows"), ("test_push_rules.py", "write_conf")])

    def test_push_rules_are_still_the_generated_set_last(self):
        for b in P.blocks(self.conf):
            n = len(P.push_patterns(b))
            self.assertEqual(n, 1922)
            self.assertEqual(list(b)[-n:], list(P.generate_all()))

    def test_documentation_phrases(self):
        doc = open(DOC).read()
        for s in ("every deny rule comes after every allow rule in both blocks", "the last matching rule",
                  "the git wrapper and the branch protection are the other layers"):
            self.assertIn(s, doc)

    def run_mutant(self, name, mutate):
        conf = copy.deepcopy(self.conf)
        self.assertTrue(mutate(conf), "mutation applied")
        self.assertTrue([list(b.items()) for b in P.blocks(conf)] != [list(b.items()) for b in P.blocks(self.conf)], "mutation applied")
        d = tempfile.mkdtemp(prefix="rulepos-", dir="/tmp")
        path = os.path.join(d, name)
        json.dump(conf, open(path, "w"), indent=2)
        p = stub_support.start(self, [sys.executable, os.path.abspath(__file__), "--check", path],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = p.communicate(timeout=60)
        self.assertEqual(p.returncode, 1, out)
        return out

    def test_mutant_later_row_moved_before_an_allow(self):
        self.run_mutant("moved.json", self._move_later)

    def _move_later(self, conf):
        for b in P.blocks(conf):
            items = list(b.items())
            k = max(i for i, (_, v) in enumerate(items) if v == "allow")
            dk = next(i for i, (_, v) in enumerate(items) if v == "deny")
            items.insert(k, items.pop(dk))
            b.clear(); b.update(items)
        return True

    def test_mutant_allow_moved_after_a_later_row(self):
        def m(conf):
            for b in P.blocks(conf):
                items = list(b.items())
                items.append(items.pop(1))   # first non-catch-all allow row goes last
                b.clear(); b.update(items)
            return True
        self.assertIn("allow", [v for _, v in list(P.blocks(self.conf)[0].items())[1:2]])
        self.run_mutant("allowlast.json", m)

    def test_mutant_rule_dropped(self):
        def m(conf):
            for b in P.blocks(conf):
                del b[next(k for k, v in b.items() if v == "deny")]
            return True
        out = self.run_mutant("dropped.json", m)
        self.assertIn("differs", out)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--check":
        bad = order_failures(json.load(open(sys.argv[2])))
        print(bad)
        sys.exit(1 if bad else 0)
    unittest.main()
