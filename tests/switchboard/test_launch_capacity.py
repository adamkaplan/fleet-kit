"""#1793: the capacity block of `launch --cloud` (S2-S7, U4-U8 of the plan), on a FAKE cluster: a fake kube object with recorded calls answers the
launcher's real `capacity` reading, and a stub launcher (its own, logging every capacity call) hands that answer to the real launch of
bin/test-cloud-worker's fixtures. No cluster login exists here. bin/test-switchboard binds the class returned by build()."""
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from support.loader import sb

TOKEN = "ghs_PlantedTokenAbCdEf0123456789xyz"
PASSWORD_URL = "https://deploy:hunter2planted@registry.example.internal/x"
ADDRESS = "10.99.88.77"
BLOCK_HEAD = "cloud capacity before the create"
UPPER = "room is counted from the pods this login can see: an upper bound"
MUTANT_TEXT = "a refused read stopped the launch"
WRITE_VERBS = ("create", "patch", "apply", "delete", "annotate", "replace", "edit")

V4, V6, MAPPED = "10.99.88.77", "fd00:abcd::1", "::ffff:10.99.88.77"
U11_CHECKER = r"""
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("launcher_u11", sys.argv[1] + "/cloud/launcher/cloud_worker.py")
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)
class K:
    def list_nodes(self):
        return [{"metadata": {"name": n}, "status": {"allocatable": {"cpu": "4", "memory": "8Gi"}}} for n in ("n1", "host-10.99.88.77")]
    def list_pods_all(self):
        return [{"metadata": {"name": "p"}, "status": {"phase": "Running"}, "spec": {"nodeName": "host-fd00:abcd::1", "containers": []}}]
    def list_events(self):
        return [{"reason": "FailedScheduling", "message": "at 12:30:45: 10.99.88.77 fd00:abcd::1 ::ffff:10.99.88.77 Insufficient cpu"}]
    def scrub(self, text, limit=300):
        return lib.scrub(text, set(), limit)
text = json.dumps(lib.Launcher.capacity(type("L", (), {"kube": K()})(), None))
if any(p in text for p in ("10.99.88.77", "fd00:abcd::1", "ffff:10.99.88.77")):
    sys.exit("an address reached the block: " + text)
if "12:30:45" not in text or '"n1"' not in text:
    sys.exit("a clock time or a name was changed: " + text)
"""

CHECKER = r"""
WRITE_VERBS = ("create", "patch", "apply", "delete", "annotate", "replace", "edit")
import ast, importlib.util, re, sys
root = sys.argv[1]
spec = importlib.util.spec_from_file_location("launcher_copy", root + "/cloud/launcher/cloud_worker.py")
lib = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lib)
argvs = []
kube = lib.Kube.__new__(lib.Kube)
kube.run = lambda args, **kw: argvs.append(list(args)) or '{"items": []}'
for read in (kube.list_nodes, kube.list_pods_all, kube.list_events):
    read()
if len(argvs) != 3 or [a[0] for a in argvs] != ["get"] * 3 or [a[1] for a in argvs] != ["nodes", "pods", "events"] \
        or any(a[0] in WRITE_VERBS or any(w in a for w in WRITE_VERBS) for a in argvs):
    sys.exit("a capacity read used a write verb: %r" % argvs)
launch = open(root + "/bin/cloud_worker.py").read()
found = re.search(r"^CAPACITY_SECONDS = (\d+)", launch, re.M)
if not found or int(found.group(1)) != 5:
    sys.exit("the capacity read bound is not 5 s")
func = [n for n in ast.parse(launch).body if isinstance(n, ast.FunctionDef) and n.name == "_print_capacity"][0]
seen = []
class Refused(Exception):
    pass
class Lib:
    SHAPES = {}
    DEFAULT_ORCHESTRATOR_SHAPE = "O"
    @staticmethod
    def _quantity(v, d):
        return d
def refused(*a, **k):
    raise Refused("refused")
import io, contextlib
ns = {"sys": sys, "_lib": lambda: Lib, "CloudError": Refused, "run_launcher": refused, "CAPACITY_SECONDS": 5,
      "capacity_block": lambda body, *a, **k: ["  nodes: not read"]}
exec(compile(ast.Module([func], []), "copy", "exec"), ns)
err = io.StringIO()
try:
    with contextlib.redirect_stderr(err):
        ns["_print_capacity"](None, None)
except Exception:
    sys.exit("a refused read stopped the launch")
if "nodes: not read" not in err.getvalue():
    sys.exit("a refused read stopped the launch")
"""


