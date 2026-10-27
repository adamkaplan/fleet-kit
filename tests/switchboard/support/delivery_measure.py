#!/usr/bin/env python3
"""Measure which delivery to an idle OpenCode v2 session starts a turn (ask #1345; docs/switchboard.md, "Which delivery starts a turn").

    python3 tests/switchboard/support/delivery_measure.py [--out FILE] [--wait SECONDS]

The helper builds a THROWAWAY OpenCode profile (its own HOME and XDG directories under one temporary directory, its own service on its own
loopback port, the repository's scripted model as the only provider, so no credential and no real model) and drives the daemon's own
`V2Client` against it. It never reads or writes the real profile, never talks to the real service and never touches a fleet agent's session.
Each measured cell gets a session of its own, titled `fleet-kit-delivery-measurement-<nonce>-<n>` so it cannot be mistaken for an agent;
the session is removed through the service's own `session.remove` whatever happens, and at the end the list of sessions is read and must
hold none of that name (the run fails, and says so, when one is left).

The cases are three states of a session; the forms are the five ways one message can be delivered to it:

    cases:  idle after a normal turn | idle after a turn that ended in a provider error | working
    forms:  queue with resume | queue without | steer with resume | steer without | a plain prompt

For each cell the answer is `turn_started` (a model request carrying the message's marker arrived within `--wait` seconds of the delivery:
the message reached the model, in a turn it started when the session was idle, or in the running turn's own next step when it was working) and
`ms` (milliseconds from the delivery call's return to that request, null when none came). The result is JSON: the measured v2 version, the
cases, the forms and a row per cell. Standard library only.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))

CASES = ("idle_after_normal_turn", "idle_after_provider_error", "working")
FORMS = ("queue_resume", "queue", "steer_resume", "steer", "prompt")
FORM_ARGS = {   # form -> (delivery, resume); a plain prompt carries neither
    "queue_resume": ("queue", True), "queue": ("queue", False),
    "steer_resume": ("steer", True), "steer": ("steer", False),
    "prompt": (None, None),
}
TITLE_PREFIX = "fleet-kit-delivery-measurement-"
CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_ACCESS_KEY", "_ACCESS_KEY_ID")
WORK_SECONDS = 14     # how long the working case's turn runs (a scripted sleep before its tool call)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def clean_env(root):
    """The environment of every process the helper starts: its own HOME and XDG directories, no ambient credential, no herdr pane identity."""
    env = {k: v for k, v in os.environ.items()
           if not k.endswith(CREDENTIAL_SUFFIXES) and not k.startswith(("GH_", "GITHUB_", "COPILOT_", "HERDR_", "OPENCODE_"))}
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state"),
                      ("XDG_CACHE_HOME", "cache"), ("TMPDIR", "tmp")):
        env[name] = str(root / sub)
        (root / sub).mkdir(parents=True, exist_ok=True)
    env["FLEET_SWITCHBOARD_ALLOW_REAL_V2"] = "1"   # the one test that means to run the real binary, inside its own profile
    return env


def load_proof():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader("proof_switchboard_measure", str(REPO / "bin" / "proof-switchboard"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def message_text(marker, script):
    return "delivery measurement %s @script %s" % (marker, script)


class Measurement:
    def __init__(self, binary, wait):
        self.binary = binary
        self.wait = wait
        self.nonce = uuid.uuid4().hex[:8]
        self.root = Path(tempfile.mkdtemp(prefix="fleet-kit-delivery-measure-"))
        self.env = clean_env(self.root)
        self.model = None
        self.model_port = free_port()
        self.service_port = free_port()
        self.log = self.root / "model.jsonl"
        self.client = None
        self.sb = None
        self.created = []
        self.removed = []
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.agent = None

    # -- the throwaway profile
    def run(self, *args, timeout=60):
        return subprocess.run([self.binary] + list(args), env=self.env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)

    def start(self):
        proof = load_proof()
        config = Path(self.env["XDG_CONFIG_HOME"]) / "opencode"
        config.mkdir(parents=True, exist_ok=True)
        (config / "opencode.json").write_text(json.dumps(proof.provider_config(self.model_port), indent=2) + "\n")
        self.model = subprocess.Popen([sys.executable, str(REPO / "bin" / "proof-switchboard"), "scripted-model", "--port", str(self.model_port),
                                       "--log", str(self.log)], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: self._port_open(self.model_port), 15, "the scripted model")
        subprocess.run(["git", "init", "-q", str(self.workdir)], check=True, env=self.env)
        done = self.run("service", "set", "port", str(self.service_port))
        if done.returncode:
            raise RuntimeError("could not set the throwaway service's port: %s" % done.stderr.strip()[:200])
        done = self.run("service", "start", timeout=120)
        if done.returncode:
            raise RuntimeError("could not start the throwaway service: %s" % (done.stderr or done.stdout).strip()[:200])
        from support.loader import sb
        self.sb = sb
        self.client = sb.V2Client(self.binary, self.env, subprocess.run, 60)
        self.client.breaker.allows = lambda essential: "ok"   # the daemon's backpressure would skip a read of a session that is busy answering; a measurement waits for it
        info = self.client.server_info()
        self.version = (info or {}).get("version") if isinstance(info, dict) else None
        if not self.version:
            raise RuntimeError("the throwaway service did not name its version")
        service = Path(self.env["XDG_STATE_HOME"]) / "opencode" / "service.json"
        if not service.exists():
            raise RuntimeError("the throwaway service wrote no service.json under the throwaway profile")

    @staticmethod
    def _port_open(port):
        with socket.socket() as sock:
            sock.settimeout(0.5)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    def wait_for(self, predicate, seconds, what):
        end = time.time() + seconds
        while time.time() < end:
            if predicate():
                return
            time.sleep(0.1)
        raise RuntimeError("timed out waiting for %s" % what)

    # -- sessions: created and removed by this helper only
    def create(self, n):
        title = "%s%s-%d" % (TITLE_PREFIX, self.nonce, n)
        made = self.client.session_create(self.agent, title, str(self.workdir), {"measurement": self.nonce})
        self.created.append(made["id"])
        return made["id"]

    def remove(self, sid):
        try:
            self.client.session_remove(sid)
        except self.sb.V2Error:
            pass
        self.removed.append(sid)

    def requests_with(self, marker):
        found = []
        try:
            for line in self.log.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if marker in json.dumps(row):
                    found.append(row)
        except OSError:
            pass
        return found

    def active(self, sid):
        return sid in (self.client.session_active() or {})

    def wait_idle(self, sid, seconds=60):
        self.wait_for(lambda: not self.active(sid), seconds, "the session to go idle")

    def drive(self, sid, case, marker):
        """Bring the session into `case` (the marker is not in the setup, so no request before the delivery can carry it)."""
        if case == "idle_after_normal_turn":
            self.client.prompt(sid, message_text("pre" + marker[1:], "reply=PONG"))
            self.wait_for(lambda: self.requests_with("pre" + marker[1:]), 30, "the setup turn")
            self.wait_idle(sid)
        elif case == "idle_after_provider_error":
            self.client.prompt(sid, message_text("pre" + marker[1:], "error=500"))
            self.wait_for(lambda: self.requests_with("pre" + marker[1:]), 30, "the failing turn")
            self.wait_idle(sid, 120)   # a provider error is retried a few times before the turn ends
            info = self.client.session_get(sid)
            if (info or {}).get("outcome") != "failed":
                raise RuntimeError("the session did not end in a provider error (outcome %r)" % ((info or {}).get("outcome"),))
        elif case == "working":
            self.client.prompt(sid, message_text("pre" + marker[1:], "sleep=%d tool=bash cmd=\"sleep 1\" reply=DONE" % WORK_SECONDS))
            self.wait_for(lambda: self.active(sid), 15, "the session to be working")
        else:
            raise ValueError(case)

    def deliver(self, sid, form, marker):
        delivery, resume = FORM_ARGS[form]
        text = message_text(marker, "reply=SAW")
        keys = ["measure:%s" % marker]
        if form == "prompt":
            return self.client.prompt(sid, text, {"fleet": {"keys": keys, "from": "switchboard"}})
        message_id = self.sb.derive_message_id(sid, keys)
        return self.client.synthetic(sid, text, {"fleet": {"keys": keys, "from": "switchboard"}}, resume, delivery, message_id, "delivery measurement")

    def cell(self, n, case, form):
        sid = self.create(n)
        marker = "mk%s%d" % (self.nonce, n)   # the setup text carries "pre" + marker[1:], never the marker itself: a request before the delivery cannot match
        row = {"case": case, "form": form}
        try:
            self.drive(sid, case, marker)
            before = time.time()
            try:
                self.deliver(sid, form, marker)
            except self.sb.V2Error as err:
                row.update(turn_started=False, ms=None, outcome="refused: %s %s" % (err.reason, err.status or ""))
                return row
            started = None
            end = before + self.wait + (WORK_SECONDS if case == "working" else 0)
            while time.time() < end:
                if self.requests_with(marker):
                    started = time.time()
                    break
                time.sleep(0.05)
            row.update(turn_started=started is not None, ms=None if started is None else int((started - before) * 1000),
                       outcome="reached the model" if started is not None else "nothing reached the model in %ds" % self.wait)
            return row
        finally:
            self.remove(sid)

    def finish(self):
        problems = []
        left = []
        if self.client is not None:
            try:
                for sid in list(self.created):
                    if sid not in self.removed:
                        self.remove(sid)
                listing, _ = self.client.session_list_page(None, None, 100)
                left = [s for s in (listing if isinstance(listing, list) else []) if str((s or {}).get("title", "")).startswith(TITLE_PREFIX)]
            except Exception as err:   # a failed list must not hide the rest of the clean-up
                problems.append("could not list the sessions: %s" % err)
        if left:
            problems.append("%d measurement session(s) were left behind" % len(left))
        try:
            self.run("service", "stop", timeout=60)
        except Exception:
            pass
        if self.model is not None:
            self.model.terminate()
            try:
                self.model.wait(10)
            except subprocess.TimeoutExpired:
                self.model.kill()
        shutil.rmtree(str(self.root), ignore_errors=True)
        return problems, len(left)


def measure(binary, wait=8):
    run = Measurement(binary, wait)
    rows, failure = [], None
    try:
        run.start()
        n = 0
        for case in CASES:
            for form in FORMS:
                n += 1
                rows.append(run.cell(n, case, form))
    except Exception as err:
        failure = "%s: %s" % (type(err).__name__, err)
    problems, left = run.finish()
    if failure:
        problems.insert(0, failure)
    return {"v2_version": getattr(run, "version", None), "cases": list(CASES), "forms": list(FORMS), "rows": rows,
            "sessions_created": len(run.created), "sessions_removed": len(run.removed), "sessions_left": left, "problems": problems}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="write the result JSON here")
    parser.add_argument("--wait", type=int, default=8, help="seconds to wait for a turn to start")
    parser.add_argument("--binary", default=shutil.which("opencode"))
    args = parser.parse_args(argv)
    result = measure(args.binary, args.wait)
    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    sys.stdout.write(text)
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
