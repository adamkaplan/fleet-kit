"""Ask #1928: one table (cloud/worker/copies.tsv) and one command (tools/make-worker-copies) for the worker copies. Offline; scratch trees."""
import ast
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
TABLE = "cloud/worker/copies.tsv"
COMMAND = "tools/make-worker-copies"
COMPARING = ("cloud/launcher/tests/test_orchestrator_agent.py", "bin/test-cloud-hub",
             "tests/switchboard/test_relay_loop_cadence.py", "cloud/launcher/tests/test_cw_names.py")
ROWNAME = re.compile(r"(cloud/worker/|bin/|agents/opencode/)?(cloud_\w+\.py|orchestrator\.md)")


def slurp(rel, root=ROOT):
    with open(os.path.join(root, rel), "rb") as f:
        return f.read()


def rows(root=ROOT):
    return [tuple(l.split("\t")) for l in slurp(TABLE, root).decode().splitlines() if l]


def load(path):
    loader = importlib.machinery.SourceFileLoader("mwc_%d" % abs(hash(path)), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def scratch(test):
    d = tempfile.mkdtemp(prefix="wc-scratch-")
    test.addCleanup(shutil.rmtree, d, True)
    for rel in [TABLE, COMMAND] + [c for pair in rows() for c in pair]:
        os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
        shutil.copyfile(os.path.join(ROOT, rel), os.path.join(d, rel))
    return d


def run_in(d, command=COMMAND):
    out = io.StringIO()
    status = load(os.path.join(d, command)).run(d, out)
    return status, out.getvalue()


def snapshot(d):
    snap = {}
    for base, _, names in os.walk(d):
        for n in names:
            p = os.path.join(base, n)
            with open(p, "rb") as f:
                snap[os.path.relpath(p, d)] = f.read()
    return snap


def check_all_made(d):
    """The S2 check: every row equal after the command."""
    for source, copy in rows(d):
        if slurp(source, d) != slurp(copy, d):
            raise AssertionError("a copy was not made from its source: %s" % copy)


def second_lists(text):
    found = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            elts = node.keys + node.values if isinstance(node, ast.Dict) else node.elts
            names = [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str) and ROWNAME.fullmatch(e.value)]
            if len(names) >= 2:
                found.append(names)
    return found


class Drift(unittest.TestCase):
    def test_s2_drift_test_names_the_pair_then_the_command_makes_it_equal(self):
        from test_orchestrator_agent import drift
        d = scratch(self)
        self.assertEqual(run_in(d), (0, "0 changed\n"))
        with open(os.path.join(d, "bin/cloud_channel_server.py"), "ab") as f:
            f.write(b"# changed\n")
        self.assertEqual(drift(d, "bin/cloud_channel_server.py", "cloud/worker/bin/cloud_channel_server.py"),
                         "cloud/worker/bin/cloud_channel_server.py differs from bin/cloud_channel_server.py")
        status, out = run_in(d)
        self.assertEqual(status, 0)
        self.assertEqual(out.splitlines(), ["copied bin/cloud_channel_server.py to cloud/worker/bin/cloud_channel_server.py", "1 changed"])
        check_all_made(d)
        self.assertEqual(sum(1 for s, c in rows(d) if slurp(s, d) == slurp(c, d)), len(rows(d)))
        self.assertIsNone(drift(d, "bin/cloud_channel_server.py", "cloud/worker/bin/cloud_channel_server.py"))
        self.assertEqual(run_in(d), (0, "0 changed\n"))

    def test_s2_the_table_has_the_nine_rows_and_the_golden(self):
        self.assertEqual(len(rows()), 9)
        self.assertIn(("agents/opencode/orchestrator.md", "cloud/worker/tests/golden/orchestrator.md"), rows())


class Missing(unittest.TestCase):
    def test_s3_missing_source_names_both_and_changes_nothing(self):
        d = scratch(self)
        with open(os.path.join(d, "bin/cloud_cli.py"), "ab") as f:
            f.write(b"# changed\n")
        os.remove(os.path.join(d, "bin/cloud_relay.py"))
        before = snapshot(d)
        status, out = run_in(d)
        self.assertNotEqual(status, 0)
        self.assertEqual(out.splitlines(), ["missing source bin/cloud_relay.py for the copy cloud/worker/bin/cloud_relay.py"])
        self.assertEqual(snapshot(d), before)

    def test_s3_mutant_skipping_the_last_row_fails_the_check(self):
        d = scratch(self)
        with open(os.path.join(d, "bin/cloud_cli.py"), "ab") as f:
            f.write(b"# changed\n")
        with open(os.path.join(d, "bin/plan_check.py"), "ab") as f:
            f.write(b"# changed\n")
        control = scratch(self)
        with open(os.path.join(control, "bin/plan_check.py"), "ab") as f:
            f.write(b"# changed\n")
        self.assertEqual(run_in(control)[0], 0)
        check_all_made(control)   # the control passes first
        original = slurp(COMMAND, d).decode()
        old = "    for source, copy in rows:\n        data ="
        self.assertEqual(original.count(old), 1)
        mutated = original.replace(old, "    for source, copy in rows[:-1]:\n        data =")
        self.assertNotEqual(mutated, original)
        self.assertIn(old, original)
        self.assertNotIn(old, mutated)
        with open(os.path.join(d, COMMAND), "w") as f:
            f.write(mutated)
        self.assertEqual(run_in(d)[0], 0)
        with self.assertRaisesRegex(AssertionError, "a copy was not made from its source"):
            check_all_made(d)

    def test_s3_a_no_op_mutation_fails_the_computation(self):
        original = slurp(COMMAND).decode()
        mutated = original.replace("not-in-the-text", "x")
        self.assertEqual(mutated, original)   # nothing applied: the computation below must refuse it
        with self.assertRaises(AssertionError):
            self.assertNotEqual(mutated, original)


