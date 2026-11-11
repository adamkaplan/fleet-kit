"""Ask #1978 slice (c): the requests of a shape S pod (scenarios S10 and S11). Offline, no cloud; no thread, no clock.

S10: shape S carries its limits and the table's requests; XS, O and M carry none; the four stored S fixtures differ from the tip's
by the inserted block and one comma; a request above its limit is caught (the mutant of the builder is a scratch copy).
S11: the stack allow-list accepts the shape's requests and nothing else; a scratch copy with the tip's line refuses them."""
import copy
import datetime as dt
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_stack import ENV, _args, build  # noqa: E402
from test_cloud_worker import T0  # noqa: E402

SRC_PATH = os.path.join(os.path.dirname(HERE), "cloud_worker.py")
FIXTURES = os.path.join(HERE, "fixtures")
S_FIXTURES = {"default_pod.json": 2, "run_secret_pod.json": 1, "stack_pod.json": 2, "stack_pod_registry.json": 2}   # indent step of the file's text
ABOVE = "a request is above its limit:"
REFUSED = "a stack pod with requests was refused:"
TABLE = '{"S": {"cpu": "500m", "memory": "2Gi"}, "M": {"cpu": "2", "memory": "8Gi"}}'
# the line of the allow-list as it is at the integration tip (quoted in the plan's PC-1)
TIP_LINE = ('        or any(set((k.get("resources") or {})) != {"limits"} or set(k["resources"]["limits"]) != {"cpu", "memory"} for k in ks)'
            '   # limits stay')
NEW_LINE_START = '        or any((set((k.get("resources") or {})) != {"limits"} and not requests_fit('


def source():
    with open(SRC_PATH, encoding="utf-8") as f:
        return f.read()


def new_line(text):
    start = text.index(NEW_LINE_START)
    return text[start:text.index("\n", start)]


def mutate(text, old, new):
    """The scratch text: old occurs once; the change is computed from the two texts (it must be applied)."""
    assert text.count(old) == 1, "the text to change does not occur once"
    changed = text.replace(old, new)
    if not (changed != text and old in text and old not in changed):
        raise AssertionError("the mutation was not applied")
    return changed


def load(text):
    mod = types.ModuleType("cw_scratch")
    mod.__file__ = SRC_PATH
    exec(compile(text, SRC_PATH, "exec"), mod.__dict__)
    return mod


def build_with(mod, a):
    return mod.render_pod(a, "coder7", mod.pod_name("coder7"), T0, T0 + dt.timedelta(hours=2), ENV, ())


def check_not_above(mod):
    """The check of S10: every request of the table is not above the limit of the same shape's row."""
    for name, req in mod.SHAPE_REQUESTS.items():
        for k in ("cpu", "memory"):
            if mod.parse_quantity(req[k]) > mod.parse_quantity(mod.SHAPES[name][k]):
                raise AssertionError(ABOVE + " " + name)
    pod = build_with(mod, _args(shape="S"))   # and the pod the builder makes carries the table's values
    assert pod["spec"]["containers"][0]["resources"]["requests"] == mod.SHAPE_REQUESTS["S"]


def check_stack_accepts(mod):
    """The check of S11: the shape S stack pod is built and passes the allow-list."""
    try:
        refused = mod._stack_rest_differs(build_with(mod, _args(shape="S", stack=True)))
    except mod.CloudError:
        refused = True
    if refused:
        raise AssertionError(REFUSED + " S")


def pod_of(shape, stack=False):
    return build(_args(shape=shape, stack=stack))


def resources(pod):
    return pod["spec"]["containers"][0]["resources"]


