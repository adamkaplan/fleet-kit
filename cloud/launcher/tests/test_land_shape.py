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


def block_lines(body, shape, lib=cw):
    import importlib.util
    spec = importlib.util.spec_from_file_location("cw_bin_land_shape", os.path.join(ROOT, "bin", "cloud_worker.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.capacity_block(body, {shape: lib.capacity_shapes()[shape]}, lib._quantity)


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
        text = mreq.print_capacity_text(cw)   # the printing path returns normally: the launch goes on
        self.assertIn("  shape L:", text)

    def test_s9_an_unreadable_node_list_says_not_read_and_proceeds(self):
        body = {"nodes_read": False, "pods_read": False}
        lines = block_lines(body, "L")
        self.assertIn("  nodes: not read", lines)
        self.assertIn("  shape L: nodes with room: not counted", lines)
        import importlib.util
        spec = importlib.util.spec_from_file_location("cw_bin_land_shape9", os.path.join(ROOT, "bin", "cloud_worker.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
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
        for ref in ("origin/integration", "integration"):
            out = subprocess.run(["git", "diff", "-U0", ref + "...HEAD", "--", ".", ":!cloud/launcher/tests/test_land_shape.py"], cwd=ROOT, capture_output=True, text=True)
            if out.returncode == 0:
                return [l[1:] for l in out.stdout.splitlines() if l.startswith("+") and not l.startswith("+++")]
        self.skipTest("no base ref to diff against")

    def test_s7_the_diff_has_no_azure_create_nodepool_or_scale_command(self):
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
        mod = base.load(base.mutate(original, L_REQ, '"L": {"cpu": "3", "memory": "8Gi"}'))
        used = {"a": {"cpu_m": 3000, "memory_b": 8 * GIB}}   # the one pod of the mutant's own row
        self.assertEqual(count(room(["a", "b", "c"], used, lib=mod)), 3)   # where 2 are required: S3 would fail
        self.assertEqual(count(room(["a", "b", "c"], {"a": {"cpu_m": 6000, "memory_b": 16 * GIB}})), 2)   # the control


if __name__ == "__main__":
    unittest.main()
