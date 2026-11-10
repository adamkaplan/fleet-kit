"""Ask #2344: the shape M request is CPU 2 / memory 8Gi (limits 4 / 8Gi), S, XS and O are as before, and the capacity read counts room by the request."""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_pod_requests as base   # the render helpers of the S10 tests

cw = base.cw
SRC_PATH = base.SRC_PATH
NOT_LOWERED = "the M request was not lowered"
M_ROW = '"M": {"cpu": "2", "memory": "8Gi"}'
FREE = {"cpu_m": 7820, "memory_b": 26 * 1024 ** 3}


def check_m(mod):
    """The check of S2: an M pod carries requests 2 / 8Gi and limits 4 / 8Gi."""
    for stack in (False, True):
        res = base.resources(base.build_with(mod, base._args(shape="M", stack=stack)))
        if res.get("requests") != {"cpu": "2", "memory": "8Gi"}:
            raise AssertionError(NOT_LOWERED + ": " + repr(res.get("requests")))
        assert res["limits"] == {"cpu": "4", "memory": "8Gi"}, res["limits"]


class PodResources(unittest.TestCase):
    def test_m_pod_requests_and_limits(self):
        for stack in (False, True):
            res = base.resources(base.pod_of("M", stack))
            self.assertEqual(res["requests"], {"cpu": "2", "memory": "8Gi"})
            self.assertEqual(res["limits"], {"cpu": "4", "memory": "8Gi"})

    def test_s_pod_is_as_today(self):
        for stack in (False, True):
            res = base.resources(base.pod_of("S", stack))
            self.assertEqual(res, {"limits": {"cpu": "2", "memory": "4Gi"}, "requests": {"cpu": "500m", "memory": "2Gi"}})

    def test_xs_and_o_pods_are_as_today(self):
        for name in ("XS", "O"):
            row = cw.SHAPES[name]
            self.assertEqual(base.resources(base.pod_of(name)), {"limits": {"cpu": row["cpu"], "memory": row["memory"]}})
        self.assertEqual(cw.SHAPES["M"]["cpu"], "4")
        self.assertEqual(cw.SHAPES["M"]["memory"], "8Gi")


def block_room(shapes, shape, used):
    """The 'nodes with room' figure of bin/cloud_worker.py's capacity_block for ONE node with FREE allocatable and `used` already requested."""
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    spec = importlib.util.spec_from_file_location("cw_bin_for_m_request", os.path.join(root, "bin", "cloud_worker.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    body = {"nodes_read": True, "pods_read": True, "nodes": [dict(FREE, name="n1")], "requests": {"n1": used}}
    for line in mod.capacity_block(body, {shape: shapes[shape]}, cw._quantity):
        if line.startswith("  shape %s:" % shape):
            return int(line.split(":")[1].split()[0])


def rooms_on_one_node(shapes, shape):
    """How many pods of the shape the one node takes: place one at a time (adding its row's cpu and memory to the node's requests) while the block says it has room."""
    used, placed = {"cpu_m": 0, "memory_b": 0}, 0
    row = shapes[shape]
    while block_room(shapes, shape, used) == 1 and placed < 20:
        used = {"cpu_m": used["cpu_m"] + cw._quantity(row["cpu"], 1000), "memory_b": used["memory_b"] + cw._quantity(row["memory"], 1)}
        placed += 1
    return placed


class CapacityRoom(unittest.TestCase):
    def test_room_for_m_is_counted_by_the_request(self):
        self.assertEqual(rooms_on_one_node(cw.capacity_shapes(), "M"), 3)   # 7.82 CPU / 26 GiB free: 3 x (2 CPU, 8Gi)

    def test_a_shape_with_no_request_row_is_counted_by_its_limit(self):
        shapes = cw.capacity_shapes()
        self.assertNotIn("M", {k: v for k, v in cw.SHAPE_REQUESTS.items() if k not in ("S", "M")})
        for name in ("XS", "O"):
            self.assertEqual(shapes[name], cw.SHAPES[name])
        self.assertEqual(rooms_on_one_node(cw.SHAPES, "M"), 1)   # counted by its limit (4 CPU): 1 on the node
        self.assertEqual(rooms_on_one_node(shapes, "XS"), 7)   # XS has no row: 7.82 CPU / 1 CPU limit

    def test_the_limits_table_is_not_changed_by_the_read(self):
        cw.capacity_shapes()
        self.assertEqual((cw.SHAPES["M"]["cpu"], cw.SHAPES["M"]["memory"]), ("4", "8Gi"))


class Mutant(unittest.TestCase):
    def test_a_mutant_that_keeps_m_at_its_limit_fails_with_the_anchored_text(self):
        original = base.source()
        check_m(base.load(original))   # the control, first
        text = base.mutate(original, M_ROW, '"M": {"cpu": "4", "memory": "8Gi"}')
        with self.assertRaises(AssertionError) as cm:
            check_m(base.load(text))
        self.assertTrue(str(cm.exception).startswith(NOT_LOWERED), str(cm.exception))

    def test_a_no_op_mutation_fails_the_computation(self):
        with self.assertRaises(AssertionError) as cm:
            base.mutate(base.source(), M_ROW, M_ROW)
        self.assertIn("the mutation was not applied", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
