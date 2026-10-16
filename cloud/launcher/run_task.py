#!/usr/bin/env python3
"""run-task: run ONE task in a cloud worker, end to end, and always clean up.

usage: run-task WORKER OWNER/NAME BRANCH BRIEF_FILE [--pull-secret NAME] [--image IMAGE]

The target repository is an argument and has no default. The driver
  1. refuses at once on a missing or unreadable input, or when a worker of that name already exists (a replayed run
     changes nothing);
  2. reads the model access entry (only the github-copilot entry of the OpenCode auth file) and a git token (the output of
     CW_GIT_TOKEN_CMD, default: app_token.py OWNER/NAME beside this file, the one-hour GitHub App token), each in memory;
  3. creates the pod through the launcher, handing both values over inherited pipes (--run-secret-fd) so they travel to
     kubectl's stdin only; the server password comes back on a pipe too;
  4. port-forwards, opens a session, sends the brief and waits for the session to be idle;
  5. stops the worker (the end hook pushes the work and the pod and its Secrets are deleted).
A normal end, a failure and an interrupt (SIGINT, SIGTERM) all pass through the same cleanup, and it says exactly once
`per-run secret NAME-run cleaned` or `REMAINS: delete it with: <command>`.

No secret is ever in an argument, an environment variable of a child, a file or a log. Everything the driver prints
passes through the launcher's ONE scrub function. The only files are a 0700 temporary directory holding the
port-forward's own log; the cluster login file is the launcher's (a path, never a token).

Environment: CW_KUBE_CONTEXT or CW_CLOUD_SERVER + CW_CLOUD_TOKEN_FILE (as the launcher), CW_MODEL_AUTH_FILE (default
~/.local/share/opencode/auth.json), CW_GIT_TOKEN_CMD, CW_WORKDIR (default /home/worker), CW_TMPDIR, CW_RUN_TIMEOUT
(1800 s), CW_HEALTH_TIMEOUT (180 s), CW_POLL_SECONDS (3), CW_PROGRESS_SECONDS (30), CW_LAUNCHER, CW_CREATE_TIMEOUT (900 s),
CW_RUN_RECORD_DIR (where each run's record goes; default: runs/ beside the launcher's audit file), CW_RECORD_TIMEOUT (30 s), CW_RECORD_MAX_BYTES (transcript cap, 5 MiB).

The run record: BEFORE the pod and its Secrets are deleted (every end takes this path) the driver saves, in
RECORD_DIR/<run id>/ (0700; files 0600): transcript.jsonl (the session's messages, one scrubbed line each, at most
TRANSCRIPT_CAP bytes), tally.json (tool calls by name and the turn count; no content) and run.json (worker, repository,
start, end, exit reason, head sha when known, and `transcript: unavailable` with a reason when it could not be read). A record
that fails never keeps a pod, and a failed stop never deletes a record.
"""
import argparse
import base64
import http.client
import json
import os
import re
import secrets
import shlex
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import cloud_worker as cw  # noqa: E402  (the launcher's scrub and Kube: one place for each)

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
MODEL_PROVIDER = "github-copilot"
MAX_BODY = 8 * 1024 * 1024
EXIT_INTERRUPTED = cw.EXIT_INTERRUPTED
TRANSCRIPT_CAP = 5 * 1024 * 1024   # bytes of transcript.jsonl kept per run; more is cut with a loud marker line
REPORT_NOTE = "\n\nWhen you are finished, write your final report as ONE line to the file %s (replace the file; one line, no secrets)." % cw.REPORT_PATH
TOOL_PART_TYPES = {"tool", "tool-call", "tool_call", "tool-use", "tool_use", "tool-invocation"}


class Interrupted(BaseException):
    pass


def _on_signal(signum, frame):
    raise Interrupted("signal %d" % signum)