WRAPPER = """#!%(python)s
import json, os, sys, time
argv = sys.argv[1:]
d = os.environ["STUB_DIR"]
if argv and argv[0] == "capacity":
    open(d + "/capacity-calls.log", "a").write(json.dumps(argv) + "\\n")
    if os.path.exists(d + "/capacity.hang"):
        time.sleep(60)
    if os.path.exists(d + "/capacity.fail"):
        sys.stderr.write("cloud-worker: the read was refused\\n")
        sys.exit(1)
    sys.stdout.write(open(d + "/capacity.json").read())
    sys.exit(0)
os.execv(%(python)r, [%(python)r, %(orig)r] + argv)
"""


class Spy(dict):
    """A node object that records every key the reading function asks for, by path."""
    def __init__(self, data, seen, path=""):
        super().__init__(data)
        self.seen, self.path = seen, path

    def _wrap(self, key, value):
        self.seen.add(self.path + "/" + str(key))
        return Spy(value, self.seen, self.path + "/" + str(key)) if isinstance(value, dict) else value

    def get(self, key, default=None):
        return self._wrap(key, dict.get(self, key, default)) if key in self else (self.seen.add(self.path + "/" + str(key)) or default)

    def __getitem__(self, key):
        return self._wrap(key, dict.__getitem__(self, key))


def node(name, cpu, memory="16Gi", address=ADDRESS):
    return {"metadata": {"name": name, "labels": {"secret": TOKEN}}, "status": {"allocatable": {"cpu": cpu, "memory": memory, "pods": "110"},
                                                                                   "addresses": [{"type": "InternalIP", "address": address}]}}


def pod(name, on, cpu, phase="Running", memory="1Gi"):
    return {"metadata": {"name": name}, "status": {"phase": phase},
            "spec": {"nodeName": on, "containers": [{"resources": {"requests": {"cpu": cpu, "memory": memory}}}]}}


# four nodes of known room: free cpu n1 7, n2 3, n3 2.5, n4 1.5  ->  XS 4, S 3 (n1 n2 n3), M 1 (n1)
NODES = [node("n1", "8", address=ADDRESS), node("n2", "4"), node("n3", "4"), node("n4", "2")]
PODS = [pod("a", "n1", "1"), pod("b", "n2", "1"), pod("c", "n3", "1500m"), pod("d", "n4", "500m"),
        pod("p1", None, "1", phase="Pending"), pod("p2", None, "2", phase="Pending")]
EVENT = {"reason": "FailedScheduling", "lastTimestamp": "2026-11-01T06:20:00Z", "involvedObject": {"name": "c-6152"},
         "message": "0/13 nodes are available: 12 Insufficient cpu. token %s url %s" % (TOKEN, PASSWORD_URL) + " x" * 150}


class FakeKube:
    """The launcher's kube object: it records every call, answers from lists, and refuses what it is told to."""
    def __init__(self, nodes=NODES, pods=PODS, events=(EVENT,), refuse=()):
        self.nodes, self.pods, self.events, self.refuse, self.calls = nodes, pods, events, refuse, []

    def _read(self, what, items):
        self.calls.append(("get", what))
        if what in self.refuse:
            raise LIB.CloudError("kubectl get: Error from server (Forbidden): %s is forbidden for %s" % (what, TOKEN))
        return list(items)

    def list_nodes(self):
        return self._read("nodes", self.nodes)

    def list_pods_all(self):
        return self._read("pods", self.pods)

    def list_events(self):
        return self._read("events", self.events)

    def scrub(self, text, limit=300):
        return LIB.scrub(text, {TOKEN, "hunter2planted"}, limit)


LIB = None