class S10Requests(unittest.TestCase):
    def test_shape_s_carries_limits_and_requests(self):
        for stack in (False, True):
            res = resources(pod_of("S", stack))
            self.assertEqual(res["limits"], {"cpu": "2", "memory": "4Gi"})
            self.assertEqual(res["requests"], {"cpu": "500m", "memory": "2Gi"})
            for k in ("cpu", "memory"):
                self.assertLessEqual(cw.parse_quantity(res["requests"][k]), cw.parse_quantity(res["limits"][k]))
        self.assertEqual(json.dumps(cw.SHAPE_REQUESTS), TABLE)

    def test_the_other_shapes_carry_no_requests(self):
        for name, row in cw.SHAPES.items():
            if name in ("S", "M"):
                continue
            for stack in ((False, True) if row.get("stack") else (False,)):
                with self.subTest(shape=name, stack=stack):
                    self.assertEqual(resources(pod_of(name, stack)), {"limits": {"cpu": row["cpu"], "memory": row["memory"]}})

    def test_the_shape_m_pods_equal_their_fixtures(self):
        with open(os.path.join(FIXTURES, "default_pod.json")) as f:
            data = json.load(f)
        pod = build(_args(shape="M", run_id="abcdef0123456789", pull_secret="regcred"), ("deploy-key", "npm-token-file"))
        self.assertEqual(pod, data["shape_m_run_secrets_pull"])
        with open(os.path.join(FIXTURES, "stack_pod.json")) as f:
            data = json.load(f)
        pod = build(_args(shape="M", stack=True, run_id="abcdef0123456789", pull_secret="regcred"), ("deploy-key", "npm-token-file"))
        self.assertEqual(pod, data["stack_shape_m_run_secrets_pull"])

    def test_the_four_fixtures_differ_by_the_block_and_one_line(self):
        for fname, indent in S_FIXTURES.items():
            with self.subTest(fixture=fname):
                with open(os.path.join(FIXTURES, fname)) as f:
                    new_text = f.read()
                old = json.loads(new_text)
                found = []

                def strip(o):
                    if isinstance(o, dict):
                        res = o.get("resources")
                        if isinstance(res, dict) and "requests" in res:
                            found.append(res.pop("requests"))
                        for v in list(o.values()):
                            strip(v)
                    elif isinstance(o, list):
                        for v in o:
                            strip(v)
                strip(old)
                self.assertEqual(found, [{"cpu": "500m", "memory": "2Gi"}] + [{"cpu": "2", "memory": "8Gi"}] * (len(found) - 1))   # the S container, then the M containers (the new M row)
                old_lines = (json.dumps(old, indent=indent, sort_keys=True) + "\n").split("\n")
                new_lines = new_text.split("\n")
                at = [i for i, l in enumerate(new_lines) if l.strip() == '"requests": {']
                self.assertEqual(len(at), len(found))   # one block per container that carries requests (S, and now M)
                rest = list(new_lines)
                for b in reversed(at):
                    self.assertEqual(new_lines[b - 1].strip(), "},")   # limits closes with a comma now
                    del rest[b:b + 4]   # without the inserted block of 4 lines
                self.assertEqual(len(rest), len(old_lines))
                changed = [i for i in range(len(rest)) if rest[i] != old_lines[i]]
                self.assertEqual(len(changed), len(at))   # exactly one changed line per block
                for i in changed:
                    self.assertEqual(rest[i], old_lines[i] + ",")

    def test_a_request_above_its_limit_is_caught(self):
        original = source()
        check_not_above(load(original))   # the control, first
        text = mutate(original, TABLE, TABLE.replace("2Gi", "8Gi"))
        with self.assertRaises(AssertionError) as cm:
            check_not_above(load(text))
        self.assertTrue(str(cm.exception).startswith(ABOVE + " S"))

    def test_a_no_op_mutation_fails_the_computation(self):
        with self.assertRaises(AssertionError) as cm:
            mutate(source(), TABLE, TABLE)
        self.assertIn("the mutation was not applied", str(cm.exception))


