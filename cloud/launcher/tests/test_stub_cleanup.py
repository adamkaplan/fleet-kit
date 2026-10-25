"""#956: no test of the cloud tree leaves a child process behind.

Three parts: the shared helper (cloud/worker/tests/stub_support.py) kills the process group and frees the port on pass, failure and a
setUp that raises; the stub server ends by itself (SIGTERM even when the parent ignored it, a SIGKILLed parent, an idle cap); and a scan
of the cloud tests finds every place that starts a child process by its syntax tree, with one case per site and one mutant per site
(the cleanup removed) that the scan must catch. Offline: loopback only. Children are only ever the ones this file started: never a name pattern."""
import ast
import errno
import http.client
import io
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "cloud", "worker", "tests"))
import stub_support  # noqa: E402

STUB = stub_support.STUB
THIS = os.path.relpath(os.path.abspath(__file__), ROOT)


def wait_until(fn, seconds=8.0):
    end = time.time() + seconds
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.05)
    return fn()


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


group_alive = stub_support.group_alive   # PermissionError (macOS: a group of only exited processes) counts as alive until it is reaped


def run_inner(case):
    """Run one TestCase class on its own and return the result (its output is not wanted)."""
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return res


GROUP_SCRIPT = "import os,subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time\\ntime.sleep(300)'])\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(300)\n"   # a child with a grandchild


def make_inner(kind, state):
    """A TestCase that starts a stub and a child-with-a-grandchild through the helper, then passes, fails or raises in setUp."""
    class Inner(unittest.TestCase):
        def setUp(self):
            self.tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, self.tmp, True)
            state["port"] = stub_support.free_port()
            state["stub"] = stub_support.start(self, [sys.executable, STUB, str(state["port"])], port=state["port"], env=dict(os.environ, STUB_MODE="ok"))
            self.marker = os.path.join(self.tmp, "child")
            state["group"] = stub_support.start(self, [sys.executable, "-c", GROUP_SCRIPT, self.marker])
            wait_until(lambda: os.path.exists(self.marker) and not stub_support.port_is_free(state["port"]))
            if kind == "setup-raises":
                raise RuntimeError("planted: setUp raises after the start")

        def test_it(self):
            if kind == "fails":
                self.fail("planted failure")
    return Inner


class TestHelperCleanup(unittest.TestCase):
    def check_gone(self, state):
        self.assertTrue(wait_until(lambda: not group_alive(state["group"].pgid)), "a process of the group is left")
        self.assertTrue(wait_until(lambda: not group_alive(state["stub"].pgid)), "the stub's group is left")
        self.assertTrue(stub_support.port_is_free(state["port"]), "the port still answers")

    def test_s2_pass_failure_and_a_setup_that_raises_each_leave_no_group_and_a_free_port(self):
        for kind in ("passes", "fails", "setup-raises"):
            with self.subTest(kind=kind):
                state = {}
                res = run_inner(make_inner(kind, state))
                self.assertEqual((res.wasSuccessful(), len(res.failures), len(res.errors)), {"passes": (True, 0, 0), "fails": (False, 1, 0), "setup-raises": (False, 0, 1)}[kind])
                self.check_gone(state)

    def test_s2_a_cleanup_that_kills_only_the_child_leaves_the_grandchild_and_is_caught(self):
        """The mutant: stop_group kills the child, not the group. The grandchild survives, so check_gone would fail."""
        state = {}

        def child_only(p, port=None):
            p.kill()
            p.wait(timeout=10)
        real = stub_support.stop_group
        stub_support.stop_group = child_only
        try:
            run_inner(make_inner("passes", state))
        finally:
            stub_support.stop_group = real
        try:
            self.assertTrue(group_alive(state["group"].pgid), "the mutant should leave the grandchild alive")
        finally:
            real(state["group"])   # our own leftovers, by the recorded group
            real(state["stub"], state["port"])

    def test_s2_the_cleanup_is_registered_before_anything_waits(self):
        """start() returns after addCleanup: a test that raises right after start() (before any readiness wait) is still cleaned."""
        state = {}

        class Early(unittest.TestCase):
            def test_it(self):
                state["port"] = stub_support.free_port()
                state["p"] = stub_support.start(self, [sys.executable, STUB, str(state["port"])], port=state["port"])
                raise RuntimeError("planted: raises with no wait at all")
        res = run_inner(Early)
        self.assertEqual(len(res.errors), 1)
        self.assertTrue(wait_until(lambda: not pid_alive(state["p"].pid) or state["p"].poll() is not None))
        self.assertTrue(wait_until(lambda: stub_support.port_is_free(state["port"])))

    def test_s7_a_class_that_starts_the_stub_leaves_no_pid_and_a_free_port_when_it_has_finished(self):
        state = {}

        class Klass(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                state["port"] = stub_support.free_port()
                state["pidfile"] = tempfile.mktemp()
                cls.p = stub_support.start(cls, [sys.executable, STUB, str(state["port"])], port=state["port"], env=dict(os.environ, STUB_PID_FILE=state["pidfile"]))
                wait_until(lambda: not stub_support.port_is_free(state["port"]))

            def test_a(self):
                self.assertFalse(stub_support.port_is_free(state["port"]))

            def test_b(self):
                self.assertIsNone(self.p.poll())
        res = run_inner(Klass)
        self.assertTrue(res.wasSuccessful())
        with open(state["pidfile"]) as f:
            pid = int(f.read())
        os.unlink(state["pidfile"])
        self.assertFalse(pid_alive(pid), "the stub's recorded pid is still alive after the class")
        self.assertTrue(stub_support.port_is_free(state["port"]))


class TestStubEndsByItself(unittest.TestCase):
    def start_stub(self, env=None, **kw):
        port = stub_support.free_port()
        p = stub_support.start(self, [sys.executable, STUB, str(port)], port=port, env=dict(os.environ, **(env or {})), **kw)
        self.assertTrue(wait_until(lambda: not stub_support.port_is_free(port)), "the stub did not start")
        return p

    def test_s3_sigterm_ends_a_stub_whose_parent_ignored_it(self):
        p = self.start_stub(preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN))   # the ignore is inherited across exec
        p.send_signal(signal.SIGTERM)
        self.assertTrue(wait_until(lambda: p.poll() is not None, 5), "the stub is still running after SIGTERM")

    def test_s4_a_stub_whose_parent_is_sigkilled_ends_by_itself(self):
        mid_code = "import subprocess,sys,time\np=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]])\nprint(p.pid,flush=True)\ntime.sleep(300)\n"
        port = stub_support.free_port()
        mid = stub_support.start(self, [sys.executable, "-c", mid_code, STUB, str(port)], port=port, stdout=subprocess.PIPE, text=True)
        pid = int(mid.stdout.readline())
        self.assertTrue(wait_until(lambda: not stub_support.port_is_free(port)))
        os.kill(mid.pid, signal.SIGKILL)   # no teardown can run: only the stub's own watch can end it
        mid.wait()
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 5), "the orphaned stub is still running")

    def test_s4_an_idle_stub_with_a_live_parent_ends_after_its_cap(self):
        p = self.start_stub(env={"STUB_IDLE_SECONDS": "2"})
        self.assertTrue(wait_until(lambda: p.poll() is not None, 6), "the idle stub is still running")


