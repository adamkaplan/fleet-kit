"""#956: no test of the cloud tree leaves a child process behind.

Three parts: the shared helper (cloud/worker/tests/stub_support.py) kills the process group and frees the port on pass, failure and a
setUp that raises; the stub server ends by itself (SIGTERM even when the parent ignored it, a SIGKILLed parent, an idle cap); and a scan
of the cloud tests finds every place that starts a child process by its syntax tree, with one case per site and one mutant per site
(the cleanup removed) that the scan must catch. Offline: loopback only. Children are only ever the ones this file started: never a name pattern."""
import ast
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

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


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def run_inner(case):
    """Run one TestCase class on its own and return the result (its output is not wanted)."""
    res = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    return res


GROUP_SCRIPT = "import os,subprocess,sys,time\nsubprocess.Popen(['sleep','300'])\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(300)\n"   # a child with a grandchild


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


# ---- the scan -------------------------------------------------------------------------------------------------------------------

HELPER = "stub_support"
HELPER_OK = {"start", "exec_stub"}   # these register a cleanup, or hand the process a self-exit, by themselves
# a child that a string of code starts inside another process: allowed only with this reason (the scan finds the string; a new one fails)
EMBEDDED_OK = {("bin/test-cloud-worker", "grandchild.pid"): "a bounded grandchild (sleep 60) of a stub tool; the test asserts it is gone"}


def py_files(root):
    out = []
    bases = [os.path.join(root, "cloud", d, "tests") for d in sorted(os.listdir(os.path.join(root, "cloud"))) if os.path.isdir(os.path.join(root, "cloud", d, "tests"))] if os.path.isdir(os.path.join(root, "cloud")) else []
    for base in bases:
        for dp, dn, names in os.walk(base):
            dn[:] = [d for d in dn if d not in ("fixtures", "__pycache__")]
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


def cleanup_calls(func, name):
    """The calls in the function that register or perform a cleanup for exactly this variable."""
    out = []
    for n in ast.walk(func):
        if isinstance(n, ast.Call) and call_name(n).rsplit(".", 1)[-1] in ("addCleanup", "addClassCleanup", "stop_group", "enterContext"):
            if any(mentions(a, name) for a in n.args):
                out.append(n)
    return out


def find_sites(root):
    """Every start site under root: dict(file, line, kind, func, ok, why, node, tree). The set comes from the code, never a list."""
    sites = []
    for path in py_files(root):
        tree = parse(path)
        if tree is not None:
            sites.extend(scan_tree(os.path.relpath(path, root), tree))
    sites.sort(key=lambda s: (s["file"], s["line"]))
    return sites


def scan_tree(rel, tree):
    """The sites of one parsed file (rel is its path under the root): the same rules for a real file and for a mutant."""
    sites = []
    if True:
        par = parents_of(tree)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                name = call_name(n)
                last = name.rsplit(".", 1)[-1]
                fn = enclosing_function(n, par)
                fname = fn.name if fn else "<module>"
                s = dict(file=rel, line=n.lineno, func=fname, node=n, tree=tree, par=par, name=name)
                if name == "%s.%s" % (HELPER, "start") or name == "%s.exec_stub" % HELPER:
                    sites.append(dict(s, kind="helper", ok=True, why="through the helper"))
                elif rel.endswith("stub_support.py") and fname == "spawn" and last == "Popen":
                    sites.append(dict(s, kind="helper-itself", ok=True, why="the helper's own spawn (tested by the helper cases)"))
                elif rel.endswith("stub_support.py") and fname == "exec_stub" and name == "os.execv":
                    sites.append(dict(s, kind="helper-itself", ok=True, why="the helper's own exec (the stub ends by itself)"))
                elif last == "Popen" or name == "%s.spawn" % HELPER:
                    var = None
                    p = par.get(n)
                    if isinstance(p, ast.Assign) and len(p.targets) == 1:
                        var = dotted(p.targets[0])
                    if isinstance(p, ast.withitem):
                        sites.append(dict(s, kind="with", ok=True, why="a with block waits for it"))
                    elif var and fn is not None and cleanup_calls(fn, var):
                        sites.append(dict(s, kind="popen", var=var, fn=fn, ok=True, why="registers a cleanup for %s" % var))
                    else:
                        sites.append(dict(s, kind="popen", var=var, fn=fn, ok=False, why="starts a child and registers no cleanup for it (use stub_support.start, or addCleanup for the process)"))
                elif name in ("os.execv", "os.execve", "os.execvp", "os.execvpe", "os.posix_spawn", "os.posix_spawnp") or name.startswith("os.spawn"):
                    sites.append(dict(s, kind="exec", ok=False, why="becomes or starts a long-lived process outside the helper (use stub_support.exec_stub)"))
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and rel != THIS and ("subprocess.Popen(" in n.value or "%s.spawn(" % HELPER in n.value):
                ok = "%s.stop_group(" % HELPER in n.value
                reason = None
                for (f, snip), why in EMBEDDED_OK.items():
                    if f == rel and snip in n.value:
                        ok, reason = True, why
                sites.append(dict(file=rel, line=n.lineno, func="<string>", node=n, tree=tree, par=par, name="string", kind="embedded", ok=ok,
                                  why=reason or ("the string stops its child with stop_group" if ok else "a string of code starts a child with no stop_group and no reason listed")))
    return sites


def site_name(s):
    return "%s:%d" % (s["file"], s["line"])


