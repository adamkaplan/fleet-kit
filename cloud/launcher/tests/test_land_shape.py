"""Ask #2457: shape L (limit and request both 6 CPU / 16Gi, nested Docker like M, 0.34 USD/hour) for a land.
S2 pod, S3 capacity count, S4 old rows byte-equal, S7 no Azure create in the diff, S8 no room, S9 unreadable node list; each with a mutant."""
import contextlib
import io
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pod_requests as base   # the render helpers
import test_m_request as mreq      # the capacity helpers

cw = base.cw
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
GIB = 1024 ** 3
NODE = {"cpu_m": 7000, "memory_b": 28 * GIB}
L_REQ = '"L": {"cpu": "6", "memory": "16Gi"}'
NOT_SIX = "the L request is not 6 CPU"
OLD_ROWS = {   # the rows of the base, byte-equal (S4)
    "XS": {"cpu": "1", "memory": "2Gi", "usd_per_hour": 0.05, "stack": False},
    "O": {"cpu": "250m", "memory": "768Mi", "usd_per_hour": 0.02, "stack": False},
    "S": {"cpu": "2", "memory": "4Gi", "usd_per_hour": 0.10, "stack": True},
    "M": {"cpu": "4", "memory": "8Gi", "usd_per_hour": 0.20, "stack": True}}
OLD_REQUESTS = {"S": {"cpu": "500m", "memory": "2Gi"}, "M": {"cpu": "2", "memory": "8Gi"}}


def check_l(mod):
    """S2: an L pod carries requests equal to its limits, 6 CPU / 16Gi, with and without a stack."""
    for stack in (False, True):
        res = base.resources(base.build_with(mod, base._args(shape="L", stack=stack)))
        want = {"cpu": "6", "memory": "16Gi"}
        if res.get("requests") != want:
            raise AssertionError(NOT_SIX + ": " + repr(res.get("requests")))
        assert res["limits"] == want, res["limits"]


def room(nodes, used, shape="L", lib=cw):
    """The 'nodes with room' figure of the capacity block for `shape` (None: the line is absent)."""
    body = {"nodes_read": True, "pods_read": True, "nodes": [dict(NODE, name=n) for n in nodes], "requests": used}
    return block_lines(body, shape, lib)


def load_bin(tag, edit=None):
    """A fresh copy of bin/cloud_worker.py (the launcher library stubbed to the one under test by the caller); edit=(old, new) mutates its source."""
    import importlib.util
    path = os.path.join(ROOT, "bin", "cloud_worker.py")
    spec = importlib.util.spec_from_file_location("cw_bin_land_shape_" + tag, path)
    mod = importlib.util.module_from_spec(spec)
    if edit:
        text = open(path).read()
        assert text.count(edit[0]) == 1, edit[0]
        exec(compile(text.replace(*edit), path, "exec"), mod.__dict__)
    else:
        spec.loader.exec_module(mod)
    return mod


def block_lines(body, shape, lib=cw):
    return load_bin("block").capacity_block(body, {shape: lib.capacity_shapes()[shape]}, lib._quantity)


def print_text(answer_body, code=0, edit=None):
    """What _print_capacity writes to stderr when the launcher answers (code, body)."""
    mod = load_bin("print", edit)
    mod._lib = lambda: cw
    mod.run_launcher = lambda *a, **k: (code, answer_body, "")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        mod._print_capacity(None, None)   # must not raise
    return err.getvalue()


def count(lines, shape="L"):
    line = [l for l in lines if l.startswith("  shape %s:" % shape)][0]
    return int(line.split(":")[1].split()[0])