def build():
    global LIB
    root = Path(__file__).resolve().parents[2]
    if not (root / "cloud" / "worker" / "tests" / "stub_support.py").exists():
        @unittest.skip("cloud/worker/tests is not in this tree")
        class TestLaunchCapacity(unittest.TestCase):
            def test_skipped(self):
                pass
        return TestLaunchCapacity
    path = root / "bin" / "test-cloud-worker"
    loader = importlib.machinery.SourceFileLoader("cw_for_launch_capacity", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("cw_for_launch_capacity", loader))
    sys.modules["cw_for_launch_capacity"] = module
    loader.exec_module(module)

    class TestLaunchCapacity(module.Base):
        """The block says the room per shape, the Pending count and the last scheduler line, once, on stderr; it never refuses or changes a launch."""

        def setUp(self):
            super().setUp()
            global LIB
            self.cw = sb.load_module("cloud_worker").bind(sb.__dict__)
            LIB = self.cw._lib()
            original = self.stub_dir / "cloud-worker-original"
            self.stub.rename(original)
            self.stub.write_text(WRAPPER % {"python": sys.executable, "orig": str(original)})
            self.stub.chmod(0o755)
            self.set_cluster(FakeKube())

        # -- the fake cluster and the log of the stub
        def set_cluster(self, kube):
            self.kube = kube
            answer = LIB.Launcher.capacity(types.SimpleNamespace(kube=kube), None)
            (self.stub_dir / "capacity.json").write_text(json.dumps(answer, sort_keys=True))
            return answer

        def capacity_calls(self):
            log = self.stub_dir / "capacity-calls.log"
            return len(log.read_text().splitlines()) if log.exists() else 0

        def creates(self):
            return [c for c in self.stub_calls() if c and c[0] == "create"]

        def block(self, err):
            lines = err.splitlines()
            starts = [i for i, line in enumerate(lines) if line.startswith(BLOCK_HEAD)]
            self.assertEqual(len(starts), 1, "the block is printed %d times: %r" % (len(starts), err))
            i = starts[0]
            out = [lines[i]]
            for line in lines[i + 1:]:
                if not line.startswith("  "):
                    break
                out.append(line)
            return "\n".join(out)

        def rows(self, block):
            return dict(re.findall(r"shape (\w+): (\d+) nodes with room", block))

        # -- S2
        def test_s2_the_block_names_the_room_per_shape_the_pending_pods_and_the_event(self):
            code, out, err = self.launch_with("--cloud")
            self.assertEqual(code, 0, err)
            block = self.block(err)
            self.assertEqual(self.rows(block), {"XS": "4", "S": "3", "M": "1"}, block)
            self.assertIn("Pending pods: 2", block)
            event = [line for line in block.splitlines() if "last FailedScheduling:" in line]
            self.assertEqual(len(event), 1, block)
            self.assertIn("0/13 nodes are available: 12 Insufficient cpu", event[0])
            self.assertLessEqual(len(event[0].split("last FailedScheduling: ", 1)[1]), 200)
            self.assertIn(UPPER, block)

        def test_s2_room_counted_from_the_allocatable_alone_would_say_4_for_M(self):
            self.assertEqual(sum(1 for n in NODES if LIB._quantity(n["status"]["allocatable"]["cpu"], 1000) >= 4000), 3)   # the discriminator: 3, not the block's 1
            answer = self.set_cluster(FakeKube())
            self.assertEqual(answer["requests"]["n1"], {"cpu_m": 1000, "memory_b": 1024 ** 3})

        # -- S3
        def test_s3_no_room_for_M_the_launch_proceeds_and_the_block_says_0(self):
            small = [node("n%d" % i, "3") for i in range(1, 5)]
            self.set_cluster(FakeKube(nodes=small, pods=[]))
            code, out, err = self.launch_with("--cloud", "--shape", "M", "--stack")
            self.assertEqual(code, 0, err)
            self.assertEqual(len(self.creates()), 1)
            self.assertEqual(self.rows(self.block(err))["M"], "0")

        # -- S4 and the check S6 reuses
        def check_refused_read_does_not_stop(self, refuse, words, name="coder7"):
            self.set_cluster(FakeKube(refuse=refuse))
            (self.stub_dir / "calls.log").unlink(missing_ok=True)
            code, out, err = self.launch_with("--cloud", name=name)
            self.assertEqual((code, len(self.creates())), (0, 1), MUTANT_TEXT + ": " + err)
            self.assertIn(words, self.block(err))
            self.assertEqual({c[0] for c in self.kube.calls}, {"get"})
            self.assertEqual([c for c in self.kube.calls if c[0] in WRITE_VERBS], [])
            return err

        def test_s4_a_refused_nodes_read_says_not_read_and_the_launch_proceeds(self):
            self.check_refused_read_does_not_stop(("nodes",), "nodes: not read")

        def test_s4_a_refused_events_read_says_not_read_and_the_launch_proceeds(self):
            self.check_refused_read_does_not_stop(("events",), "events: not read")

        def test_s4_a_refused_pods_read_says_not_read_and_the_launch_proceeds(self):
            self.check_refused_read_does_not_stop(("pods",), "pods: not read")

        def test_s4_a_refused_read_leaks_nothing_of_the_refusal(self):
            err = self.check_refused_read_does_not_stop(("nodes",), "nodes: not read")
            self.assertNotIn(TOKEN, err)

        # -- S6: the control first (the unmutated copy passes), then the mutant (a refused read stops the launch) fails with the text of the check
        def test_s6_the_control_passes_and_the_mutant_fails_with_the_text(self):
            self.check_refused_read_does_not_stop(("nodes",), "nodes: not read")
            run_launcher = self.cw.run_launcher

            def mutant(runner, cluster):
                code, body, _ = run_launcher(runner, "capacity", "", (), 5, cluster=cluster)
                if isinstance(body, dict) and not body.get("nodes_read"):
                    raise self.cw.H["LaunchError"]("the nodes read was refused")
            with mock.patch.object(self.cw, "_print_capacity", mutant):
                with self.assertRaises(AssertionError) as caught:
                    self.check_refused_read_does_not_stop(("nodes",), "nodes: not read", name="coder8")
            self.assertIn(MUTANT_TEXT, str(caught.exception))

        # -- S5, U5
        def test_s5_standard_output_is_the_launch_json_alone_and_the_block_is_on_standard_error_once(self):
            code, out, err = self.launch_with("--cloud")
            self.assertEqual(code, 0, err)
            self.assertNotIn(BLOCK_HEAD, out)
            self.assertEqual(err.count(BLOCK_HEAD), 1)
            expected = json.dumps({"name": "coder7", "session": "ses_remote1", "runtime_id": "pod/coder7-7f9c", "url": self.remote.url, "cloud": True},
                                  sort_keys=True) + "\n"
            self.assertEqual(out.encode(), expected.encode())

        # -- S7, U6
        def test_s7_no_planted_token_password_or_address_is_in_the_block_and_the_names_are_kept(self):
            code, out, err = self.launch_with("--cloud")
            block = self.block(err)
            for planted in (TOKEN, "hunter2planted", ADDRESS):
                self.assertEqual(block.count(planted), 0, planted)
            answer = json.loads((self.stub_dir / "capacity.json").read_text())
            self.assertEqual([n["name"] for n in answer["nodes"]], ["n1", "n2", "n3", "n4"])
            self.assertEqual(set(answer["nodes"][0]), {"name", "cpu_m", "memory_b"})
            self.assertNotIn(ADDRESS, json.dumps(answer))

        def test_u6_only_the_name_and_the_allocatable_of_a_node_are_read(self):
            seen = set()
            kube = FakeKube(nodes=[Spy(n, seen) for n in NODES])
            LIB.Launcher.capacity(types.SimpleNamespace(kube=kube), None)
            self.assertEqual({k for k in seen if k.count("/") == 1}, {"/metadata", "/status"})
            self.assertFalse([k for k in seen if "address" in k or "labels" in k], seen)
            self.assertEqual({k for k in seen if k.count("/") == 2}, {"/metadata/name", "/status/allocatable"})

        # -- U4
        def test_u4_the_block_makes_three_reads_all_gets_and_a_hung_read_ends_in_not_read(self):
            self.launch_with("--cloud")
            self.assertEqual(self.kube.calls, [("get", "nodes"), ("get", "pods"), ("get", "events")])
            (self.stub_dir / "capacity.hang").write_text("")
            with mock.patch.object(self.cw, "CAPACITY_SECONDS", 1):
                code, out, err = self.launch_with("--cloud", name="coder9")
            self.assertEqual(code, 0, err)
            self.assertIn("nodes: not read", err)
            self.assertIn("events: not read", err)
            self.assertEqual(len(self.creates()), 2)

        # -- U5: ONE capacity call whatever the launcher does inside its own create
        def test_u5_one_capacity_call_and_one_block_per_launch(self):
            code, out, err = self.launch_with("--cloud")
            self.assertEqual((self.capacity_calls(), err.count(BLOCK_HEAD), out.count(BLOCK_HEAD)), (1, 1, 0))

        # -- U7
        def test_u7_the_rows_come_from_the_launchers_shapes_table(self):
            shapes = dict(LIB.SHAPES, Q={"cpu": "6", "memory": "4Gi", "usd_per_hour": 0.3, "stack": False})
            with mock.patch.dict(LIB.SHAPES, shapes):
                code, out, err = self.launch_with("--cloud")
            rows = self.rows(self.block(err))
            self.assertEqual(sorted(rows), ["M", "Q", "S", "XS"], err)
            self.assertEqual(rows["Q"], "1")
            self.assertNotIn("shape O", err)

        # -- U8
        def test_u8_one_capacity_call_for_a_launch_that_creates_a_pod(self):
            code, out, err = self.launch_with("--cloud")
            self.assertEqual((code, self.capacity_calls(), len(self.creates())), (0, 1, 1), err)

        def test_u8_no_capacity_call_for_a_launch_refused_before_any_pod(self):
            brief = self.home / "docker-brief.txt"
            brief.write_text("NEEDS: docker\n")
            code, out, err = self.main("launch", "coder7", "--cloud", "--agent", "coder", "--repo", "acme-org/widget-service", "--branch", "main",
                                       "--role", "coder", "--reports-to", "orch", "--brief-file", str(brief))
            self.assertNotEqual(code, 0)
            self.assertEqual((self.capacity_calls(), len(self.creates())), (0, 0), err)
            self.assertNotIn(BLOCK_HEAD, err)

        def test_u8_a_refused_capacity_read_says_not_read_and_the_create_runs_once(self):
            (self.stub_dir / "capacity.fail").write_text("")
            code, out, err = self.launch_with("--cloud")
            self.assertEqual((code, self.capacity_calls(), len(self.creates())), (0, 1, 1), err)
            self.assertIn("nodes: not read", err)
            self.assertIn("events: not read", err)

        # -- U10: the real read verbs, the shipped bound and the refused read, each with a control and a mutant on a scratch copy
        def scratch_check(self, mutate=None):
            import shutil, subprocess, tempfile
            root = Path(__file__).resolve().parents[2]
            with tempfile.TemporaryDirectory(prefix="u10-") as tmp:
                for src in sorted((root / "cloud" / "launcher").glob("*.py")) + [root / "bin" / "cloud_worker.py", root / "bin" / "path_walk.py"]:
                    rel = src.relative_to(root)
                    (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, Path(tmp) / rel)
                if mutate:
                    rel, old, new = mutate
                    text = (Path(tmp) / rel).read_text()
                    self.assertEqual(text.count(old), 1, "the mutant's text is not in the copy: " + old)
                    (Path(tmp) / rel).write_text(text.replace(old, new))
                done = subprocess.run([sys.executable, "-c", CHECKER, tmp], capture_output=True, text=True, timeout=60,
                                      env=dict(os.environ, PYTHONPATH=tmp + "/bin"))
            return done.returncode, done.stderr

        def test_u10_the_real_launcher_reads_are_three_gets_and_no_write_verb(self):
            kube = LIB.Kube.__new__(LIB.Kube)
            argvs = []
            kube.run = lambda args, **kw: argvs.append(list(args)) or '{"items": []}'
            kube.list_nodes(), kube.list_pods_all(), kube.list_events()
            self.assertEqual(len(argvs), 3)
            self.assertEqual(sum(1 for a in argvs if a[0] == "get"), 3, "a capacity read used a write verb: %r" % argvs)
            self.assertEqual([a[1] for a in argvs], ["nodes", "pods", "events"])
            self.assertEqual([a for a in argvs if a[0] in WRITE_VERBS], [], "a capacity read used a write verb")

        def test_u10_the_shipped_bound_is_5(self):
            self.assertEqual(self.cw.CAPACITY_SECONDS, 5, "the capacity read bound is not 5 s")

        def test_u10_the_control_passes(self):
            code, err = self.scratch_check()
            self.assertEqual((code, err), (0, ""))

        def test_u10_mutant_1_a_write_verb_in_list_events_fails_with_the_text(self):
            code, err = self.scratch_check(("cloud/launcher/cloud_worker.py", 'json.loads(self.run(["get", "events"', 'json.loads(self.run(["delete", "events"'))
            self.assertEqual(code, 1, err)
            self.assertIn("a capacity read used a write verb", err)

        def test_u10_mutant_2_a_bound_of_500_fails_with_the_text(self):
            code, err = self.scratch_check(("bin/cloud_worker.py", "CAPACITY_SECONDS = 5 ", "CAPACITY_SECONDS = 500 "))
            self.assertEqual(code, 1, err)
            self.assertIn("the capacity read bound is not 5 s", err)

        def test_u10_mutant_3_a_refused_read_raised_into_the_launch_fails_with_the_text(self):
            code, err = self.scratch_check(("bin/cloud_worker.py", "except CloudError:\n            body = None", "except CloudError:\n            raise"))
            self.assertEqual(code, 1, err)
            self.assertIn("a refused read stopped the launch", err)

        # -- U11 (PC-10): an address of the three forms is masked in the event line and in a node name; the block prints the event line, the node names are in the answer
        def u11_cluster(self, with_addresses):
            tail = (" addrs %s %s %s" % (V4, V6, MAPPED)) if with_addresses else ""
            event = dict(EVENT, message="0/13 nodes are available at 12:30:45:" + tail + " Insufficient cpu")
            names = ["n1", "n2", "n3", "n4"]
            nodes = [node(n, "8" if n == "n1" else "4") for n in names]
            if with_addresses:
                nodes.append(node("host-" + V4, "4"))
            self.set_cluster(FakeKube(nodes=nodes, events=(event,)))
            return json.loads((self.stub_dir / "capacity.json").read_text())

        def u11_count(self, block, answer, planted_in_names):
            text = block + "\n" + json.dumps(answer)
            plain = {p: text.count(p) for p in (V4, V6, "ffff:" + V4)}
            return plain, block.count("[address]"), json.dumps(answer["nodes"]).count("[address]")

        def test_u11_control_no_address_prints_none_and_keeps_the_clock_time(self):
            answer = self.u11_cluster(False)
            code, out, err = self.launch_with("--cloud")
            block = self.block(err)
            self.assertEqual(self.u11_count(block, answer, False)[1:], (0, 0), block)
            self.assertIn("12:30:45", block)
            self.assertEqual([n["name"] for n in answer["nodes"]], ["n1", "n2", "n3", "n4"])

        def test_u11_the_three_forms_are_masked_in_the_event_line_and_in_a_node_name(self):
            answer = self.u11_cluster(True)
            code, out, err = self.launch_with("--cloud")
            block = self.block(err)
            plain, in_block, in_names = self.u11_count(block, answer, True)
            self.assertEqual(plain, {V4: 0, V6: 0, "ffff:" + V4: 0}, "an address reached the block: " + block)
            self.assertEqual((in_block, in_names), (3, 1), block)
            self.assertIn("12:30:45", block)
            names = [n["name"] for n in answer["nodes"]]
            self.assertEqual(names[:4], ["n1", "n2", "n3", "n4"])
            self.assertEqual(names[4], "host-[address]")

        def test_u11_the_control_passes_and_a_mutant_masking_only_ipv4_fails_with_the_text(self):
            code, err = self.scratch_u11()
            self.assertEqual((code, err), (0, ""))
            code, err = self.scratch_u11(("cloud/launcher/cloud_worker.py", "MASK_IPV6 = True", "MASK_IPV6 = False"))
            self.assertEqual(code, 1, err)
            self.assertIn("an address reached the block", err)

        def scratch_u11(self, mutate=None):
            import shutil, subprocess, tempfile
            root = Path(__file__).resolve().parents[2]
            with tempfile.TemporaryDirectory(prefix="u11-") as tmp:
                for src in sorted((root / "cloud" / "launcher").glob("*.py")):
                    rel = src.relative_to(root)
                    (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy(src, Path(tmp) / rel)
                if mutate:
                    rel, old, new = mutate
                    text = (Path(tmp) / rel).read_text()
                    self.assertEqual(text.count(old), 1, "the mutant's text is not in the copy: " + old)
                    (Path(tmp) / rel).write_text(text.replace(old, new))
                done = subprocess.run([sys.executable, "-c", U11_CHECKER, tmp], capture_output=True, text=True, timeout=60)
            return done.returncode, done.stderr

    return TestLaunchCapacity