class Driver:
    def __init__(self, a, env):
        self.a, self.env = a, env
        self.kube = None
        self.t0 = time.monotonic()
        self.name = cw.pod_name(a.worker)
        self.created = self.stopped = self.stop_failed = False
        self.killed = self.create_started = False
        self.note_seen = False   # the launcher already said what became of the per-run secret
        self.pf = None
        self.tmp = None
        self.password = ""
        self.sid = None
        self.recorded = False
        self.record_dir = None
        self.started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.run_id = "%s-%s-%s" % (a.worker, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), secrets.token_hex(3))
        self.nonce = secrets.token_hex(8)   # chosen before anything is made; the launcher records it on what it creates
        self.refused = False   # the launcher found the worker already there: it is another run's, never ours to touch

    # -- output: every line passes through the launcher's scrub --
    def say(self, text, stream=None, prefix="run-task: "):
        stream = stream or sys.stderr
        for line in (str(text).splitlines() or [""]):
            print(prefix + self.kube.scrub(line, limit=2000), file=stream)

    def step(self, text):
        self.say("[+%ds] %s" % (time.monotonic() - self.t0, text))

    def relay(self, text, stream=sys.stderr):
        for line in str(text).splitlines():
            if line.strip():
                if re.search(r"per-run secret \S+ (cleaned|REMAINS)", line):
                    self.note_seen = True
                print(self.kube.scrub(line, limit=2000), file=stream)

    # -- inputs --
    def model_entry(self):
        path = self.env.get("CW_MODEL_AUTH_FILE") or os.path.expanduser("~/.local/share/opencode/auth.json")
        try:
            with open(path) as f:
                entry = json.load(f).get(MODEL_PROVIDER)
        except (OSError, ValueError, AttributeError):
            entry = None
        if not entry or not isinstance(entry, dict):
            raise cw.CloudError("the model auth file is missing, unreadable or has no %s entry; nothing was created" % MODEL_PROVIDER, cw.EXIT_USAGE)
        return json.dumps({MODEL_PROVIDER: entry})

    def git_token(self):
        raw = self.env.get("CW_GIT_TOKEN_CMD")
        cmd = shlex.split(raw) if raw else [sys.executable, os.path.join(HERE, "app_token.py"), self.a.repo]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, env=dict(self.env, GH_REPO=self.a.repo))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            p = None
        token = (p.stdout if p and p.returncode == 0 else "").strip()
        if not token or len(token) > 4096:
            why = ""
            if p and p.stderr.strip():
                why = ": " + cw.scrub(p.stderr.strip().splitlines()[-1], (), 200)
            raise cw.CloudError("the git token command gave no usable token%s; nothing was created" % why, cw.EXIT_USAGE)
        return token

    # -- children --
    def run_cloud_worker(self, args, feeds=(), write_fds=(), timeout=900):
        """feeds: [(read_fd, value)]: the child inherits read_fd; a thread writes value into the matching write end."""
        pass_fds = [r for r, _, _ in feeds] + list(write_fds)
        argv = [sys.executable, self.launcher] + args
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=pass_fds,
                             env=self.kube.env, start_new_session=True)
        for r, w, value in feeds:
            os.close(r)
            threading.Thread(target=self._feed, args=(w, value), daemon=True).start()
        for fd in write_fds:
            os.close(fd)
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
        except Interrupted:
            self._shield()
            try:
                p.send_signal(signal.SIGINT)   # the launcher cleans up what it made and says so
                out, err = p.communicate(timeout=180)
            except Exception:
                p.kill()
                out, err = p.communicate()
            self.relay(err)
            raise
        return p.returncode, out, err

    @staticmethod
    def _feed(fd, value):
        try:
            data = value.encode()
            while data:
                n = os.write(fd, data)
                data = data[n:]
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _shield():
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, signal.SIG_IGN)   # a second Ctrl-C must not abandon the cleanup

    # -- the run --
    def create(self, model, token):
        pipes = [os.pipe(), os.pipe()]
        pw_r, pw_w = os.pipe()
        feeds = [(pipes[0][0], pipes[0][1], model), (pipes[1][0], pipes[1][1], token)]
        args = ["create", self.a.worker, "--repo", self.a.repo, "--branch", self.a.branch,
                "--run-secret-fd", "model-auth=%d" % pipes[0][0], "--run-secret-fd", "git-token=%d" % pipes[1][0],
                "--password-fd", str(pw_w), "--run-id", self.nonce]
        if self.a.pull_secret:
            args += ["--pull-secret", self.a.pull_secret]
        if self.a.image:
            args += ["--image", self.a.image]
        timeout = float(self.env.get("CW_CREATE_TIMEOUT") or 900)
        self.create_started = True
        try:
            rc, out, err = self.run_cloud_worker(args, feeds, write_fds=[pw_w], timeout=timeout)
            with os.fdopen(pw_r, "rb") as f:
                pw = f.read(4096).decode(errors="replace").strip()
        except BaseException:
            for fd in (pw_r,):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        self.relay(err)
        if rc == 0:
            self.created = True
        if rc == cw.EXIT_EXISTS:   # another run made the worker first: this run made nothing and must change nothing
            self.refused = True
            raise cw.CloudError("worker %s already exists (another run created it first); refusing, nothing was changed" % self.a.worker, cw.EXIT_REFUSED)
        if rc != 0:
            self.killed = rc < 0
            raise cw.CloudError("create failed (exit %d)" % rc, cw.EXIT_CLOUD)
        if not pw:
            raise cw.CloudError("create gave no server password", cw.EXIT_CLOUD)
        self.password = pw
        self.kube.add_secrets(pw)

    def port_forward(self, pod):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        k = self.kube
        cmd = [k.bin] + (["--kubeconfig", k.kubeconfig] if k.kubeconfig else []) + ["--context", k.context, "-n", k.ns,
               "port-forward", "pod/" + pod, "%d:4096" % port, "--address", "127.0.0.1"]
        self.logf = open(os.path.join(self.tmp, "pf.log"), "wb")
        self.pf = subprocess.Popen(cmd, stdout=self.logf, stderr=subprocess.STDOUT, env=k.env, start_new_session=True)
        self.pf_port = port
        return port

    def api(self, port, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)   # fixed host, plain loopback, no redirect followed
        try:
            hdr = {"Authorization": "Basic " + base64.b64encode(("opencode:" + self.password).encode()).decode()}
            data = None
            if body is not None:
                data = json.dumps(body).encode()
                hdr["Content-Type"] = "application/json"
            c.request(method, path, body=data, headers=hdr)
            r = c.getresponse()
            return r.status, r.read(MAX_BODY + 1)[:MAX_BODY]
        finally:
            c.close()

    def pf_log(self):
        try:
            self.logf.flush()
            with open(os.path.join(self.tmp, "pf.log"), "rb") as f:
                return f.read(300).decode(errors="replace").replace("\n", " ")
        except OSError:
            return ""

    def task(self, pod):
        e = self.env
        health_t, run_t = float(e.get("CW_HEALTH_TIMEOUT") or 180), float(e.get("CW_RUN_TIMEOUT") or 1800)
        poll, prog = float(e.get("CW_POLL_SECONDS") or 3), float(e.get("CW_PROGRESS_SECONDS") or 30)
        self.step("step port-forward: starting")
        port = self.port_forward(pod)
        end = time.monotonic() + health_t
        while True:
            try:
                if self.api(port, "GET", "/api/info")[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            if self.pf.poll() is not None:
                raise cw.CloudError("health: the port-forward ended (%s)" % self.pf_log())
            if time.monotonic() >= end:
                raise cw.CloudError("health: the worker did not become healthy in %ds" % health_t)
            time.sleep(poll)
        self.step("healthy")
        st, body = self.api(port, "POST", "/api/session", {"title": self.a.worker, "location": {"directory": e.get("CW_WORKDIR") or "/home/worker"}})
        try:
            sid = json.loads(body)["data"]["id"]
            if st >= 300 or not isinstance(sid, str):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise cw.CloudError("session: could not create one (status %d)" % st)
        self.sid = sid
        self.step("session created (%s)" % sid)
        st, _ = self.api(port, "POST", "/api/session/%s/prompt" % sid, {"text": self.brief.rstrip() + REPORT_NOTE})
        if st >= 300:
            raise cw.CloudError("prompt: could not send the brief (status %d)" % st)
        self.step("prompt sent")
        end, nxt = time.monotonic() + run_t, time.monotonic() + prog
        while True:
            try:
                st, active = self.api(port, "GET", "/api/session/active")
                is_active = st != 200 or sid in active.decode(errors="replace")   # unreadable: treated as still running
            except (OSError, http.client.HTTPException):
                is_active = True
            if not is_active and self.last_reply_done(port, sid):
                self.step("idle: the session finished")
                return
            if time.monotonic() >= nxt:
                self.step("polling: task running")
                nxt = time.monotonic() + prog
            if time.monotonic() >= end:
                raise cw.CloudError("polling: the task did not finish in %ds" % run_t)
            time.sleep(poll)

    def last_reply_done(self, port, sid):
        try:
            st, body = self.api(port, "GET", "/api/session/%s/message" % sid)
            msgs = json.loads(body).get("data", []) if st == 200 else []
        except (OSError, ValueError, AttributeError, http.client.HTTPException):
            return False
        replies = [m for m in msgs if isinstance(m, dict) and m.get("type") == "assistant"]
        return bool(replies and isinstance(replies[-1].get("time"), dict) and replies[-1]["time"].get("completed"))

    # -- the run record: kept on the operator's machine, written before the pod is deleted --
    def record_root(self):
        env = self.env
        return env.get("CW_RUN_RECORD_DIR") or os.path.join(
            os.path.dirname(env.get("CW_CLOUD_AUDIT") or os.path.join(env.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "cw-cloud", "audit.jsonl")), "runs")

    def _write_private(self, name, text):
        fd = os.open(os.path.join(self.record_dir, name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(os.path.join(self.record_dir, name), 0o600)

    def _read_messages(self):
        """The session's messages through a port-forward to the pod's server; (list, None) or (None, why)."""
        if not self.sid or not self.password:
            return None, "no session was opened"
        try:
            if self.pf is None or self.pf.poll() is not None:
                port = self.port_forward(self.name)
            else:
                port = self.pf_port
            end = time.monotonic() + float(self.env.get("CW_RECORD_TIMEOUT") or 30)
            while True:
                try:
                    if self.api(port, "GET", "/api/info")[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if self.pf.poll() is not None or time.monotonic() >= end:
                    return None, "the pod's server could not be reached"
                time.sleep(0.3)
            st, body = self.api(port, "GET", "/api/session/%s/message" % self.sid)
            if st != 200:
                return None, "the server answered status %d" % st
            msgs = json.loads(body).get("data")
            if not isinstance(msgs, list):
                return None, "the messages were not a list"
            return msgs, None
        except (OSError, ValueError, AttributeError, http.client.HTTPException, cw.CloudError):
            return None, "the messages could not be read"

    @staticmethod
    def _tool_names(node, out):
        if isinstance(node, dict):
            if node.get("type") in TOOL_PART_TYPES:
                n = node.get("tool") or node.get("name") or node.get("toolName")
                out.append(n if isinstance(n, str) and n else "unknown")
            for v in node.values():
                Driver._tool_names(v, out)
        elif isinstance(node, list):
            for v in node:
                Driver._tool_names(v, out)

    def _head_sha(self):
        try:
            out = self.kube.exec(self.name, "git", "-C", self.env.get("CW_WORKDIR") or "/home/worker", "rev-parse", "HEAD", keep_stdout=True)
        except cw.CloudError:
            return None
        sha = (out.strip().splitlines() or [""])[0]
        return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else None

    def record(self, reason):
        """Save the run's record once, before the pod goes. Never raises an error (an interrupt apart, and then run.json
        is still written): a record that fails must not keep a pod."""
        if self.recorded or not self.created:
            return
        self.recorded = True
        info = {"worker": self.a.worker, "repository": self.a.repo, "run_id": self.run_id, "start": self.started,
                "end": None, "exit_reason": self.kube.scrub(reason, limit=300), "head_sha": None, "transcript": "unavailable",
                "reason": "the record was not finished"}
        try:
            root = self.record_root()
            os.makedirs(root, mode=0o700, exist_ok=True)
            os.chmod(root, 0o700)
            self.record_dir = os.path.join(root, self.run_id)
            os.mkdir(self.record_dir, 0o700)
            try:
                msgs, why = self._read_messages()
                if msgs is None:
                    info["reason"] = why
                else:
                    cap = int(self.env.get("CW_RECORD_MAX_BYTES") or TRANSCRIPT_CAP)
                    lines, size, cut = [], 0, False
                    tally, turns = {}, 0
                    for m in msgs:
                        names = []
                        self._tool_names(m, names)
                        for n in names:
                            n = self.kube.scrub(n, limit=64)
                            tally[n] = tally.get(n, 0) + 1
                        if isinstance(m, dict) and m.get("type") == "assistant":
                            turns += 1
                        line = self.kube.scrub(json.dumps(m, sort_keys=True), limit=cw.SCRUB_INPUT_MAX)
                        if size + len(line) + 1 > cap and not cut:
                            cut = True
                            lines.append(json.dumps({"truncated": True, "note": "TRANSCRIPT CUT: over %d bytes; the rest is not kept" % cap}))
                        if not cut:
                            lines.append(line)
                            size += len(line) + 1
                    self._write_private("transcript.jsonl", "".join(x + "\n" for x in lines))
                    self._write_private("tally.json", json.dumps({"tool_calls": tally, "turns": turns, "messages": len(msgs)}, sort_keys=True) + "\n")
                    info["transcript"], info["reason"] = "saved", None
                    info["transcript_truncated"] = cut
            finally:
                info["head_sha"] = self._head_sha()
        except (OSError, ValueError, cw.CloudError) as e:
            info["reason"] = "the record could not be written: %s" % self.kube.scrub(e, limit=200)
        finally:
            info["end"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if info["reason"] is None:
                del info["reason"]
            try:
                self._write_private("run.json", json.dumps(info, sort_keys=True) + "\n")
            except (OSError, TypeError, AttributeError):
                pass
            if self.record_dir:
                self.say("run record: %s" % self.record_dir)

    def stop(self, reason):
        self.record(reason)   # before anything is deleted; it never raises and never holds the stop back
        if self.record_dir:
            reason = "%s; run record %s" % (reason, self.record_dir)
        self.kill_pf()   # the record has read what it needs
        rc, out, err = self.run_cloud_worker(["stop", self.a.worker, "--reason", reason], timeout=600)
        self.relay(err)
        if out.strip():
            self.relay("stopped; result: " + out.strip().splitlines()[-1], sys.stdout)
        return rc == 0

    def kill_pf(self):
        if self.pf and self.pf.poll() is None:
            self.pf.terminate()
            try:
                self.pf.wait(5)
            except subprocess.TimeoutExpired:
                self.pf.kill()
                self.pf.wait()
        self.pf = None

    def secret_line(self, everything=False):
        """The ONE statement about the per-run secret: cleaned, or REMAINS with the exact command. Deleting is idempotent
        (no error when it is already gone). `everything` also removes the pod and the password Secret (the worker is
        known to be ours: a replayed run was refused before anything was made)."""
        run = self.name + "-run"
        try:
            if everything:
                self.kube.delete(self.name)
            else:
                self.kube.delete_run_secret(self.name)
            self.say("per-run secret %s cleaned" % run)
        except cw.CloudError:
            self.say("per-run secret %s REMAINS: delete it with: %s" % (run, self.kube.delete_command("secret", run)))

    def _owner(self):
        """Whose is the worker named for this run? Read back from the cluster: the pod's cw/run-id annotation (or, when
        there is no pod, the per-run Secret's cw/run-id label) must equal this run's nonce. Returns (verdict, has_pod):
        'ours', 'theirs' (present, no match: leave it whole), 'none' (nothing there) or 'unknown' (could not be read)."""
        try:
            pod = self.kube.get_pod(self.name)
            if pod:
                got = ((pod.get("metadata") or {}).get("annotations") or {}).get("cw/run-id")
                return ("ours" if got == self.nonce else "theirs"), True
            sec = self.kube.get_run_secret(self.name)
            if sec:
                got = ((sec.get("metadata") or {}).get("labels") or {}).get("cw/run-id")
                if got is None:   # a Secret with credentials and no run id (a crash before it was labelled): never guessed
                    return "unknown", False
                return ("ours" if got == self.nonce else "theirs"), False
            return "none", False
        except (cw.CloudError, ValueError, AttributeError, TypeError):
            return "unknown", False

    def cleanup(self):
        self._shield()
        if self.refused:
            self.say("worker %s belongs to another run; left whole (no end hook, no Secret touched)" % self.name)
        elif self.create_started and not self.created and not self.note_seen:
            # the launcher did not say what became of its work (cut off, killed, or finished just as the signal came):
            # touch only what the cluster proves is this run's
            verdict, has_pod = self._owner()
            if verdict == "ours" and has_pod:
                self.created = True
            elif verdict == "ours":
                self.secret_line(everything=True)
            elif verdict == "theirs":
                self.say("worker %s is not this run's (its run id does not match); left whole" % self.name)
            elif verdict == "unknown":
                self.say("per-run secret %s-run REMAINS (ownership could not be read, nothing was touched): if this run made it, delete it with: %s"
                         % (self.name, self.kube.delete_command("secret", self.name + "-run")))
        if self.created and not self.stopped:
            self.say("ending early; stopping the worker so no per-run secret is left")
            try:
                self.stopped = self.stop("driver ended early")
            except BaseException:
                self.stopped = False
            self.stop_failed = not self.stopped
        if self.stop_failed:
            self.say("the worker is kept (its end hook could not push, see above); delete it only after the work is saved: %s"
                     % self.kube.delete_command("pod", self.name))
        if self.created:
            self.secret_line()
        self.kill_pf()
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def run(self):
        a = self.a
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        try:
            self.kube = cw.Kube(self.env)
        except cw.CloudError as e:
            print("run-task: %s" % cw.scrub(e, (), 1500), file=sys.stderr)
            return e.code
        self.launcher = self.env.get("CW_LAUNCHER") or os.path.join(HERE, "cloud_worker.py")
        try:
            try:
                with open(a.brief_file) as f:
                    self.brief = f.read(1024 * 1024)
            except OSError:
                raise cw.CloudError("the brief file is not readable; nothing was created", cw.EXIT_USAGE)
            if not self.brief.strip():
                raise cw.CloudError("the brief file is empty; nothing was created", cw.EXIT_USAGE)
            model = self.model_entry()
            self.kube.add_secrets(model)
            if self.kube.get_pod(self.name):   # a replayed run: nothing is touched, nothing is cleaned
                raise cw.CloudError("worker %s already exists; refusing (nothing was changed)" % a.worker, cw.EXIT_REFUSED)
            token = self.git_token()
            self.kube.add_secrets(token)
            self.tmp = tempfile.mkdtemp(prefix="cw-run-", dir=self.env.get("CW_TMPDIR") or None)   # 0700
            self.step("step create: creating the worker pod")
            self.create(model, token)
            del model, token
            self.step("pod created")
            self.task(self.name)
            self.step("stopping: running the end hook and deleting the per-run secret")
            if self.stop("task done"):
                self.stopped = True
                self.step("stopped")
                return 0
            self.stop_failed = self.stopped = True   # the launcher kept the pod on purpose; the cleanup removes the run secret
            raise cw.CloudError("stop failed: the pod is kept so the work is not lost (see above)", cw.EXIT_HOOK)
        except cw.CloudError as e:
            self.say("FAILED: %s" % e)
            return e.code
        except Interrupted:
            self.say("interrupted")
            return EXIT_INTERRUPTED
        finally:
            try:
                self.cleanup()
            finally:
                self.kube.close()


def main(argv=None, env=None):
    env = dict(os.environ if env is None else env)
    p = argparse.ArgumentParser(prog="run-task", description="run one task in a cloud worker and clean up")
    p.add_argument("worker")
    p.add_argument("repo", metavar="OWNER/NAME", help="the target repository (no default)")
    p.add_argument("branch")
    p.add_argument("brief_file")
    p.add_argument("--pull-secret")
    p.add_argument("--image")
    try:
        a = p.parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    if not cw.NAME_RE.match(a.worker) or not REPO_RE.match(a.repo) or not re.match(r"^[A-Za-z0-9._/-]{1,200}$", a.branch):
        print("run-task: bad worker name, repository (OWNER/NAME) or branch", file=sys.stderr)
        return 2
    if not (cw.token_mode(env) or (env.get("CW_KUBE_CONTEXT") or "").strip()):
        print("run-task: CW_KUBE_CONTEXT is not set; refusing to run (the active kubectl context is never used)", file=sys.stderr)
        return 2
    return Driver(a, env).run()


if __name__ == "__main__":
    sys.exit(main())