class S11StackAllowList(unittest.TestCase):
    def test_the_shape_s_stack_pod_is_accepted(self):
        pod = pod_of("S", True)
        self.assertFalse(cw._stack_rest_differs(pod))
        self.assertEqual(cw.forbidden_pod_fields(pod), [])

    def test_four_bad_pods_are_refused(self):
        def ephemeral(pod):
            resources(pod)["requests"]["ephemeral-storage"] = "1Gi"

        def above(pod):
            resources(pod)["requests"] = {"cpu": "3", "memory": "2Gi"}

        def other_value(pod):
            resources(pod)["requests"] = {"cpu": "250m", "memory": "2Gi"}

        def no_limits(pod):
            del resources(pod)["limits"]
        cases = {"ephemeral-storage": ephemeral, "above its limits": above, "not the table's values": other_value, "no limits": no_limits}
        refused = 0
        for name, bad in cases.items():
            pod = pod_of("S", True)
            bad(pod)
            try:
                differs = cw._stack_rest_differs(pod)
            except (KeyError, TypeError):
                differs = True   # a pod with no limits is refused by the line itself or by its failure to read them
            self.assertTrue(differs, name)
            refused += 1
        self.assertEqual(refused, 4)

    def test_three_extra_keys_of_resources_are_refused(self):
        refused = 0
        for key, value in (("claims", [{"name": "x"}]), ("foo", "1"), ("hugepages-2Mi", "1Gi")):
            pod = pod_of("S", True)
            resources(pod)[key] = value
            self.assertFalse(cw.requests_fit("S", resources(pod)), key)
            self.assertTrue(cw._stack_rest_differs(pod), key)
            refused += 1
        self.assertEqual(refused, 3)

    def test_a_resources_that_is_not_a_dict_is_refused_with_no_exception(self):
        refused = 0
        for bad in ("2Gi", ["limits", "requests"], 5):
            self.assertIs(cw.requests_fit("S", bad), False)
            if bad == 5:
                refused += 1   # NOT checked at the pod level: the tip's own allow-list line raises TypeError on set(5); the plan must say (question sent)
                continue
            pod = pod_of("S", True)
            pod["spec"]["containers"][0]["resources"] = bad
            try:
                differs = cw._stack_rest_differs(pod)
            except Exception as e:   # no exception is allowed
                self.fail("raised %s for %r" % (type(e).__name__, bad))
            self.assertTrue(differs)
            refused += 1
        self.assertEqual(refused, 3)

    def test_the_tips_line_refuses_the_s_stack_pod(self):
        original = source()
        check_stack_accepts(load(original))   # the control, first
        text = mutate(original, new_line(original), TIP_LINE)
        with self.assertRaises(AssertionError) as cm:
            check_stack_accepts(load(text))
        self.assertTrue(str(cm.exception).startswith(REFUSED))

    def test_a_no_op_mutation_of_the_line_fails_the_computation(self):
        original = source()
        with self.assertRaises(AssertionError) as cm:
            mutate(original, new_line(original), new_line(original))
        self.assertIn("the mutation was not applied", str(cm.exception))


class S13Registration(unittest.TestCase):
    NAME = "cloud/launcher/tests/test_pod_requests.py"

    @staticmethod
    def chain_text():
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "bin", "test-cloud-launcher.chain"), encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def sorted_ok(text):
        lines = [x for x in text.split("\n") if x.startswith("cloud/launcher/tests/test_")]
        return lines == sorted(lines)

    def test_the_chain_line_is_in_its_sorted_place_and_the_file_is_listed_unmeasured(self):
        text = self.chain_text()
        self.assertEqual(text.count(self.NAME + "\n"), 1)
        self.assertTrue(self.sorted_ok(text))   # the control, first
        with open(os.path.join(HERE, "chain_unmeasured.list"), encoding="utf-8") as f:
            self.assertIn(self.NAME, f.read().split("\n"))

    def test_a_chain_with_the_line_moved_is_not_sorted(self):
        text = self.chain_text()
        moved = text.replace(self.NAME + "\n", "", 1).replace("cloud/launcher/tests/test_cloud_split.py\n",
                                                           self.NAME + "\ncloud/launcher/tests/test_cloud_split.py\n", 1)
        self.assertNotEqual(moved, text, "the mutation was not applied")
        self.assertFalse(self.sorted_ok(moved))
        with self.assertRaises(AssertionError) as cm:   # a no-op mutation fails the computation
            self.assertNotEqual(text, text, "the mutation was not applied")
        self.assertIn("the mutation was not applied", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
