"""Ask #1928: one table (cloud/worker/copies.tsv) and one command (tools/make-worker-copies) for the worker copies. Offline; scratch trees."""
import ast
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
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


PATH_LIMIT = 1024   # the limit the command states: PATH_LIMIT characters in one path of the table
S12_ROWS = (
    ("c-long-path", ("bin/cloud_cli.py\tcloud/worker/" + "a" * 5000).encode()),
    ("d-not-utf8", b"bin/cloud_cli.py\tcloud/worker/\xff\xfe"),
    ("e-copy-dot", b"bin/cloud_cli.py\tcloud/worker/./x"),
    ("f-source-dot", b"./bin/cloud_cli.py\tcloud/worker/x"),
    ("g-double-slash", b"bin/cloud_cli.py\tcloud/worker//x"),
    ("h-trailing-slash", b"bin/cloud_cli.py\tcloud/worker/x/"),
)


def put_bytes(d, row):
    with open(os.path.join(d, TABLE), "ab") as f:
        f.write(b"\n" + row + b"\n")


def make_links(test, d):
    """An outside file and an outside directory (kept apart from the scratch tree) and two links to them inside cloud/worker."""
    out = tempfile.mkdtemp(prefix="wc-outside-")
    test.addCleanup(shutil.rmtree, out, True)
    target = os.path.join(out, "target.txt")
    with open(target, "wb") as f:
        f.write(b"outside bytes\n")
    os.mkdir(os.path.join(out, "dir"))
    os.symlink(target, os.path.join(d, "cloud/worker/leaf"))
    os.symlink(os.path.join(out, "dir"), os.path.join(d, "cloud/worker/linkdir"))
    return out, target


def check_symlink_refused(d, target, row):
    put_bytes(d, row.encode())
    before = slurp_abs(target)
    status, out = run_in(d)
    if status == 0 or slurp_abs(target) != before:
        raise AssertionError("a symlink led out of cloud/worker: " + row)
    return status, out


def slurp_abs(path):
    with open(path, "rb") as f:
        return f.read()


class Real(unittest.TestCase):
    def test_s12_eight_kinds_of_bad_path_are_refused_with_one_line(self):
        for name, row in S12_ROWS + (("a-leaf-symlink", b"bin/cloud_cli.py\tcloud/worker/leaf"),
                                     ("b-dir-symlink", b"bin/cloud_cli.py\tcloud/worker/linkdir/x")):
            with self.subTest(row=name):
                d = scratch(self)
                out_dir, target = make_links(self, d)
                put_bytes(d, row)
                before = snapshot(d)
                target_before = slurp_abs(target)
                status, out = run_in(d)
                self.assertEqual(status, 2, out)
                lines = out.splitlines()
                self.assertEqual(len(lines), 1, out)
                self.assertRegex(lines[0], r"^row \d+: \S")
                self.assertNotIn("Traceback", out)
                self.assertEqual(snapshot(d), before)          # no file changed or created
                self.assertEqual(slurp_abs(target), target_before)
                self.assertEqual(os.listdir(os.path.join(out_dir, "dir")), [])

    def test_s12_the_command_states_its_limit(self):
        text = slurp(COMMAND).decode()
        self.assertIn("PATH_LIMIT = %d" % PATH_LIMIT, text)

    def test_s12_a_mutant_that_judges_the_written_path_is_caught(self):
        control = scratch(self)
        _, target = make_links(self, control)
        check_symlink_refused(control, target, "bin/cloud_cli.py\tcloud/worker/leaf")   # the control first
        d = scratch(self)
        _, target = make_links(self, d)
        original = slurp(COMMAND).decode()
        old = ("    base = os.path.realpath(os.path.join(root, sub))\n"
               "    real = os.path.realpath(os.path.join(root, rel))\n")
        self.assertEqual(original.count(old), 1)
        mutated = original.replace(old, "    base = os.path.abspath(os.path.join(root, sub))\n"
                                        "    real = os.path.abspath(os.path.join(root, rel))\n")
        self.assertNotEqual(mutated, original, "the mutation was not applied")
        self.assertIn(old, original)
        self.assertNotIn(old, mutated)
        with open(os.path.join(d, COMMAND), "w") as f:
            f.write(mutated)
        with self.assertRaisesRegex(AssertionError, "a symlink led out of cloud/worker: bin/cloud_cli.py\tcloud/worker/leaf"):
            check_symlink_refused(d, target, "bin/cloud_cli.py\tcloud/worker/leaf")

    def test_s12_a_no_op_mutation_fails_the_computation(self):
        original = slurp(COMMAND).decode()
        with self.assertRaises(AssertionError):
            self.assertNotEqual(original.replace("not-in-the-text", "x"), original, "the mutation was not applied")

    def test_s12_the_orchestrator_test_file_closes_its_table(self):
        r = subprocess.run([sys.executable, "-B", "-W", "error::ResourceWarning", os.path.join(HERE, "test_orchestrator_agent.py")],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90, cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout.decode(errors="replace")[-600:])
        # other opens of that file (lines 105, 236, 255 ...) are outside PC-1 and still warn: only the read of the table is judged here
        self.assertNotIn(b"copies.tsv", r.stdout)


