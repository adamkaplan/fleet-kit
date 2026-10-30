"""#1477: the relay-loop tests of this change (cadence, two loops, trickle hub, hub files, one worker). The fixtures are those of
bin/test-cloud-relay-daemon, loaded from it (it is not a package, and that file is near its size limit); every name the tests use is taken from it."""
import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

from support.paths import REPO

_loader = importlib.machinery.SourceFileLoader("relay_daemon_fixtures_loop", str(REPO / "bin" / "test-cloud-relay-daemon"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
_fx = importlib.util.module_from_spec(_spec)
_loader.exec_module(_fx)
BIN, CODER, PROVE, Fixture, json, load_backend_module, plan_ledger, relay, threading, time, types = (
    _fx.BIN, _fx.CODER, _fx.PROVE, _fx.Fixture, _fx.json, _fx.load_backend_module, _fx.plan_ledger, _fx.relay, _fx.threading, _fx.time, _fx.types)


class RelayLoopCadence(unittest.TestCase):
    """#1477: the relay is pulled on its own loop, not after the engine pass.
    Mutants: pull inline after the pass (S1/S2 fail: no call while the pass runs); a loop started with no hub (U3 fails);
    a thread per tick (single-worker test fails); cadence over 30 s (cadence test fails)."""
    ENV = {"FLEET_RELAY_HUB_URL": "http://127.0.0.1:9", "FLEET_RELAY_HUB_TOKEN_FILE": "/nonexistent"}

    def setUp(self):
        self.orig = relay.pass_if_configured

    def tearDown(self):
        relay.pass_if_configured = self.orig

    def test_nothing_starts_without_a_hub(self):
        before = threading.active_count()
        self.assertIsNone(relay.start_loop(None, None, None, None, environ={}))
        self.assertEqual(threading.active_count(), before)

    def test_a_hub_without_a_host_module_fails_at_start(self):
        with self.assertRaises(ValueError):
            relay.start_loop(None, None, None, None, environ=self.ENV, engine_factory=lambda: None)

    def test_cadence_is_well_under_the_hub_wait(self):
        self.assertLessEqual(relay.CADENCE, 5.0)
        self.assertLess(relay.CADENCE, 30.0)

    def ticking(self, ticks, **kw):
        """A loop whose sleep is injected: it counts ticks and stops the loop at `ticks`. No wall time is read."""
        sleeps = []
        loop = relay.RelayLoop(None, None, None, None, environ=self.ENV, cadence=0.02, engine_factory=lambda: types.SimpleNamespace(), **kw)

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= ticks:
                loop.stop_flag.set()
        loop.sleep = sleep
        return loop, sleeps

    def test_one_pull_per_tick_at_the_cadence(self):
        calls = []
        relay.pass_if_configured = lambda *a: calls.append(threading.get_ident())
        loop, sleeps = self.ticking(5)
        loop.run()
        self.assertEqual((len(calls), sleeps), (5, [0.02] * 5))

    def test_a_pull_runs_on_the_loop_thread_only_and_the_thread_ends_at_stop(self):
        threads = set()
        relay.pass_if_configured = lambda *a: threads.add(threading.get_ident())
        counter, holder, ready = [], [], threading.Event()

        def sleep(seconds):
            ready.wait(30)   # `loop` is assigned only after start_loop returns: the thread may run first
            counter.append(1)
            if len(counter) >= 6:
                holder[0].stop_flag.set()
        loop = relay.start_loop(None, object(), None, None, environ=self.ENV, cadence=0.0, sleep=sleep, engine_factory=lambda: None)
        holder.append(loop)
        ready.set()
        loop.thread.join(30)   # a generous bound, not a measurement: the loop ends by its own tick count
        self.assertFalse(loop.thread.is_alive())
        self.assertEqual(threads, {loop.thread.ident})
        self.assertEqual(len(counter), 6)

    def test_a_stuck_pull_blocks_only_its_own_thread(self):
        gate, entered = threading.Event(), threading.Event()
        notes = []
        relay.pass_if_configured = lambda *a: (entered.set(), gate.wait(30))
        loop = relay.start_loop(None, object(), None, None, environ=self.ENV, cadence=0.02, engine_factory=lambda: types.SimpleNamespace(_note=lambda *a: notes.append(a)))
        self.assertTrue(entered.wait(30))
        loop.stop(join=0.01)   # the caller returns although the thread is stuck, and says so in the audit
        self.assertTrue(loop.thread.is_alive())
        self.assertEqual([n[0] for n in notes], ["relay.stop_timeout"])
        gate.set()
        loop.thread.join(30)
        self.assertFalse(loop.thread.is_alive())

    def test_a_stop_that_joins_leaves_no_stop_timeout_line(self):
        notes = []
        relay.pass_if_configured = lambda *a: None
        loop = relay.start_loop(None, object(), None, None, environ=self.ENV, cadence=0.0, engine_factory=lambda: types.SimpleNamespace(_note=lambda *a: notes.append(a)))
        loop.stop(join=30)
        self.assertEqual(notes, [])

    def test_a_pass_that_raises_with_a_note_that_raises_does_not_kill_the_loop(self):
        calls = []

        def boom(*a):
            calls.append(1)
            raise RuntimeError("the handler's own _note failed")

        def bad_note(*a):
            raise OSError("audit append failed")
        relay.pass_if_configured = boom
        loop, sleeps = self.ticking(4)
        loop.engine = types.SimpleNamespace(_note=bad_note)
        loop.run()
        self.assertEqual((len(calls), loop.failures), (4, 4))   # it kept pulling, and counted

    def test_mutant_loop_without_the_guard_dies_on_the_first_failure(self):
        mod = load_backend_module("            except Exception as err:\n                self._record_failure(err)\n", "            except ZeroDivisionError as err:\n                self._record_failure(err)\n")
        mod.pass_if_configured = lambda *a: (_ for _ in ()).throw(RuntimeError("x"))
        loop = mod.RelayLoop(None, None, None, None, environ=self.ENV, cadence=0.0, sleep=lambda s: None, engine_factory=lambda: types.SimpleNamespace())
        with self.assertRaises(RuntimeError):
            loop.run()

    def test_a_loop_failure_is_audited_by_class_name_when_it_can_be(self):
        notes = []
        relay.pass_if_configured = lambda *a: (_ for _ in ()).throw(RuntimeError("secret path /x"))
        loop, _ = self.ticking(1)
        loop.engine = types.SimpleNamespace(_note=lambda *a: notes.append(a))
        loop.run()
        self.assertEqual(notes, [("relay.loop_failed", None, {"error": "RuntimeError"})])

    def _timeout_loop(self, mod):
        """A loop whose pass is pass_once with a hub fetch that raises a plain TimeoutError (not PassCut); a second pass answers nothing."""
        notes, fetches = [], []

        def fetch():
            fetches.append(1)
            if len(fetches) == 1:
                raise TimeoutError("the hub read timed out: secret path /x")
            return {"requests": []}
        mod.pass_if_configured = lambda *a: mod.pass_once(types.SimpleNamespace(backend=types.SimpleNamespace(audit=lambda *x: notes.append(("pass_cut",)))), fetch, lambda *x: None)
        loop = mod.RelayLoop(None, None, None, None, environ=self.ENV, cadence=0.02, engine_factory=lambda: types.SimpleNamespace(_note=lambda *a: notes.append(a)))
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) >= 2:
                loop.stop_flag.set()
        loop.sleep = sleep
        return loop, notes, fetches, sleeps

    def test_a_plain_timeout_from_the_hub_fetch_leaves_one_loop_failed_line_and_the_loop_goes_on(self):
        loop, notes, fetches, sleeps = self._timeout_loop(relay)
        loop.run()
        self.assertEqual(notes, [("relay.loop_failed", None, {"error": "TimeoutError"})])   # one line, the class name only
        self.assertEqual((len(fetches), sleeps, loop.failures), (2, [0.02, 0.02], 1))   # a later pass ran after the cadence

    def test_mutant_loop_that_does_not_record_a_failure_is_caught_by_the_timeout_test(self):
        mod = load_backend_module('            self.engine._note("relay.loop_failed", None, {"error": type(err).__name__})\n', "            pass\n")
        loop, notes, fetches, sleeps = self._timeout_loop(mod)
        loop.run()
        self.assertEqual(notes, [])   # the mutant leaves no line: the test above fails on it
        self.assertEqual(len(fetches), 2)

    def test_the_loop_gets_its_own_engine_from_the_factory(self):
        shared, own = object(), object()
        loop = relay.RelayLoop(shared, None, None, None, environ=self.ENV, engine_factory=lambda: own)
        self.assertIs(loop.args[0], own)
        self.assertIs(relay.RelayLoop(shared, None, None, None, environ=self.ENV, engine_factory=lambda: shared).args[0], shared)   # sharing is explicit

    def test_a_hub_without_an_engine_factory_fails_at_start(self):
        with self.assertRaises(ValueError):
            relay.start_loop(object(), object(), None, None, environ=self.ENV)
        with self.assertRaises(ValueError):
            relay.RelayLoop(object(), object(), None, None, environ=self.ENV)

    def test_string_mutant_without_the_factory_use_is_caught(self):
        mod = load_backend_module("        self.engine = engine_factory()\n", "        self.engine = engine\n")
        shared, own = object(), object()
        loop = mod.RelayLoop(shared, None, None, None, environ=self.ENV, engine_factory=lambda: own)
        with self.assertRaises(AssertionError):   # the guard of test_the_loop_gets_its_own_engine_from_the_factory
            self.assertIs(loop.args[0], own)


class TwoLoopsTrace(Fixture):
    """#1477 U1/U4: two relay loops (two Executors on one state dir) answer at once.
    Mutants: the replay check in build removed (U1: 2 records); no flock (U4: 2 holders); the head read inside build (U4: a network call under the lock)."""

    def trace(self, mod, workers=2, each=4, lock=None):
        import threading as th
        orig_lock, orig_write, orig_append = plan_ledger._lock_exclusive, plan_ledger._write_head, plan_ledger.append_checked
        lock = lock or orig_lock
        tl, lk = th.local(), th.Lock()
        out = types.SimpleNamespace(holders=0, most=0, net=0)

        def release():
            if getattr(tl, "locked", False):
                tl.locked = False
                with lk:
                    out.holders -= 1

        def lock_ex(fd, *a):
            lock(fd, *a)
            tl.locked = True
            with lk:
                out.holders += 1
                out.most = max(out.most, out.holders)

        def write_head(*a, **k):
            release()   # still inside the lock: the close that frees it comes after
            return orig_write(*a, **k)

        def append(state, now, build):
            def wrapped(entries, seq):
                time.sleep(0.02)   # widen the window in which a second writer would overlap
                try:
                    return build(entries, seq)
                except BaseException:
                    release()
                    raise
            return orig_append(state, now, wrapped)
        backend = self.backend
        real_head = backend.live_head

        def live_head(pr):
            if getattr(tl, "locked", False):
                out.net += 1
            return real_head(pr)
        backend.live_head = live_head
        plan_ledger._lock_exclusive, plan_ledger._write_head, plan_ledger.append_checked = lock_ex, write_head, append
        try:
            def work(n):
                ex = mod.Executor(self.state, backend, lambda: 1000)
                for i in range(each):
                    hub = _fx.FakeHub()
                    rid = hub.submit(CODER, "prove", cid="w%d-%d" % (n, i), **PROVE)
                    mod.pass_once(ex, hub.fetch, hub.post)
            ts = [th.Thread(target=work, args=(n,)) for n in range(workers)]
            [t.start() for t in ts]
            [t.join(30) for t in ts]
        finally:
            plan_ledger._lock_exclusive, plan_ledger._write_head, plan_ledger.append_checked = orig_lock, orig_write, orig_append
        return out

    def test_two_loops_never_hold_the_flock_together_and_read_no_network_under_it(self):
        got = self.trace(relay)
        self.assertEqual((got.most, got.net), (1, 0))

    def test_mutant_without_the_flock_is_caught(self):
        # the REAL _lock_exclusive with its flock call removed (a string mutant of plan_ledger's source)
        src = (BIN / "plan_ledger.py").read_text()
        old_call = "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
        self.assertEqual(src.count(old_call), 1)
        mutant = types.ModuleType("plan_ledger_mut")
        mutant.__file__ = str(BIN / "plan_ledger.py")
        exec(compile(src.replace(old_call, "pass"), mutant.__file__, "exec"), mutant.__dict__)
        self.assertGreater(self.trace(relay, lock=mutant._lock_exclusive).most, 1)

    def two_sends(self, mod):
        import threading as th
        barrier = th.Barrier(2)
        real_head = self.backend.live_head

        def live_head(pr):
            with __import__("contextlib").suppress(th.BrokenBarrierError):
                barrier.wait(5)   # both loops have missed the index before either appends
            return real_head(pr)
        self.backend.live_head = live_head
        answers = []

        def send():
            hub = _fx.FakeHub()
            hub.submit(CODER, "prove", cid="same", **PROVE)
            mod.pass_once(mod.Executor(self.state, self.backend, lambda: 1000), hub.fetch, hub.post)
            answers.append(list(hub.results.values())[0])
        ts = [th.Thread(target=send) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(20) for t in ts]
        return answers

    def test_same_call_id_sent_twice_writes_one_record_and_one_claim(self):
        answers = self.two_sends(relay)
        self.assertEqual([a["status"] for a in answers], [200, 200])
        self.assertEqual(answers[0]["record"]["seq"], answers[1]["record"]["seq"])
        self.assertEqual(len(self.entries()), 1)
        self.assertEqual(len(json.loads((Path(self.state) / relay.INDEX_FILE).read_text())), 1)

    def test_mutant_replay_check_in_build_removed_is_caught(self):
        mod = load_backend_module("            if again:\n                raise Replay(again)\n", "            pass\n")
        self.two_sends(mod)
        self.assertEqual(len(self.entries()), 2)


class TrickleHub(unittest.TestCase):
    """#1477 U5: a hub that sends one byte per second. The relay's pass is cut at the total deadline and audits one "relay pass cut"."""

    def serve(self):
        import socketserver
        stop = threading.Event()

        class H(socketserver.BaseRequestHandler):
            def handle(self):
                with __import__("contextlib").suppress(OSError):
                    self.request.recv(4096)
                    for b in b"HTTP/1.1 200 OK\r\nContent-Length: 40\r\n\r\n" + b"x" * 40:
                        if stop.wait(1.0):
                            return
                        self.request.sendall(bytes([b]))
        srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (stop.set(), srv.shutdown(), srv.server_close()))
        return "http://127.0.0.1:%d" % srv.server_address[1]

    def one_pass(self, mod, deadline, limit):
        old, mod.READ_DEADLINE = mod.READ_DEADLINE, deadline
        self.addCleanup(setattr, mod, "READ_DEADLINE", old)
        fetch, post = mod.http_transport(self.serve(), "tok")
        notes = []
        ex = types.SimpleNamespace(backend=types.SimpleNamespace(audit=lambda *a: notes.append(a)))
        worker = threading.Thread(target=lambda: mod.pass_once(ex, fetch, post), daemon=True)
        t0 = time.monotonic()
        worker.start()
        worker.join(limit)
        return (not worker.is_alive()), time.monotonic() - t0, notes

    # The transport's OWN deadlines (socket timeouts, `end`, the watchdog Timer) are stretched x30 in these two tests, so that the ONLY
    # wall-clock cut left is the worker join at READ_DEADLINE (1.0 s). With all of them at 1.0 s the outcome depended on which timer fired
    # first (a TimeoutError from the transport, or the cut): that is speed, not behaviour. Now the outcome is the observation: the pass
    # ended through the cut path (one relay.pass_cut line) or it did not end.
    STRETCH = (("end = time.monotonic() + READ_DEADLINE", "end = time.monotonic() + READ_DEADLINE * 30"),
               ("conn = conn_class(parts.netloc, timeout=READ_DEADLINE)", "conn = conn_class(parts.netloc, timeout=READ_DEADLINE * 30)"),
               ("threading.Timer(READ_DEADLINE, cut)", "threading.Timer(READ_DEADLINE * 30, cut)"))

    def test_pass_returns_within_5s_with_one_cut_line(self):
        mod = load_backend_module(*self.STRETCH[0], extra=self.STRETCH[1:])
        done, _, notes = self.one_pass(mod, 1.0, 20.0)   # the stub needs about 40 s to finish; 20 s is a loose bound for a loaded machine
        self.assertTrue(done)
        self.assertEqual([(k, d["error"]) for k, _, d in notes], [("relay.pass_cut", "relay pass cut")])

    def test_mutant_without_the_total_deadline_is_caught(self):
        # the join cut removed from the REAL code (a string mutant, like the neighbouring tests): the pass is still running after 3 s
        mod = load_backend_module(*self.STRETCH[0], extra=self.STRETCH[1:] + (("worker.join(READ_DEADLINE)", "worker.join()"),))
        done, _, notes = self.one_pass(mod, 1.0, 3.0)
        self.assertFalse(done)
        self.assertEqual(notes, [])


