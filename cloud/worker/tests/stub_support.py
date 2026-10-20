"""Start a stub server (or any long-lived child) from a test with a cleanup that always runs. Stdlib only.

The rule (docs: cloud/README.md, "Tests"): every test that starts a server or another long-lived child process starts it here, or registers
its own cleanup for that very process. The cleanup is registered BEFORE anything waits for the child, kills the child's whole process
group (never by name: only the group this module created, by its recorded pgid), waits, and asserts the port is free.

    port = stub_support.free_port()
    p = stub_support.start(self, [sys.executable, STUB, str(port)], port=port, env=env)   # self.addCleanup is already registered
"""
import os
import signal
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STUB = os.path.join(HERE, "stub_server.py")
PORT_FREE_SECONDS = 5.0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def port_is_free(port):
    """True only when the connect is REFUSED. A timeout (a listener that never accepts fills its backlog and the next SYN is dropped), an
    accepted connection, a reset and any other OS error all mean something may still hold the port: not free."""
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    return False


def spawn(argv, **kw):
    """Popen in a NEW session (its own process group, pgid == pid), so the whole group can be ended. No cleanup: use start() in a test."""
    kw["start_new_session"] = True
    kw["env"] = dict(kw.get("env") or os.environ, STUB_PARENT_PID=str(os.getpid()))   # the stub compares its parent to THIS, not to getppid() at its start
    p = subprocess.Popen(argv, **kw)
    p.pgid = p.pid   # recorded now: the group is ours, whatever the child does later
    return p


def group_alive(pgid):
    """True while some process still has this process group id. PermissionError (macOS, a group of only exited processes) counts as alive."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_group(p, port=None):
    """Kill the group spawn() made, wait for the child, close its pipes, and assert the port refuses connections (bounded retry).

    When the leader was already reaped and no member of the group is left, nothing is signalled: the pid could since belong to a stranger
    (a group id is reserved only while it has a member). A PermissionError from killpg after the leader has exited (macOS answers it for a
    group of only exited processes) is swallowed: there is then nothing left to kill. A port that still answers after PORT_FREE_SECONDS raises."""
    pgid = getattr(p, "pgid", None)
    p.poll()
    if pgid is not None and not (p.returncode is not None and not group_alive(pgid)):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        p.wait(timeout=10)
    finally:
        for f in (p.stdin, p.stdout, p.stderr):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
    if port is not None:
        deadline = time.time() + PORT_FREE_SECONDS
        while not port_is_free(port):
            if time.time() > deadline:
                raise AssertionError("port %s still accepts connections after the group was killed" % port)
            time.sleep(0.05)


def start(test, argv, port=None, **kw):
    """spawn() and register the cleanup on the TestCase (or the class, from setUpClass) at once, before any wait. Returns the Popen."""
    p = spawn(argv, **kw)
    (test.addClassCleanup if isinstance(test, type) else test.addCleanup)(stop_group, p, port)   # a class (setUpClass) or a test instance
    return p


def exec_stub(port):
    """For a fake tool that must BECOME the stub (fake_kubectl port-forward): the stub watches its parent and ends by itself."""
    os.environ["STUB_PARENT_PID"] = str(os.getppid())   # the parent as of now, before the stub starts
    os.execv(sys.executable, [sys.executable, STUB, str(port)])