class Row(unittest.TestCase):
    def test_s2_the_row_and_the_pod(self):
        self.assertEqual(cw.SHAPES["L"], {"cpu": "6", "memory": "16Gi", "usd_per_hour": 0.34, "stack": True})
        self.assertEqual(cw.SHAPE_REQUESTS["L"], {"cpu": "6", "memory": "16Gi"})
        check_l(cw)

    def test_s4_old_rows_are_byte_equal_and_the_default_is_xs(self):
        for name, row in OLD_ROWS.items():
            self.assertEqual(cw.SHAPES[name], row, name)
        self.assertEqual({k: v for k, v in cw.SHAPE_REQUESTS.items() if k != "L"}, OLD_REQUESTS)
        self.assertEqual(cw.DEFAULT_SHAPE, "XS")
        self.assertEqual(cw.DEFAULT_STACK_SHAPE, "S")
        self.assertEqual(base.resources(base.pod_of("XS")), {"limits": {"cpu": "1", "memory": "2Gi"}})

    def test_the_cli_choices_and_validation_take_l(self):
        self.assertIn("L", cw.SHAPES)   # the --shape help and the validation read this table
        help_text = [a for a in cw.build_parser()._subparsers._group_actions[0].choices["create"]._actions if a.dest == "shape"][0].help
        self.assertIn("L", re.findall(r"\b[A-Z]{1,2}\b", help_text))


class Capacity(unittest.TestCase):
    def test_s3_three_nodes_then_two_after_one_l_pod(self):
        self.assertEqual(count(room(["a", "b", "c"], {})), 3)
        used = {"a": {"cpu_m": 6000, "memory_b": 16 * GIB}}
        self.assertEqual(count(room(["a", "b", "c"], used)), 2)   # two L pods never share a node

    def test_s8_no_room_prints_zero_nodes_and_does_not_refuse(self):
        used = {n: {"cpu_m": 6000, "memory_b": 16 * GIB} for n in "abc"}
        lines = room(["a", "b", "c"], used)
        self.assertEqual(count(lines), 0, lines)
        body = {"nodes_read": True, "pods_read": True, "nodes": [dict(NODE, name=n) for n in "abc"], "requests": used}
        self.assertIn("shape L: 0 nodes with room", print_text(body))   # the printing path returns normally: the launch goes on

    def test_s9_an_unreadable_node_list_says_not_read_and_proceeds(self):
        body = {"nodes_read": False, "pods_read": False}
        lines = block_lines(body, "L")
        self.assertIn("  nodes: not read", lines)
        self.assertIn("  shape L: nodes with room: not counted", lines)
        mod = load_bin("s9")
        mod._lib = lambda: cw
        mod.run_launcher = lambda *a, **k: (1, None, "unreadable")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mod._print_capacity(None, None)   # must not raise
        self.assertIn("nodes: not read", err.getvalue())
        self.assertIn("shape L:", err.getvalue())


class NoAzureCreate(unittest.TestCase):
    PATTERN = re.compile(r"\baz\s+(aks|group|vm|network|acr)\b[^\n]*\b(create|nodepool|scale)\b|\bnodepool\s+(add|scale|update)\b|\bscale\b[^\n]*--node-count", re.I)

    def added(self):
        """The text of the files the shape-L change touched: the shape table region of cloud_worker.py and the README section (the working tree, not the pull request's diff)."""
        with open(os.path.join(ROOT, "cloud/launcher/cloud_worker.py"), encoding="utf-8") as f:
            src = f.read()
        table = src[src.index("SHAPES = {"):src.index("DEFAULT_SHAPE =")]
        with open(os.path.join(ROOT, "cloud/README.md"), encoding="utf-8") as f:
            readme = f.read()
        start = readme.index("## Shape `L`, for a land")
        end = readme.find("\n## ", start + 1)
        section = readme[start:end if end != -1 else None]
        return (table + "\n" + section).splitlines()

    def test_s7_the_shape_l_text_has_no_azure_create_nodepool_or_scale_command(self):
        self.assertEqual([l for l in self.added() if self.PATTERN.search(l)], [])

    def test_s7_the_scan_finds_a_planted_command(self):
        for line in ("az aks nodepool add --name land", "az aks scale --node-count 3", "az group create -n x"):
            self.assertTrue(self.PATTERN.search(line), line)