class TestPortCheckOutcomes(unittest.TestCase):
    """port_is_free over every outcome of the connect: only a refusal is free."""

    def outcome(self, effect):
        with mock.patch.object(stub_support.socket, "create_connection", side_effect=effect):
            return stub_support.port_is_free(1)

    def test_refused_is_free(self):
        self.assertTrue(self.outcome(ConnectionRefusedError()))

    def test_a_timeout_is_not_free(self):
        self.assertFalse(self.outcome(socket.timeout()))

    def test_an_accepted_connection_is_not_free(self):
        self.assertFalse(self.outcome([mock.MagicMock()]))

    def test_a_reset_is_not_free(self):
        self.assertFalse(self.outcome(ConnectionResetError()))

    def test_another_os_error_is_not_free(self):
        self.assertFalse(self.outcome(OSError(errno.EHOSTUNREACH, "no route")))

    def test_a_listener_that_never_accepts_is_never_free(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        port = s.getsockname()[1]
        self.assertEqual([stub_support.port_is_free(port) for _ in range(8)], [False] * 8)   # past the backlog


class TestStubParentDiesDuringStartup(unittest.TestCase):
    def test_a_stub_whose_parent_died_before_it_started_ends_by_itself(self):
        port = stub_support.free_port()
        # The "parent" starts the stub (which names it in STUB_PARENT_PID) and is killed at once: the stub's own getppid() at its start may already be 1.
        mid_code = ("import os,subprocess,sys\n"
                    "env=dict(os.environ,STUB_PARENT_PID=str(os.getpid()))\n"
                    "p=subprocess.Popen(['sh','-c','sleep 1; exec \"$0\" \"$@\"',sys.executable,sys.argv[1],sys.argv[2]],env=env)\n"
                    "print(p.pid,flush=True)\nos._exit(0)\n")
        mid = stub_support.spawn([sys.executable, "-c", mid_code, STUB, str(port)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(stub_support.stop_group, mid, port)
        pid = int(mid.stdout.readline())
        mid.wait()
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 6), "a stub that lost its parent during startup is still running")


class TestSpawnPassesWhatTheStubNeeds(unittest.TestCase):
    """Everything stub_support.spawn passes to the child, each pinned through spawn itself: STUB_PARENT_PID, the new session, the caller's env."""

    def probe(self, **kw):
        code = "import os,json;print(json.dumps([os.environ.get('STUB_PARENT_PID'),os.getpgid(0)==os.getpid(),os.environ.get('SPAWN_PROBE'),os.getpid()]))"
        p = stub_support.spawn([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, **kw)
        self.addCleanup(stub_support.stop_group, p)
        import json
        return json.loads(p.stdout.readline())

    def test_the_child_is_told_the_starting_process_as_its_parent(self):
        self.assertEqual(self.probe()[0], str(os.getpid()))

    def test_the_child_leads_its_own_process_group(self):
        self.assertTrue(self.probe()[1])

    def test_a_given_env_is_kept_and_the_default_env_is_the_callers(self):
        self.assertEqual(self.probe(env=dict(os.environ, SPAWN_PROBE="given"))[2], "given")
        with mock.patch.dict(os.environ, {"SPAWN_PROBE": "inherited"}):
            self.assertEqual(self.probe()[2], "inherited")

    def test_a_stub_started_through_spawn_by_a_parent_that_exits_at_once_ends_by_itself(self):
        port = stub_support.free_port()
        # The parent starts the stub THROUGH spawn (a delayed exec, so the parent is gone before the stub looks) and exits: only the
        # STUB_PARENT_PID that spawn passes lets the stub see that its parent is gone (its getppid() is already 1 or a reaper).
        mid_code = ("import os,sys\nsys.path.insert(0,sys.argv[1])\nimport stub_support\n"
                    "p=stub_support.spawn(['sh','-c','sleep 1; exec \"$0\" \"$@\"',sys.executable,sys.argv[2],sys.argv[3]])\n"
                    "print(p.pid,flush=True)\nos._exit(0)\n")
        mid = stub_support.spawn([sys.executable, "-c", mid_code, os.path.dirname(STUB), STUB, str(port)], stdout=subprocess.PIPE, text=True)
        self.addCleanup(stub_support.stop_group, mid, port)
        pid = int(mid.stdout.readline())
        mid.wait()
        self.addCleanup(lambda: pid_alive(pid) and os.kill(pid, signal.SIGKILL))   # only our own recorded pid, whatever the result
        self.assertTrue(wait_until(lambda: not pid_alive(pid), 6), "a stub started through spawn whose parent exited is still running")


class TestStopGroupPins(unittest.TestCase):
    """What the helper promises, pinned one at a time: the port check, the pid-reuse guard, and the stub's idle timer."""

    def listener(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)   # never accepts: after 5 checks the backlog is full and a Linux connect times out
        self.addCleanup(s.close)
        return s, s.getsockname()[1]

    def quick_child(self):
        p = stub_support.spawn([sys.executable, "-c", "import time\ntime.sleep(300)"])
        self.addCleanup(stub_support.stop_group, p)
        return p

    def test_m3_a_port_that_still_answers_makes_stop_group_raise(self):
        s, port = self.listener()
        p = self.quick_child()
        old = stub_support.PORT_FREE_SECONDS
        stub_support.PORT_FREE_SECONDS = 0.5
        try:
            with self.assertRaises(AssertionError):
                stub_support.stop_group(p, port)
        finally:
            stub_support.PORT_FREE_SECONDS = old
        self.assertFalse(group_alive(p.pgid), "the group is killed even when the port check fails")

    def test_m3_a_port_that_frees_within_the_retry_passes(self):
        s, port = self.listener()
        p = self.quick_child()
        timer = threading.Timer(0.5, s.close)
        timer.start()
        self.addCleanup(timer.cancel)
        old = stub_support.PORT_FREE_SECONDS
        stub_support.PORT_FREE_SECONDS = 5
        try:
            stub_support.stop_group(p, port)   # raises when the port never frees
        finally:
            stub_support.PORT_FREE_SECONDS = old

    def test_low_nothing_is_signalled_when_the_leader_was_reaped_and_the_group_is_empty(self):
        p = stub_support.spawn([sys.executable, "-c", "pass"])
        self.addCleanup(stub_support.stop_group, p)
        p.wait()
        self.assertTrue(wait_until(lambda: not group_alive(p.pgid)))
        calls = []
        real = os.killpg

        def spy(pgid, sig):
            calls.append((pgid, sig))
            return real(pgid, sig)
        with mock.patch.object(os, "killpg", spy):
            stub_support.stop_group(p)
        self.assertNotIn((p.pgid, signal.SIGKILL), calls)

    def test_low_a_group_member_that_outlived_a_reaped_leader_is_still_killed(self):
        code = "import subprocess,sys\nsubprocess.Popen([sys.executable,'-c','import time\\ntime.sleep(300)'])\n"
        p = stub_support.spawn([sys.executable, "-c", code])
        self.addCleanup(stub_support.stop_group, p)
        p.wait()   # the leader is gone and reaped; its grandchild still holds the group
        self.assertTrue(group_alive(p.pgid))
        stub_support.stop_group(p)
        self.assertTrue(wait_until(lambda: not group_alive(p.pgid)), "the member of the group is left")

    def test_low_the_idle_timer_is_refreshed_by_requests(self):
        port = stub_support.free_port()
        p = stub_support.start(self, [sys.executable, STUB, str(port)], port=port, env=dict(os.environ, STUB_IDLE_SECONDS="2"))
        self.assertTrue(wait_until(lambda: not stub_support.port_is_free(port)))
        end = time.time() + 5   # longer than the 2 s cap, with a request every half second
        while time.time() < end:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/api/info")
            c.getresponse().read()
            c.close()
            self.assertIsNone(p.poll(), "the stub ended while it was being asked")
            time.sleep(0.5)
        self.assertTrue(wait_until(lambda: p.poll() is not None, 6), "the stub did not end once the requests stopped")


# ---- the scan -------------------------------------------------------------------------------------------------------------------

HELPER = "stub_support"
# ONE table of every way a test starts a child or another thread of control. Names are canonical (imports and aliases resolved).
STARTERS = {
    "popen": {"subprocess.Popen"},
    "blocking": {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput"},
    "shell": {"os.system", "os.popen"},
    "fork": {"os.fork", "os.forkpty", "pty.fork", "pty.spawn"},
    "exec": {"os.execl", "os.execle", "os.execlp", "os.execlpe", "os.execv", "os.execve", "os.execvp", "os.execvpe", "os.posix_spawn", "os.posix_spawnp",
             "os.spawnl", "os.spawnle", "os.spawnlp", "os.spawnlpe", "os.spawnv", "os.spawnve", "os.spawnvp", "os.spawnvpe"},
    "process": {"multiprocessing.Process", "multiprocessing.Pool", "multiprocessing.get_context", "concurrent.futures.ProcessPoolExecutor"},
    "async": {"asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell"},
    "thread": {"threading.Thread"},   # fine as a daemon (it dies with the test process); a non-daemon thread is a site that needs a cleanup
}
KIND_OF = {name: kind for kind, names in STARTERS.items() for name in names}
CLEANUP_REGISTERS = {"addCleanup", "addClassCleanup", "enterContext"}
ENDERS = {"kill", "terminate", "send_signal", "stop_group", "killpg"}   # what ENDS a process: wait or close alone does not
BACKGROUND = re.compile(r"(?<![&])&\s*($|[;\n)])|\bnohup\b|\bsetsid\b|\bdisown\b")   # a shell string that backgrounds something
SERVER_WORDS = re.compile(r"stub_server|serve_forever|http\.server")   # a blocking call whose argument is a server never returns
# a child that a string of code starts inside another process: allowed only with this reason (the scan finds the string; a new one fails)
EMBEDDED_OK = {("bin/test-cloud-worker", "grandchild.pid"): "a bounded grandchild (sleep 60) of a stub tool; the test asserts it is gone",
               ("cloud/worker/tests/fixtures/prepush_spawner.py", "sleep"): "a bounded grandchild (sleep 77) of a fixture script; the hardening test asserts the check's process-group kill ends it"}


def py_files(root):
    out = []
    bases = [os.path.join(root, "cloud", d, "tests") for d in sorted(os.listdir(os.path.join(root, "cloud"))) if os.path.isdir(os.path.join(root, "cloud", d, "tests"))] if os.path.isdir(os.path.join(root, "cloud")) else []
    for base in bases:
        for dp, dn, names in os.walk(base):
            dn[:] = [d for d in dn if d != "__pycache__"]   # fixtures are scanned too: a script a test writes out and runs starts a child as much as a test does
            for n in sorted(names):
                out.append(os.path.join(dp, n))
    cw = os.path.join(root, "bin", "test-cloud-worker")
    if os.path.exists(cw):
        out.append(cw)
    return out


def parse(path):
    try:
        with open(path) as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    if not (path.endswith(".py") or text.startswith("#!") and "python" in text.split("\n", 1)[0] or os.path.basename(path).startswith("test-")):
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return None
    return ".".join(reversed(parts))


def aliases_of(tree):
    """name -> canonical dotted name, from every import (import a.b as x; from m import n as k)."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module:
            for a in n.names:
                out[a.asname or a.name] = n.module + "." + a.name
    return out


def star_imports(tree):
    """'from m import *' cannot be resolved by name: the limit, counted so it is reported and never silent."""
    return [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)]


def resolve(node, aliases):
    """The canonical dotted name of a callee: through import aliases and through getattr(module, 'name')."""
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        base = resolve(node.value, aliases)
        return base + "." + node.attr if base else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2 \
            and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
        base = resolve(node.args[0], aliases)
        return base + "." + node.args[1].value if base else None
    return None


def call_name(call):
    return dotted(call.func) or ""


def parents_of(tree):
    par = {}
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            par[c] = n
    return par


def enclosing_function(node, par):
    while node in par:
        node = par[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def mentions(node, name):
    for n in ast.walk(node):
        if isinstance(n, (ast.Name, ast.Attribute)) and dotted(n) == name:
            return True
    return False


def ends_var(args, var):
    """Do these call arguments end the process held in var (var.kill, a lambda that kills it, stop_group(var...))? wait or close alone does not."""
    for a in args:
        for sub in ast.walk(a):
            if isinstance(sub, ast.Attribute) and sub.attr in ENDERS and dotted(sub.value) == var:
                return True
            if (isinstance(sub, ast.Name) and sub.id in ENDERS or isinstance(sub, ast.Attribute) and sub.attr in ENDERS) and any(mentions(b, var) for b in args):
                return True
    return False


def cleanup_calls(func, name):
    """The calls in the function that register (or in a finally, perform) a cleanup that ENDS exactly this variable's process."""
    out = []
    finals = {id(x) for t in ast.walk(func) if isinstance(t, ast.Try) for s in t.finalbody for x in ast.walk(s)}
    for n in ast.walk(func):
        if not isinstance(n, ast.Call):
            continue
        last = call_name(n).rsplit(".", 1)[-1]
        if last in CLEANUP_REGISTERS and ends_var(n.args, name):
            out.append(n)
        elif id(n) in finals and (last == "stop_group" and any(mentions(a, name) for a in n.args) or last in ENDERS and call_name(n) == name + "." + last):
            out.append(n)
    return out


def scan_tree(rel, tree):
    """The sites of one parsed file (rel is its path under the root): the same rules for a real file and for a mutant."""
    sites = []
    par = parents_of(tree)
    aliases = aliases_of(tree)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            canon = resolve(n.func, aliases) or ""
            fn = enclosing_function(n, par)
            fname = fn.name if fn else "<module>"
            s = dict(file=rel, line=n.lineno, func=fname, node=n, tree=tree, par=par, name=canon)
            if canon in (HELPER + ".start", HELPER + ".exec_stub"):
                sites.append(dict(s, kind="helper", ok=True, why="through the helper"))
                continue
            if rel.endswith("stub_support.py") and (fname == "spawn" and canon == "subprocess.Popen" or fname == "exec_stub" and canon == "os.execv"):
                sites.append(dict(s, kind="helper-itself", ok=True, why="the helper's own start (tested by the helper cases)"))
                continue
            kind = KIND_OF.get(canon) or ("exec" if canon.startswith("os.spawn") else None)
            if canon == HELPER + ".spawn":
                kind = "popen"
            if kind is None:
                continue
            if kind == "blocking":
                strings = [c.value for a in list(n.args) + [k.value for k in n.keywords] for c in ast.walk(a) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
                if any(BACKGROUND.search(x) or SERVER_WORDS.search(x) for x in strings):
                    sites.append(dict(s, kind="blocking", ok=False, why="a blocking call that backgrounds a process or runs a server (it leaves a child or never returns)"))
                continue   # a plain blocking call waits for its child inline: not a start site
            if kind == "thread":
                daemon = any(k.arg == "daemon" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords)
                sites.append(dict(s, kind="thread", ok=daemon, why="a daemon thread dies with the test process" if daemon else "a non-daemon thread outlives the test (daemon=True, or end it in a cleanup)"))
                continue
            var = None
            p = par.get(n)
            if isinstance(p, ast.Assign) and len(p.targets) == 1:
                var = dotted(p.targets[0])
            if kind in ("popen", "process") and var and fn is not None and cleanup_calls(fn, var):
                sites.append(dict(s, kind=kind, var=var, fn=fn, ok=True, why="registers a cleanup that ends %s" % var))
            else:
                why = "starts a child (%s) with no cleanup that ENDS it (kill, terminate, stop_group; a wait alone does not): use stub_support.start" % canon
                listed = [r for (f, snip), r in EMBEDDED_OK.items() if f == rel and "/fixtures/" in "/" + rel and snip in ast.unparse(n)]   # a script a test runs, with its reason
                sites.append(dict(s, kind=kind, var=var, fn=fn, ok=bool(listed), listed=bool(listed), why=listed[0] if listed else why))
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and rel != THIS:
            text = n.value
            starts, stops = string_starts(text)
            if not (starts or BACKGROUND.search(text)):
                continue
            ok = starts and stops
            reason = None
            for (f, snip), why in EMBEDDED_OK.items():
                if f == rel and snip in text:
                    ok, reason = True, why
            sites.append(dict(file=rel, line=n.lineno, func="<string>", node=n, tree=tree, par=par, name="string", kind="embedded", ok=ok,
                              why=reason or ("the string stops its child with stop_group" if ok else "a string of code starts a child or backgrounds a process with no stop_group and no reason listed")))
    return sites


def string_starts(text):
    """(starts, stops) of a string a test hands to another process as code. A string that parses as Python is read like code, through its own import aliases: it
    starts a child when a call resolves to a starter and stops it when a call resolves to stub_support.stop_group. A string that does not parse (a shell script, or
    code with a placeholder) falls back to the word match."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return "subprocess.Popen(" in text or "%s.spawn(" % HELPER in text, "%s.stop_group(" % HELPER in text
    aliases = aliases_of(tree)
    names = {resolve(c.func, aliases) for c in ast.walk(tree) if isinstance(c, ast.Call)}
    return bool(names & {"subprocess.Popen", HELPER + ".spawn"}), HELPER + ".stop_group" in names


def find_sites(root):
    """Every start site under root, from the syntax trees. The set comes from the code, never a list."""
    sites = []
    for path in py_files(root):
        tree = parse(path)
        if tree is not None:
            sites.extend(scan_tree(os.path.relpath(path, root), tree))
    sites.sort(key=lambda s: (s["file"], s["line"]))
    return sites


def site_name(s):
    return "%s:%d" % (s["file"], s["line"])


def _replace(parent, old, new):
    for field, value in ast.iter_fields(parent):
        if value is old:
            setattr(parent, field, new)
            return
        if isinstance(value, list) and old in value:
            value[value.index(old)] = new
            return
    raise AssertionError("node not found in its parent")


def _remove_stmt(parent, stmt):
    for field, value in ast.iter_fields(parent):
        if isinstance(value, list) and stmt in value:
            value.remove(stmt)
            if not value:
                value.append(ast.Pass())
            return
    raise AssertionError("statement not found")


def mutate(tree, site, how="remove"):
    """The source of a copy of the tree with this site's cleanup removed (how=remove), or reduced to a wait (how=wait). None: no such mutant."""
    alias, how = how == "alias", ("remove" if how == "alias" else how)   # alias: the remove mutant, with the started call renamed through an alias
    if alias and site["kind"] not in ("popen", "process", "thread", "helper"):
        return None
    t = ast.parse(ast.unparse(tree))   # a private copy; the same site is found again by its place among the sites of its kind in the file
    ordinal = [x["line"] for x in SITES if x["file"] == site["file"] and x["kind"] == site["kind"]].index(site["line"])
    target = sorted([x for x in scan_tree(site["file"], t) if x["kind"] == site["kind"]], key=lambda x: x["line"])[ordinal]
    par, node, kind = target["par"], target["node"], site["kind"]
    if kind == "helper" and how == "remove":
        if call_name(node).endswith("exec_stub"):
            new = ast.parse("os.execv(sys.executable, [sys.executable, 'stub', 'port'])").body[0].value
        else:   # start(test, argv, port=..., **kw) -> Popen(argv, **kw): no cleanup is registered
            new = ast.Call(func=ast.parse("subprocess.Popen").body[0].value, args=node.args[1:], keywords=[k for k in node.keywords if k.arg != "port"])
        _replace(par[node], node, new)
        if alias and not _alias_call(t, new, "os.execv" if call_name(node).endswith("exec_stub") else "subprocess.Popen"):
            return None
    elif kind == "thread" and how == "remove":
        node.keywords = [k for k in node.keywords if k.arg != "daemon"]
        if alias and not _alias_call(t, node, target["name"]):
            return None
    elif kind in ("popen", "process"):
        var = target["var"]
        calls = cleanup_calls(target["fn"], var)
        if not calls:
            return None
        for c in calls:
            if how == "remove":
                holder = par.get(c)
                if not isinstance(holder, ast.Expr):
                    return None
                _remove_stmt(par[holder], holder)
            else:   # only wait for it
                for sub in ast.walk(c):
                    if isinstance(sub, ast.Attribute) and sub.attr in ENDERS and dotted(sub.value) == var:
                        sub.attr = "wait"
                if cleanup_calls_in(c, var):   # a stop_group(var, ...) style call
                    wait = ast.Attribute(value=ast.parse(var, mode="eval").body, attr="wait", ctx=ast.Load())
                    if call_name(c).rsplit(".", 1)[-1] in CLEANUP_REGISTERS:
                        c.args, c.keywords = [wait], []
                    else:
                        c.func, c.args, c.keywords = wait, [], []
        if alias and not _alias_call(t, node, target["name"]):
            return None
    elif kind == "embedded":
        node.value = node.value.replace("%s.stop_group(" % HELPER, "print(" if how == "remove" else "p.wait(")
    else:
        return None
    ast.fix_missing_locations(t)
    return ast.unparse(t)


ALIAS = "_alias_start"


def _alias_call(t, node, canon):
    """Rename the started call (node, in tree t) through 'from <module> import <name> as _alias_start'. False: it cannot be aliased."""
    if "." not in canon or not isinstance(node.func, (ast.Name, ast.Attribute)):
        return False
    module, name = canon.rsplit(".", 1)
    node.func = ast.Name(id=ALIAS, ctx=ast.Load())
    at = 0
    while at < len(t.body) and (isinstance(t.body[at], ast.ImportFrom) and t.body[at].module == "__future__"
                                or at == 0 and isinstance(t.body[at], ast.Expr) and isinstance(t.body[at].value, ast.Constant) and isinstance(t.body[at].value.value, str)):
        at += 1
    t.body.insert(at, ast.parse("from %s import %s as %s" % (module, name, ALIAS)).body[0])
    return True


def cleanup_calls_in(call, var):
    """Is this one call a stop_group-style ender (a function named in ENDERS taking var), after the attribute forms were turned into waits?"""
    for sub in ast.walk(call):
        if (isinstance(sub, ast.Name) and sub.id in ENDERS or isinstance(sub, ast.Attribute) and sub.attr in ENDERS) and any(mentions(b, var) for b in [call]):
            return True
    return False


SITES = find_sites(ROOT)


def mutable(s, how):
    if s["kind"] == "helper-itself" or s["kind"] == "blocking" or s.get("listed"):   # a listed script has no cleanup to take away
        return False
    if s["kind"] == "embedded":
        return "stop_group" in s["node"].value and how != "alias"   # a string of code has no call to rename
    if s["kind"] == "thread":
        return how in ("remove", "alias")
    if s["kind"] == "helper":
        return how in ("remove", "alias")
    if how == "alias":
        return s["kind"] in ("popen", "process")
    return s["kind"] in ("popen", "process")


PLANTED = {   # every way a test can start a child that the scan must NOT let through (each is a case): name -> source
    "an aliased import of Popen": "from subprocess import Popen as P\ndef t():\n    P(['x'])\n",
    "a module alias": "import subprocess as sp\ndef t():\n    sp.Popen(['x'])\n",
    "getattr naming Popen": "import subprocess\ndef t():\n    getattr(subprocess, 'Popen')(['x'])\n",
    "a shell run that backgrounds": "import subprocess\ndef t():\n    subprocess.run('nohup sleep 300 &', shell=True)\n",
    "os.system": "import os\ndef t():\n    os.system('sleep 300 &')\n",
    "os.popen": "import os\ndef t():\n    os.popen('sleep 300')\n",
    "multiprocessing.Process": "import multiprocessing\ndef t():\n    multiprocessing.Process(target=print).start()\n",
    "os.fork": "import os\ndef t():\n    os.fork()\n",
    "pty.fork": "import pty\ndef t():\n    pty.fork()\n",
    "asyncio.create_subprocess_exec": "import asyncio\nasync def t():\n    await asyncio.create_subprocess_exec('x')\n",
    "a string with a shell background": "CODE = 'sleep 300 &\\n'\n",
    "a string with nohup": "CODE = 'nohup sleep 300'\n",
    "a blocking check_call of a server": "import subprocess\ndef t():\n    subprocess.check_call(['python3', 'stub_server.py', '1'])\n",
    "a non-daemon thread": "import threading\ndef t():\n    threading.Thread(target=print).start()\n",
    "an exec outside the helper": "import os\nos.execv('a', ['a'])\n",
    "a process that is only waited for": "import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n        self.addCleanup(p.wait)\n",
    "a process that is only closed": "import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n        self.addCleanup(p.stdout.close)\n",
    "a cleanup for another process": "import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n        q = subprocess.Popen(['y'])\n        self.addCleanup(q.kill)\n",
    "a cleanup in another function": "import subprocess\nclass T:\n    def a(self):\n        p = subprocess.Popen(['x'])\n    def b(self):\n        self.addCleanup(p.kill)\n",
    "a popen with no cleanup": "import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n",
}
CONTROLS = {   # shapes that are fine
    "a process killed by its cleanup": "import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n        self.addCleanup(p.kill)\n",
    "a process ended by a lambda": "import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n        self.addCleanup(lambda: p.terminate())\n",
    "a process ended in a finally": "import subprocess\ndef t():\n    p = subprocess.Popen(['x'])\n    try:\n        pass\n    finally:\n        p.kill()\n",
    "the helper": "import stub_support\ndef t(self):\n    stub_support.start(self, ['x'])\n",
    "a blocking run with a timeout": "import subprocess\ndef t():\n    subprocess.run(['x'], timeout=5)\n",
    "a daemon thread": "import threading\ndef t():\n    threading.Thread(target=print, daemon=True).start()\n",
    "a string with a stop": "CODE = 'p = stub_support.spawn([1])\\nstub_support.stop_group(p)'\n",
}


class TestScanFindsTheShape(unittest.TestCase):
    """The scan on planted trees: every way to start a child outside the helper fails, the fine shapes pass (one case each)."""

    def scan(self, source):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "cloud", "worker", "tests"))
        with open(os.path.join(d, "cloud", "worker", "tests", "test_planted.py"), "w") as f:
            f.write(source)
        return find_sites(d)


def _planted(name, source):
    def run(self):
        sites = self.scan(source)
        self.assertTrue(sites, "no site found: %s passes the scan unseen" % name)
        self.assertTrue(any(not x["ok"] for x in sites), "%s is accepted" % name)
    return run


def _control(name, source):
    def run(self):
        sites = self.scan(source)
        self.assertTrue(all(x["ok"] for x in sites), "%s is refused: %s" % (name, [x["why"] for x in sites if not x["ok"]]))
    return run


def _tagname(text):
    return "".join(c if c.isalnum() else "_" for c in text)


for _n, _src in PLANTED.items():
    setattr(TestScanFindsTheShape, "test_planted_" + _tagname(_n), _planted(_n, _src))
for _n, _src in CONTROLS.items():
    setattr(TestScanFindsTheShape, "test_control_" + _tagname(_n), _control(_n, _src))


class TestEverySite(unittest.TestCase):
    """One case per start site found in the cloud tests (generated below from SITES), and one mutant case per site and way."""

    def test_the_scan_finds_the_expected_families(self):
        kinds = {s["kind"] for s in SITES}
        self.assertGreaterEqual(len(SITES), 5, [site_name(s) for s in SITES])
        for k in ("helper", "popen", "embedded", "helper-itself"):
            self.assertIn(k, kinds)
        files = {s["file"] for s in SITES}
        for f in ("cloud/launcher/tests/test_driver.py", "cloud/launcher/tests/fake_kubectl", "bin/test-cloud-worker", "cloud/worker/tests/test_activity.py",
                  "cloud/worker/tests/test_entrypoint.py", "cloud/launcher/tests/test_reaper_managed.py"):
            self.assertIn(f, files)

    def test_the_cases_are_exactly_the_sites(self):
        self.assertEqual(len({n for n in dir(TestEverySite) if n.startswith("test_site_")}), len(SITES))
        want = sum(1 for s in SITES if mutable(s, "remove"))
        self.assertEqual(len({n for n in dir(TestEverySite) if n.startswith("test_mutant_remove_")}), want)
        self.assertEqual(len({n for n in dir(TestEverySite) if n.startswith("test_mutant_wait_")}), sum(1 for s in SITES if mutable(s, "wait")))
        self.assertEqual(len({n for n in dir(TestEverySite) if n.startswith("test_mutant_alias_")}), sum(1 for s in SITES if mutable(s, "alias")))

    def test_an_aliased_start_is_found_as_a_site(self):
        for src in ("from subprocess import Popen as start\ndef t():\n    start(['x'])\n", "import subprocess as sp\ndef t():\n    sp.Popen(['x'])\n",
                    "from subprocess import Popen as P\ndef t():\n    P(['x'])\n", "import subprocess\ndef t():\n    subprocess.Popen(['x'])\n"):
            sites = scan_tree("cloud/worker/tests/test_x.py", ast.parse(src))
            self.assertEqual([x["kind"] for x in sites], ["popen"], src)
        self.assertEqual(scan_tree("cloud/worker/tests/test_x.py", ast.parse("from subprocess import Popen\n")), [])

    def test_a_string_of_code_is_read_as_code_through_its_own_aliases(self):
        embedded = lambda text: [x for x in scan_tree("cloud/worker/tests/test_x.py", ast.parse("CODE = %r\n" % text)) if x["kind"] == "embedded"]
        spawn = "from subprocess import Popen as go\ngo(['sleep', '1'])\n"
        self.assertEqual([(x["ok"]) for x in embedded(spawn)], [False], "an aliased spawn inside a string is a failing embedded site")
        self.assertEqual([x["ok"] for x in embedded("import subprocess as sp\nsp.Popen(['x'])\n")], [False])
        stopped = "import stub_support as ss\nfrom stub_support import stop_group as sg\np = ss.spawn(['x'])\nsg(p)\n"
        self.assertEqual([x["ok"] for x in embedded(stopped)], [True], "an aliased stop_group in the string is a cleanup")
        self.assertEqual(embedded("from subprocess import Popen as go\nprint('go(1)')\n"), [], "an import and a call that starts nothing: 0 sites")
        self.assertEqual(embedded("print('subprocess.Popen(')\n"), [], "the words in a printed string are not a spawn when the string parses as code")
        self.assertEqual([x["ok"] for x in embedded("subprocess.Popen(['x'\n")], [False], "a string that does not parse falls back to the word match")
        self.assertEqual(embedded("plain text with no code"), [])

    def test_a_script_under_a_fixtures_directory_is_a_counted_site_and_must_be_explained(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "cloud", "worker", "tests", "fixtures"))
        path = os.path.join(root, "cloud", "worker", "tests", "fixtures", "spawner.py")
        with open(path, "w") as fh:
            fh.write("import subprocess\np = subprocess.Popen(['sleep', '5'])\n")
        found = find_sites(root)
        self.assertEqual([(x["file"], x["kind"], x["ok"]) for x in found], [("cloud/worker/tests/fixtures/spawner.py", "popen", False)])
        with mock.patch.dict(EMBEDDED_OK, {("cloud/worker/tests/fixtures/spawner.py", "sleep"): "a bounded child"}):
            self.assertEqual([x["ok"] for x in find_sites(root)], [True], "named in EMBEDDED_OK with a reason, it passes")

    def test_every_script_under_the_real_fixtures_directories_is_counted_and_explained(self):
        fixtures = [s for s in SITES if "/fixtures/" in s["file"]]
        self.assertTrue(fixtures, "the fixture scripts are no longer scanned")
        self.assertEqual([site_name(s) for s in fixtures if not s["ok"]], [], "a fixture starts a child with no cleanup and no reason in EMBEDDED_OK")
        for key in EMBEDDED_OK:
            self.assertTrue(os.path.exists(os.path.join(ROOT, key[0])), "EMBEDDED_OK names a file that is gone: %s" % (key,))

    def test_a_star_import_is_an_unresolved_limit_and_is_counted(self):
        tree = ast.parse("from subprocess import *\ndef t():\n    Popen(['x'])\n")
        self.assertEqual(len(star_imports(tree)), 1)
        self.assertEqual(scan_tree("cloud/worker/tests/test_x.py", tree), [])   # not found: the documented limit
        self.assertEqual(sum(len(star_imports(t)) for t in (parse(p) for p in py_files(ROOT)) if t is not None), 0, "a real test uses a star import: the scan cannot see its starts")


def _case(s):
    def run(self):
        self.assertTrue(s["ok"], "%s %s: %s" % (site_name(s), s["func"], s["why"]))
    return run


def _mutant_case(s, how):
    def run(self):
        src = mutate(s["tree"], s, how)
        self.assertIsNotNone(src, "no mutant for %s" % site_name(s))
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        dest = os.path.join(d, s["file"])
        os.makedirs(os.path.dirname(dest))
        with open(os.path.join(ROOT, s["file"])) as f:
            first = f.readline()
        with open(dest, "w") as f:
            f.write((first if first.startswith("#!") else "") + src)   # unparse drops the shebang that tells the scan it is Python
        bad = [x for x in find_sites(d) if not x["ok"]]
        self.assertEqual(sorted({x["func"] for x in bad}), [s["func"] if s["kind"] != "embedded" else "<string>"], "the scan missed the %s mutant of %s" % (how, site_name(s)))
    return run


for _s in SITES:
    _tag = _tagname(site_name(_s))
    setattr(TestEverySite, "test_site_" + _tag, _case(_s))
    for _how in ("remove", "wait", "alias"):
        if mutable(_s, _how):
            setattr(TestEverySite, "test_mutant_%s_%s" % (_how, _tag), _mutant_case(_s, _how))


class TestDocsStateTheRule(unittest.TestCase):
    def test_s8_the_test_docs_state_the_rule(self):
        with open(os.path.join(ROOT, "cloud", "README.md")) as f:
            text = f.read()
        self.assertIn("starts a server or another long-lived child process starts it through `cloud/worker/tests/stub_support.py`", text)


if __name__ == "__main__":
    unittest.main()