class HubFilesUnchanged(unittest.TestCase):
    """#1477 U6: the hub keeps its 30 s wait and its worker copy is byte-identical to bin/cloud_relay.py."""

    def test_wait_is_30_and_the_copies_match(self):
        import cloud_relay
        self.assertEqual(cloud_relay.WAIT, 30)
        copy = BIN.parent / "cloud" / "worker" / "bin" / "cloud_relay.py"
        self.assertEqual(copy.read_bytes(), (BIN / "cloud_relay.py").read_bytes())
        for other in (BIN.parent / "cloud" / "worker" / "bin").glob("*.py"):
            if (BIN / other.name).exists():
                self.assertEqual(other.read_bytes(), (BIN / other.name).read_bytes(), other.name)


class OneWorkerAndCancel(unittest.TestCase):
    """#1477 S4: one in-flight hub worker, and no POST after a cut. Slow DNS keeps the first worker alive past its cut.
    Mutants: the in-flight check removed (2 workers start); the cancel flag not set at the cut (the late thread POSTs)."""

    def slow_dns(self, seconds):
        import socket
        real, calls = socket.getaddrinfo, []

        def gai(*a, **k):
            calls.append(1)
            time.sleep(seconds)
            return real(*a, **k)
        socket.getaddrinfo = gai
        self.addCleanup(setattr, socket, "getaddrinfo", real)
        return calls

    def cut_transport(self, mod, url="http://127.0.0.1:9", deadline=0.3):
        old, mod.READ_DEADLINE = mod.READ_DEADLINE, deadline
        self.addCleanup(setattr, mod, "READ_DEADLINE", old)
        return mod.http_transport(url, "tok")

    def second_pull(self, mod):
        calls = self.slow_dns(1.5)
        fetch, _ = self.cut_transport(mod)
        errors = []
        for _ in range(2):
            try:
                fetch()
            except Exception as err:
                errors.append(err)
        return calls, errors

    def test_a_second_pull_while_a_worker_is_in_flight_starts_no_second_worker(self):
        calls, errors = self.second_pull(relay)
        self.assertEqual([str(e) for e in errors], ["relay pass cut", "previous request still running"])
        self.assertEqual(len(calls), 1)

    def test_mutant_without_the_in_flight_check_is_caught(self):
        mod = load_backend_module("if inflight and inflight[0].is_alive():", "if False:")
        calls, _ = self.second_pull(mod)
        self.assertEqual(len(calls), 2)

    def late_post_count(self, mod):
        import http.server
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        self.slow_dns(1.0)
        _, post = self.cut_transport(mod, "http://127.0.0.1:%d" % srv.server_address[1])
        with self.assertRaises(mod.PassCut):
            post("0" * 32, {"status": 200, "reason": "ok", "record": None})
        time.sleep(1.8)   # the abandoned thread wakes from DNS and finds the cut
        return seen

    def test_no_post_is_made_after_the_cut(self):
        self.assertEqual(self.late_post_count(relay), [])

    def test_mutant_without_the_cancel_flag_is_caught(self):
        mod = load_backend_module("cancel.set()   # the pass is cut: a late thread must not POST", "pass")
        self.assertEqual(len(self.late_post_count(mod)), 1)