class Mutants(unittest.TestCase):
    def test_a_request_left_at_two_cpu_fails_with_the_anchored_text(self):
        original = base.source()
        check_l(base.load(original))   # the control, first
        with self.assertRaises(AssertionError) as cm:
            check_l(base.load(base.mutate(original, L_REQ, '"L": {"cpu": "2", "memory": "16Gi"}')))
        self.assertTrue(str(cm.exception).startswith(NOT_SIX), str(cm.exception))

    def test_a_request_of_three_cpu_counts_three_nodes_after_one_pod(self):
        original = base.source()
        def check(lib, used):
            got = count(room(["a", "b", "c"], used, lib=lib))
            if got != 2:
                raise AssertionError("S3: nodes with room after one L pod is %d, not 2" % got)
        check(cw, {"a": {"cpu_m": 6000, "memory_b": 16 * GIB}})   # the control, first
        mod = base.load(base.mutate(original, L_REQ, '"L": {"cpu": "3", "memory": "8Gi"}'))
        with self.assertRaises(AssertionError) as cm:
            check(mod, {"a": {"cpu_m": 3000, "memory_b": 8 * GIB}})   # the one pod of the mutant's own row
        self.assertEqual(str(cm.exception), "S3: nodes with room after one L pod is 3, not 2")


class MoreMutants(unittest.TestCase):
    def test_s4_a_changed_m_request_is_caught_with_anchored_text(self):
        original = base.source()
        row = '"M": {"cpu": "2", "memory": "8Gi"}'
        def check(mod):
            if mod.SHAPE_REQUESTS["M"] != OLD_REQUESTS["M"]:
                raise AssertionError("old row changed: M request " + repr(mod.SHAPE_REQUESTS["M"]))
        check(base.load(original))   # control first
        with self.assertRaises(AssertionError) as cm:
            check(base.load(base.mutate(original, row, '"M": {"cpu": "3", "memory": "8Gi"}')))
        self.assertTrue(str(cm.exception).startswith("old row changed"), str(cm.exception))

    def test_s8_a_capacity_block_that_hides_the_zero_is_caught(self):
        body = {"nodes_read": True, "pods_read": True, "nodes": [dict(NODE, name=n) for n in "abc"],
                "requests": {n: {"cpu_m": 6000, "memory_b": 16 * GIB} for n in "abc"}}
        def check(text):
            if "shape L: 0 nodes with room" not in text:
                raise AssertionError("S8: the no-room line is missing")
        check(print_text(body))   # control first
        with self.assertRaises(AssertionError) as cm:
            check(print_text(body, edit=('"  shape %s: %d nodes with room" % (shape, room)', '"  shape %s: nodes with room: not counted" % shape')))
        self.assertEqual(str(cm.exception), "S8: the no-room line is missing")

    def test_s9_a_capacity_block_that_hides_not_read_is_caught(self):
        def check(text):
            if "nodes: not read" not in text:
                raise AssertionError("S9: 'nodes: not read' is missing")
        text = print_text(None, code=1)
        check(text)   # control first
        with self.assertRaises(AssertionError) as cm:
            check(print_text(None, code=1, edit=('"read" if nodes else "not read"', '"read"')))
        self.assertEqual(str(cm.exception), "S9: 'nodes: not read' is missing")

    def test_s4_a_changed_old_row_is_caught_with_anchored_text(self):
        original = base.source()
        def check(mod):
            for name, row in OLD_ROWS.items():
                if mod.SHAPES[name] != row:
                    raise AssertionError("S4: old row changed: " + name)
        check(base.load(original))   # control first
        with self.assertRaises(AssertionError) as cm:
            check(base.load(base.mutate(original, '"usd_per_hour": 0.20', '"usd_per_hour": 0.21')))
        self.assertEqual(str(cm.exception), "S4: old row changed: M")

    def test_s8_s9_no_room_and_unreadable_never_end_the_launch(self):
        mod = load_bin("m")
        mod._lib = lambda: cw
        full = {n: {"cpu_m": 6000, "memory_b": 16 * GIB} for n in "abc"}
        body = {"nodes_read": True, "pods_read": True, "nodes": [dict(NODE, name=n) for n in "abc"], "requests": full}
        for name, answer in (("no room", (0, body, "")), ("unreadable", (1, None, "x"))):
            mod.run_launcher = lambda *a, **k: answer
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                mod._print_capacity(None, None)   # control: must not raise
            self.assertIn("shape L:", err.getvalue(), name)
        mod.run_launcher = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            mod._print_capacity(None, None)   # an unexpected failure of the read is said, never raised: the launch goes on



if __name__ == "__main__":
    unittest.main()