TREE = ("tools", "bin", "cloud", "agents", "tests", "docs")
THREE = {
    "cloud/launcher/tests/test_orchestrator_agent.py": (["cloud/launcher/tests", "test_orchestrator_agent.Drift"],
                                                        [("self.assertIsNone(drift(ROOT, source, copy))", "pass")]),
    "bin/test-cloud-hub": (["bin/test-cloud-hub", "-k", "test_worker_copies_match_bin"],
                           [("self.assertEqual((BIN / name).read_text(), (BIN.parent / copy).read_text(), name)", "pass")]),
    "tests/switchboard/test_relay_loop_cadence.py": (["tests/switchboard", "test_relay_loop_cadence.HubFilesUnchanged"],
                                                     [("self.assertEqual((BIN.parent / copy).read_bytes(), (BIN.parent / source).read_bytes())", "pass"),
                                                      ("self.assertEqual(other.read_bytes(), (BIN / other.name).read_bytes(), other.name)", "pass")]),
}


def big_scratch(test):
    d = tempfile.mkdtemp(prefix="wc-big-")
    test.addCleanup(shutil.rmtree, d, True)
    for top in TREE:
        shutil.copytree(os.path.join(ROOT, top), os.path.join(d, top), ignore=shutil.ignore_patterns("__pycache__", ".git"))
    return d


def run_comparing(d, rel):
    where, arg = THREE[rel][0][0], THREE[rel][0][1:]
    if rel == "bin/test-cloud-hub":
        cmd = [sys.executable, "-B", os.path.join(d, rel)] + arg
        cwd = d
    else:
        cmd = [sys.executable, "-B", "-m", "unittest"] + arg
        cwd = os.path.join(d, where)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "TMPDIR")}
    r = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=90)
    return r.returncode, r.stdout.decode(errors="replace")


def check_fails_on_drift(d, rel):
    status, out = run_comparing(d, rel)
    if status == 0:
        raise AssertionError("a comparing test did not fail on a drifted copy: " + rel)
    return out


def drift_every_copy(d):
    for _, copy in rows(d):
        with open(os.path.join(d, copy), "ab") as f:
            f.write(b"\n")


def mutate_file(d, rel, test):
    path = os.path.join(d, rel)
    original = slurp(rel, d).decode()
    mutated = original
    for old, new in THREE[rel][1]:
        test.assertEqual(original.count(old), 1)
        mutated = mutated.replace(old, new)
        test.assertIn(old, original)
        test.assertNotIn(old, mutated)
    test.assertNotEqual(mutated, original, "the mutation was not applied")
    with open(path, "w") as f:
        f.write(mutated)
    return original


def matrix_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from matrix_strings(k)
            yield from matrix_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from matrix_strings(v)


def plain_matrix_path(path, root):
    """A matrix path made plain by its real path relative to the repository; a path that cannot be is refused loudly."""
    try:
        base = os.path.realpath(root)
        rel = os.path.relpath(os.path.realpath(os.path.join(base, path)), base)
    except (OSError, ValueError):
        raise AssertionError("a matrix path was not understood: " + repr(path))
    if rel == "." or rel == ".." or rel.startswith("../") or os.path.isabs(path):
        raise AssertionError("a matrix path was not understood: " + repr(path))
    return rel


