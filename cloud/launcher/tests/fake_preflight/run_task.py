#!/usr/bin/env python3
"""Runs the operator driver exactly as run_task.py does, with the repository preflight replaced by a passing fake (a test hook of the
launcher library, #665). Used by test_driver.py, which drives the driver as a subprocess (or, with CW_FAKE_INPROC=1 set by it, imports this
file and runs the driver's launcher calls in the same process: an interpreter start per create and stop is most of the time of a run)."""
import contextlib
import io
import os
import sys
import threading

if __name__ == "__main__":
    sys.path.pop(0)   # this directory, which holds a file of the same name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import run_task  # noqa: E402

FAKE_PREFLIGHT = ["sh", "-c", 'echo "preflight: preflight ok: deletion, non_fast_forward, pull_request"']
run_task.cw.PREFLIGHT_ARGV = FAKE_PREFLIGHT
_FAKE_LINE = "preflight: preflight ok: deletion, non_fast_forward, pull_request"
_real_check_repo_preflight = run_task.cw.check_repo_preflight


def _fast_check_repo_preflight(repo, env):
    """The fake's one line, without starting a shell for it, while the argv is still the fake above; any other argv takes the real path."""
    if run_task.cw.PREFLIGHT_ARGV is FAKE_PREFLIGHT:
        return _FAKE_LINE
    return _real_check_repo_preflight(repo, env)


run_task.cw.check_repo_preflight = _fast_check_repo_preflight

_original_run_cloud_worker = run_task.Driver.run_cloud_worker


def _run_cloud_worker(self, args, feeds=(), write_fds=(), timeout=900):
    """The launcher in this process, when the test asked for it and no other launcher program is set; else the real child."""
    if os.environ.get("CW_FAKE_INPROC") != "1" or self.env.get("CW_LAUNCHER"):
        return _original_run_cloud_worker(self, args, feeds, write_fds, timeout)
    for r, w, value in feeds:
        threading.Thread(target=self._feed, args=(w, value), daemon=True).start()
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_task.cw.main(list(args), dict(self.kube.env))
    finally:
        for fd in [r for r, _, _ in feeds] + list(write_fds):
            try:
                os.close(fd)   # what the parent of a child does right after the spawn: here nothing else holds them
            except OSError:
                pass
    return code, out.getvalue(), err.getvalue()


_original_relay = run_task.Driver.relay


def _relay(self, text, stream=None):
    """The real relay binds sys.stderr when the module is imported: in process that is the wrong stream."""
    _original_relay(self, text, sys.stderr if stream is None else stream)


run_task.Driver.run_cloud_worker = _run_cloud_worker
run_task.Driver.relay = _relay

if __name__ == "__main__":
    sys.exit(run_task.main())