def mutate(tree, site):
    """A copy of the tree with the site's cleanup removed. Returns the new source or None when this kind has none to remove."""
    t = ast.parse(ast.unparse(tree))   # a private copy; the same site is found again by its place among the sites of its kind in the file
    ordinal = [x["line"] for x in SITES if x["file"] == site["file"] and x["kind"] == site["kind"]].index(site["line"])
    target = sorted([x for x in scan_tree(site["file"], t) if x["kind"] == site["kind"]], key=lambda x: x["line"])[ordinal]
    par = target["par"]
    node = target["node"]
    kind = site["kind"]
    if kind == "helper":
        if call_name(node).endswith("exec_stub"):
            new = ast.parse("os.execv(sys.executable, [sys.executable, 'stub', 'port'])").body[0].value
        else:   # start(test, argv, port=..., **kw) -> Popen(argv, **kw): no cleanup is registered
            node.args = node.args[1:]
            node.keywords = [k for k in node.keywords if k.arg != "port"]
            new = ast.Call(func=ast.parse("subprocess.Popen").body[0].value, args=node.args, keywords=node.keywords)
        _replace(par[node], node, new)
    elif kind == "popen":
        fn = target["fn"]
        for c in cleanup_calls(fn, target["var"]):
            holder = par.get(c)
            if isinstance(holder, ast.Expr):
                _remove_stmt(par[holder], holder)
            else:
                return None
    elif kind == "embedded":
        node.value = node.value.replace("%s.stop_group(" % HELPER, "p.kill(")
    else:
        return None
    ast.fix_missing_locations(t)
    return ast.unparse(t)


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


SITES = find_sites(ROOT)


class TestScanFindsTheShape(unittest.TestCase):
    """The scan on planted trees: it must tell the cleanup of THIS process from a cleanup of another call."""

    def scan(self, source):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "cloud", "worker", "tests"))
        with open(os.path.join(d, "cloud", "worker", "tests", "test_planted.py"), "w") as f:
            f.write(source)
        return find_sites(d)

    def test_a_popen_with_no_cleanup_fails(self):
        s = self.scan("import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n")
        self.assertEqual([x["ok"] for x in s], [False])

    def test_a_cleanup_for_another_process_does_not_count(self):
        s = self.scan("import subprocess\nclass T:\n    def t(self):\n        p = subprocess.Popen(['x'])\n        q = subprocess.Popen(['y'])\n        self.addCleanup(q.kill)\n")
        self.assertEqual([x["ok"] for x in s], [False, True])

    def test_a_cleanup_in_another_function_does_not_count(self):
        s = self.scan("import subprocess\nclass T:\n    def a(self):\n        p = subprocess.Popen(['x'])\n    def b(self):\n        self.addCleanup(p.kill)\n")
        self.assertEqual([x["ok"] for x in s], [False])

    def test_an_exec_outside_the_helper_fails_and_the_helper_passes(self):
        s = self.scan("import os\nos.execv('a', ['a'])\n")
        self.assertEqual([x["ok"] for x in s], [False])
        s = self.scan("import stub_support\ndef t(self):\n    stub_support.start(self, ['x'])\n")
        self.assertEqual([x["ok"] for x in s], [True])

    def test_a_string_with_a_popen_needs_a_stop_or_a_listed_reason(self):
        self.assertEqual([x["ok"] for x in self.scan("CODE = 'import subprocess\\np = subprocess.Popen([1])'\n")], [False])
        self.assertEqual([x["ok"] for x in self.scan("CODE = 'p = stub_support.spawn([1])\\nstub_support.stop_group(p)'\n")], [True])


class TestEverySite(unittest.TestCase):
    """One case per start site found in the cloud tests (generated below from SITES), and one mutant case per site."""

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
        names = {n for n in dir(TestEverySite) if n.startswith("test_site_")}
        self.assertEqual(len(names), len(SITES))
        self.assertEqual(len({n for n in dir(TestEverySite) if n.startswith("test_mutant_")}), len([s for s in SITES if s["kind"] != "helper-itself" and not (s["kind"] == "embedded" and "stop_group" not in s["node"].value)]))


def _case(s):
    def run(self):
        self.assertTrue(s["ok"], "%s %s: %s" % (site_name(s), s["func"], s["why"]))
    return run


def _mutant_case(s):
    def run(self):
        src = mutate(s["tree"], s)
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
        self.assertEqual([x["func"] for x in bad], [s["func"]] if s["kind"] != "embedded" else ["<string>"], "the scan missed the mutant of %s" % site_name(s))
    return run


for _s in SITES:
    _tag = "".join(c if c.isalnum() else "_" for c in site_name(_s))
    setattr(TestEverySite, "test_site_" + _tag, _case(_s))
    if _s["kind"] == "helper-itself" or (_s["kind"] == "embedded" and "stop_group" not in _s["node"].value):
        continue   # the helper itself is tested above; a listed embedded site has no cleanup to remove
    setattr(TestEverySite, "test_mutant_" + _tag, _mutant_case(_s))


class TestDocsStateTheRule(unittest.TestCase):
    def test_s8_the_test_docs_state_the_rule(self):
        with open(os.path.join(ROOT, "cloud", "README.md")) as f:
            text = f.read()
        self.assertIn("starts a server or another long-lived child process starts it through `cloud/worker/tests/stub_support.py`", text)


if __name__ == "__main__":
    unittest.main()