def check_matrix(matrix_text, table_rows, root=ROOT):
    copies = {c for _, c in table_rows}
    named = []
    for raw in matrix_strings(json.loads(matrix_text)):
        if "cloud/worker" not in raw:
            continue   # not a path of cloud/worker: a sentence or another tree
        path = plain_matrix_path(raw, root)
        if path.startswith("cloud/worker/bin/") or path in copies:
            named.append(path)
    named.sort()
    for path in named:
        if path not in copies:
            raise AssertionError("the matrix names a copy that is not in the table: " + path)
    return named, len(copies - set(named))


def allowed_copy(text):
    """The entries of ALLOWED_COPY read by syntax in every form of an assignment of a collection of strings."""
    unread = AssertionError("ALLOWED_COPY has a form that was not read:")
    found = None
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ALLOWED_COPY" for t in node.targets):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "ALLOWED_COPY" and node.value is not None:
            value = node.value
        else:
            continue
        if found is not None:
            raise unread
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ("frozenset", "set", "tuple", "list") \
                and len(value.args) == 1 and not value.keywords:
            value = value.args[0]
        if not isinstance(value, (ast.Set, ast.Tuple, ast.List)):
            raise unread
        try:
            items = [ast.literal_eval(e) for e in value.elts]
        except ValueError:
            raise unread
        if not all(isinstance(i, str) for i in items):
            raise unread
        found = sorted(items)
    if found is None:
        raise unread
    return found


def check_allowed(text, table_rows):
    copies = {c for _, c in table_rows}
    entries = allowed_copy(text)
    for path in entries:
        if path not in copies:
            raise AssertionError("an allowed carrier is not a copy in the table: " + path)
    return entries