class OneTable(unittest.TestCase):
    def test_s4_one_definition_and_no_second_list(self):
        self.assertEqual(sum(1 for _, _, ns in os.walk(os.path.join(ROOT, "cloud/worker")) for n in ns if n == "copies.tsv"), 1)
        for rel in COMPARING:
            with self.subTest(test=rel):
                text = slurp(rel).decode()
                self.assertIn("copies.tsv", text)
                self.assertEqual(second_lists(text), [], "a second list of copies in " + rel)

    def test_s4_a_second_list_is_caught(self):
        text = slurp("bin/test-cloud-hub").decode()
        anchor = "\nAGE_EXPR = "
        self.assertEqual(text.count(anchor), 1)
        mutated = text.replace(anchor, "\nSECOND = ('cloud_hub.py', 'cloud_relay.py')" + anchor)
        self.assertNotEqual(mutated, text)
        self.assertIn("SECOND = ", mutated)
        self.assertNotIn("SECOND = ", text)
        self.assertEqual(second_lists(text), [])
        found = second_lists(mutated)
        self.assertEqual(len(found), 1, "a second list of copies")


BAD_ROWS = (
    ("a", "bin/cloud_cli.py\tREADME.md"),
    ("b1", "../x\tcloud/worker/x"),
    ("b2", "/etc/hostname\tcloud/worker/x"),
    ("c1", " bin/cloud_cli.py\tcloud/worker/x"),
    ("c2", "bin/cloud_cli.py\tcloud/worker/x "),
    ("d1", "bin/cloud_cli.py cloud/worker/x"),
    ("d2", "bin/cloud_cli.py\tcloud/worker/x\tmore"),
    ("e", "bin/cloud_cli.py\tcloud/worker/bin/cloud_hub.py\ncloud/worker/bin/cloud_hub.py\tcloud/worker/y"),
    ("f", "bin/cloud_cli.py\tcloud/worker/dup\nbin/cloud_hub.py\tcloud/worker/dup"),
    ("g", "# a comment"),
)


def put_table(d, extra):
    with open(os.path.join(d, TABLE), "ab") as f:
        f.write(("\n" + extra + "\n").encode())


class Strict(unittest.TestCase):
    def refused(self, d):
        before = snapshot(d)
        status, out = run_in(d)
        return before, status, out

    def test_s8_seven_kinds_of_bad_row_are_refused_with_one_line(self):
        for name, row in BAD_ROWS:
            with self.subTest(row=name):
                d = scratch(self)
                put_table(d, row)
                before, status, out = self.refused(d)
                self.assertEqual(status, 2)
                lines = out.splitlines()
                self.assertEqual(len(lines), 1, out)
                self.assertRegex(lines[0], r"^row \d+: \S")
                self.assertEqual(snapshot(d), before)

    def test_s8_blank_lines_are_skipped(self):
        d = scratch(self)
        with open(os.path.join(d, TABLE), "ab") as f:
            f.write(b"\n\n")
        self.assertEqual(run_in(d), (0, "0 changed\n"))

    def check_copy_path_refused(self, d):
        put_table(d, "bin/cloud_cli.py\tREADME.md")
        shutil.copyfile(os.path.join(ROOT, "README.md"), os.path.join(d, "README.md"))
        readme = slurp("README.md", d)
        status, out = run_in(d)
        if status == 0 or slurp("README.md", d) != readme:
            raise AssertionError("a row outside cloud/worker was accepted: bin/cloud_cli.py\tREADME.md")

    def test_s8_mutant_without_the_copy_path_test_is_caught(self):
        control = scratch(self)
        self.check_copy_path_refused(control)   # the control passes first
        d = scratch(self)
        original = slurp(COMMAND).decode()
        old = 'if not copy.startswith("cloud/worker/") or ".." in copy.split("/"):'
        self.assertEqual(original.count(old), 1)
        mutated = original.replace(old, 'if False:')
        self.assertNotEqual(mutated, original)
        self.assertIn(old, original)
        self.assertNotIn(old, mutated)
        with open(os.path.join(d, COMMAND), "w") as f:
            f.write(mutated)
        with self.assertRaisesRegex(AssertionError, "a row outside cloud/worker was accepted: bin/cloud_cli.py"):
            self.check_copy_path_refused(d)

    def test_s8_a_no_op_mutation_fails_the_computation(self):
        original = slurp(COMMAND).decode()
        mutated = original.replace("not-in-the-text", "x")
        with self.assertRaises(AssertionError):
            self.assertNotEqual(mutated, original, "the mutation was not applied")


class Safe(unittest.TestCase):
    def test_s7_no_source_changes_no_deletion_no_contents(self):
        d = scratch(self)
        equal = snapshot(d)
        status, out = run_in(d)
        self.assertEqual((status, out), (0, "0 changed\n"))
        self.assertEqual(snapshot(d), equal)
        with open(os.path.join(d, "bin/cloud_hub.py"), "ab") as f:
            f.write(b"SECRET-LINE-OF-A-SOURCE\n")
        before = snapshot(d)
        status, out = run_in(d)
        after = snapshot(d)
        self.assertEqual(status, 0)
        self.assertEqual(set(after), set(before))   # no deletion, no new file
        sources = {s for s, _ in rows(d)}
        for rel in sources:
            self.assertEqual(after[rel], before[rel])
        self.assertEqual([r for r in after if after[r] != before[r]], ["cloud/worker/bin/cloud_hub.py"])
        self.assertNotIn("SECRET-LINE", out)
        for line in out.splitlines():
            self.assertRegex(line, r"^(copied \S+ to \S+|\d+ changed)$")


if __name__ == "__main__":
    unittest.main()