class Comparing(unittest.TestCase):
    def test_s9_the_three_tests_fail_on_a_drifted_copy_and_pass_after_the_command(self):
        d = big_scratch(self)
        for rel in THREE:
            self.assertEqual(run_comparing(d, rel)[0], 0, rel)   # the control: undrifted passes
        drift_every_copy(d)
        names = {os.path.basename(c) for _, c in rows(d)}
        for rel in THREE:
            with self.subTest(test=rel):
                out = check_fails_on_drift(d, rel)
                self.assertTrue(any(n in out for n in names | {os.path.basename(rel)}), "the failure names no pair or file: " + rel)
        status, out = run_in(d)
        self.assertEqual(status, 0)
        for rel in THREE:
            with self.subTest(after=rel):
                self.assertEqual(run_comparing(d, rel)[0], 0, rel)

    def test_s9_a_test_whose_comparison_is_pass_is_caught(self):
        d = big_scratch(self)
        drift_every_copy(d)
        for rel in THREE:
            check_fails_on_drift(d, rel)   # the control first, for each
        for rel in THREE:
            with self.subTest(test=rel):
                original = mutate_file(d, rel, self)
                try:
                    with self.assertRaisesRegex(AssertionError, "a comparing test did not fail on a drifted copy: " + re.escape(rel)):
                        check_fails_on_drift(d, rel)
                finally:
                    with open(os.path.join(d, rel), "w") as f:
                        f.write(original)

    def test_s9_a_no_op_mutation_fails_the_computation(self):
        original = slurp("bin/test-cloud-hub").decode()
        mutated = original.replace("not-in-the-text", "x")
        with self.assertRaises(AssertionError):
            self.assertNotEqual(mutated, original, "the mutation was not applied")

    def test_s10_the_matrix_names_only_copies_of_the_table(self):
        named, unnamed = check_matrix(slurp("docs/review-laws/impact-matrix.json").decode(), rows())
        print("S10 matrix paths:", named, "rows:", [r for r in rows() if r[1] in named], "copies not named:", unnamed)
        self.assertEqual(len(named), 2)

    def test_s10_a_table_without_the_relay_row_is_caught(self):
        text = slurp("docs/review-laws/impact-matrix.json").decode()
        table = slurp(TABLE).decode()
        row = "bin/cloud_relay.py\tcloud/worker/bin/cloud_relay.py\n"
        self.assertEqual(table.count(row), 1)
        mutated = table.replace(row, "")
        self.assertNotEqual(mutated, table, "the mutation was not applied")
        self.assertIn(row, table)
        self.assertNotIn(row, mutated)
        check_matrix(text, rows())   # the control first
        with self.assertRaisesRegex(AssertionError, "the matrix names a copy that is not in the table: cloud/worker/bin/cloud_relay.py"):
            check_matrix(text, [tuple(l.split("\t")) for l in mutated.splitlines() if l])

    def test_s11_the_allowlist_entries_are_rows(self):
        text = slurp("cloud/launcher/tests/test_cw_names.py").decode()
        entries = check_allowed(text, rows())
        print("S11 allowlist:", entries, "rows:", [r for r in rows() if r[1] in entries])
        self.assertEqual(len(entries), 1)

    def test_s11_one_more_entry_is_caught(self):
        text = slurp("cloud/launcher/tests/test_cw_names.py").decode()
        old = 'ALLOWED_COPY = {"cloud/worker/plan_check.py"}'
        self.assertEqual(text.count(old), 1)
        mutated = text.replace(old, 'ALLOWED_COPY = {"cloud/worker/plan_check.py", "cloud/worker/not_a_copy.py"}')
        self.assertNotEqual(mutated, text, "the mutation was not applied")
        self.assertIn(old, text)
        self.assertNotIn(old, mutated)
        check_allowed(text, rows())   # the control first
        with self.assertRaisesRegex(AssertionError, "an allowed carrier is not a copy in the table: cloud/worker/not_a_copy.py"):
            check_allowed(mutated, rows())

    def test_s11_a_no_op_mutation_fails_the_computation(self):
        text = slurp("cloud/launcher/tests/test_cw_names.py").decode()
        with self.assertRaises(AssertionError):
            self.assertNotEqual(text.replace("not-in-the-text", "x"), text, "the mutation was not applied")

    def test_s10_odd_forms_of_the_path_are_judged_like_the_plain_path(self):
        base = json.loads(slurp("docs/review-laws/impact-matrix.json").decode())
        self.assertEqual(len(check_matrix(json.dumps(base), rows())[0]), 2)
        for form in ("./cloud/worker/bin/cloud_relay.py", "cloud/worker/bin/cloud_relay.py/"):
            with self.subTest(form=form):
                got = check_matrix(json.dumps({"x": [form]}), rows())
                self.assertEqual(got[0], ["cloud/worker/bin/cloud_relay.py"])
                self.assertEqual(got[1], len(rows()) - 1)
        with self.assertRaisesRegex(AssertionError, "a matrix path was not understood:"):
            check_matrix(json.dumps({"x": ["cloud/worker/../../../etc/passwd"]}), rows())

    def test_s11_the_six_forms_are_read_and_an_unread_form_fails_loudly(self):
        text = slurp("cloud/launcher/tests/test_cw_names.py").decode()
        old = 'ALLOWED_COPY = {"cloud/worker/plan_check.py"}'
        self.assertEqual(text.count(old), 1)
        item = '"cloud/worker/plan_check.py"'
        forms = {
            "plain": 'ALLOWED_COPY = {%s}' % item,
            "annotated": 'ALLOWED_COPY: set = {%s}' % item,
            "set-over-lines": 'ALLOWED_COPY = {\n    %s,\n}' % item,
            "frozenset": 'ALLOWED_COPY = frozenset({%s})' % item,
            "tuple": 'ALLOWED_COPY = (%s,)' % item,
            "list": 'ALLOWED_COPY = [\n    %s,\n]' % item,
        }
        d = tempfile.mkdtemp(prefix="wc-forms-")
        self.addCleanup(shutil.rmtree, d, True)
        for name, form in forms.items():
            with self.subTest(form=name):
                mutated = text.replace(old, form)
                self.assertNotEqual(mutated, text, "the mutation was not applied")
                path = os.path.join(d, name + ".py")
                with open(path, "w") as f:
                    f.write(mutated)
                self.assertEqual(check_allowed(slurp_abs(path).decode(), rows()), ["cloud/worker/plan_check.py"])
        for name, bad in (("call", 'ALLOWED_COPY = make_set()'), ("union", 'ALLOWED_COPY = {%s} | {"x"}' % item), ("number", 'ALLOWED_COPY = {1}')):
            with self.subTest(unreadable=name):
                with self.assertRaisesRegex(AssertionError, "ALLOWED_COPY has a form that was not read:"):
                    check_allowed(text.replace(old, bad), rows())


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
