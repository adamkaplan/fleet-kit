"""Offline tests for the cloud-worker launcher, reaper, end hook and token mint (scenarios S1-S5, U1 of #370).

No cloud, no network beyond loopback: kubectl is the fake beside this file; time is a fake clock.
"""
import argparse
import datetime as dt
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402
import end_hook  # noqa: E402
import token_mint  # noqa: E402

T0 = dt.datetime(2026, 10, 14, 12, 0, 0, tzinfo=dt.timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += dt.timedelta(**kw)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.state = os.path.join(d, "kube.json")
        self.write_state({"pods": {}})
        self.env = {"FLEET_KUBECTL": os.path.join(HERE, "fake_kubectl"), "FAKE_KUBE_STATE": self.state,
                    "FLEET_CLOUD_AUDIT": os.path.join(d, "audit.jsonl"), "FLEET_CLOUD_IMAGE": "registry.example.internal/worker:1",
                    "FLEET_TOKEN_ENDPOINT": "https://tokens.example.internal/mint", "FLEET_CLOUD_DOMAIN": "example.internal"}
        self.clock = Clock()

    def write_state(self, st):
        with open(self.state, "w") as f:
            json.dump(st, f)

    def st(self):
        with open(self.state) as f:
            return json.load(f)

    def patch_state(self, **kw):
        st = self.st()
        st.update(kw)
        self.write_state(st)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cw.main(list(argv), self.env, self.clock, lambda s: self.clock.advance(seconds=s))
        return code, out.getvalue(), err.getvalue()

    def create(self, worker="coder7", *extra):
        code, out, err = self.run_cli("create", worker, "--repo", "acme-org/widget-service", "--branch", "main", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def pod(self, worker="coder7"):
        return self.st()["pods"].get(cw.pod_name(worker))

    def set_activity(self, worker, busy, last):
        act = self.st().get("activity", {})
        act[cw.pod_name(worker)] = {"busy": busy, "last_activity": cw.iso(last)}
        self.patch_state(activity=act)

    def audit_lines(self):
        with open(self.env["FLEET_CLOUD_AUDIT"]) as f:
            return [json.loads(x) for x in f]


class S1Idle(Base):
    def test_idle_worker_stopped_active_kept(self):
        self.create("idle1")
        self.create("busy1")
        self.patch_state(hook_env={"FLEET_WORKDIR": self.clean_repo(), "FLEET_WORKER": "x"})
        for w in ("idle1", "busy1"):
            self.set_activity(w, False, self.clock())
        self.clock.advance(minutes=9)
        self.set_activity("busy1", False, self.clock())  # a tool call 1 minute before the idle limit
        self.clock.advance(minutes=1, seconds=30)  # 10m30s after start; idle1 idle 10m30s, busy1 1m30s
        res = self.reap_pass()
        self.assertEqual([r[:2] for r in res], [("idle1", "idle")])
        self.assertIsNone(self.pod("idle1"))
        self.assertIsNotNone(self.pod("busy1"))

    def test_mid_task_worker_is_not_idle(self):
        self.create()
        self.patch_state(hook_env={"FLEET_WORKDIR": self.clean_repo(), "FLEET_WORKER": "x"})
        self.set_activity("coder7", True, T0)  # a long build: busy, however old the last activity
        self.clock.advance(minutes=30)
        self.assertEqual(self.reap_pass(), [])
        self.assertIsNotNone(self.pod())

    def test_inbound_traffic_alone_is_not_activity(self):
        # The reaper reads only the worker's session state, never request counts: a watched-but-idle worker is stopped.
        self.create()
        self.patch_state(hook_env={"FLEET_WORKDIR": self.clean_repo(), "FLEET_WORKER": "x"})
        self.set_activity("coder7", False, T0)
        self.clock.advance(minutes=12)
        self.assertEqual([r[:2] for r in self.reap_pass()], [("coder7", "idle")])

    def test_unreadable_activity_is_not_a_reason_to_stop(self):
        self.create()
        self.clock.advance(minutes=30)  # no activity entry: the fake's exec fails
        self.assertEqual(self.reap_pass(), [("coder7", "activity unreadable", 1)])  # reported, not killed
        self.assertIsNotNone(self.pod())

    def reap_pass(self):
        return cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()

    def clean_repo(self):
        return make_repo(self.tmp.name, "clean", dirty=False)[0]


def make_repo(base, name, dirty):
    """A working clone with a bare origin; returns (workdir, origin)."""
    origin, work = os.path.join(base, name + "-origin.git"), os.path.join(base, name)
    g = lambda *a, cwd=None: subprocess.run(["git"] + list(a), cwd=cwd, check=True, capture_output=True)
    g("init", "--bare", "-b", "main", origin)
    g("clone", origin, work)
    for k, v in (("user.name", "t"), ("user.email", "t@invalid")):
        g("config", k, v, cwd=work)
    with open(os.path.join(work, "a.txt"), "w") as f:
        f.write("one\n")
    g("add", "-A", cwd=work)
    g("commit", "-m", "init", cwd=work)
    g("push", "origin", "HEAD:main", cwd=work)
    g("fetch", "origin", cwd=work)
    if dirty:
        with open(os.path.join(work, "a.txt"), "a") as f:
            f.write("two\n")
        with open(os.path.join(work, "new.txt"), "w") as f:
            f.write("new\n")
        with open(os.path.join(work, ".env"), "w") as f:
            f.write("X=1\n")
        g("checkout", "-b", "feature", cwd=work)
    return work, origin


class S2Hard(Base):
    def stub_hook(self):
        self.patch_state(hook_env={"FLEET_WORKDIR": make_repo(self.tmp.name, "r", False)[0], "FLEET_WORKER": "x"})

    def reap(self):
        return cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()

    def test_unextended_stopped_extended_runs(self):
        self.create("plain", "--hard", "1h")
        self.create("longer", "--hard", "1h")
        self.stub_hook()
        for w in ("plain", "longer"):
            self.set_activity(w, True, T0)  # busy throughout: only the hard limit can stop them
        code, out, _ = self.run_cli("extend", "longer", "--hard", "+1h")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["hard_deadline"], "2026-10-14T14:00:00Z")
        self.clock.advance(hours=1, minutes=1)
        self.assertEqual([r[:2] for r in self.reap()], [("plain", "hard limit")])
        self.assertIsNone(self.pod("plain"))
        self.assertIsNotNone(self.pod("longer"))

    def test_extend_past_max_exits_6(self):
        self.create("coder7", "--hard", "2h")
        code, _, err = self.run_cli("extend", "coder7", "--hard", "+7h")
        self.assertEqual(code, 6, err)
        code, _, _ = self.run_cli("extend", "coder7", "--hard", "+1h")
        self.assertEqual(code, 0)
        self.assertEqual(self.audit_lines()[-2]["ok"], False)

    def test_extend_to_iso_time_and_idle(self):
        self.create()
        code, out, _ = self.run_cli("extend", "coder7", "--hard", "2026-10-14T15:00:00Z", "--idle", "30m")
        self.assertEqual((code, json.loads(out)["idle_seconds"]), (0, 1800))

    def test_record_dir_deadline_wins(self):
        self.create("coder7", "--hard", "2h")
        self.stub_hook()
        self.set_activity("coder7", True, T0)
        rd = os.path.join(self.tmp.name, "records")
        os.makedirs(rd)
        with open(os.path.join(rd, "coder7.json"), "w") as f:
            json.dump({"hard_deadline": "2026-10-14T12:30:00Z"}, f)
        self.env["FLEET_CLOUD_RECORD_DIR"] = rd
        self.clock.advance(minutes=31)
        self.assertEqual([r[:2] for r in self.reap()], [("coder7", "hard limit")])

    def test_reap_via_cli_at_the_limit(self):
        self.create("coder7", "--hard", "1h")
        self.stub_hook()
        self.set_activity("coder7", True, T0)
        self.clock.advance(minutes=59)
        self.assertEqual(self.run_cli("reap")[0], 0)
        self.assertIsNotNone(self.pod())  # not yet
        self.clock.advance(minutes=3)  # a pass once a minute: at most 1 minute late
        self.assertEqual(self.run_cli("reap")[0], 0)
        self.assertIsNone(self.pod())


class S3NoStoredCredential(Base):
    def test_rendered_spec_is_clean_and_has_endpoint(self):
        self.create()
        applied = self.st()["applied"]
        spec = [o for o in applied if o["kind"] == "Pod"][0]
        self.assertEqual(cw.scan_credentials(spec), [])
        env = {e["name"]: e["value"] for e in spec["spec"]["containers"][0]["env"]}
        self.assertEqual(env["FLEET_TOKEN_ENDPOINT"], "https://tokens.example.internal/mint")
        self.assertEqual(sorted(env), sorted(["FLEET_WORKER", "FLEET_REPO", "FLEET_BRANCH", "FLEET_IDLE_SECONDS",
                                              "FLEET_HARD_DEADLINE", "FLEET_TOKEN_ENDPOINT", "FLEET_AUDIT_RUNTIME_ID"]))
        # the password is a secret-backed file, 0600, not an environment variable
        vol = [v for v in spec["spec"]["volumes"] if v["name"] == "pw"][0]
        self.assertEqual(vol["secret"]["defaultMode"], 0o600)
        self.assertEqual(spec["spec"]["containers"][0]["volumeMounts"][0]["mountPath"], "/run/fleet")
        self.assertNotIn("password", json.dumps(env).lower())

    def test_password_is_never_in_output_or_audit(self):
        r, w = os.pipe()
        out = self.create("coder7", "--password-fd", str(w))
        os.close(w)
        with os.fdopen(r) as f:
            password = f.read()
        self.assertGreaterEqual(len(password), 32)
        self.assertNotIn(password, json.dumps(out))
        self.assertNotIn(password, json.dumps(self.audit_lines()))
        self.assertNotIn(password, json.dumps([o for o in self.st()["applied"] if o["kind"] == "Pod"]))

    def test_a_pat_in_the_spec_is_found_and_create_refuses(self):
        spec = {"spec": {"containers": [{"env": [{"name": "GH_TOKEN", "value": "x"}]}]}}
        self.assertEqual(cw.scan_credentials(spec), ["env:GH_TOKEN"])
        spec = {"spec": {"containers": [{"env": [{"name": "FLEET_REPO", "value": "ghp_" + "a" * 36}]}]}}
        self.assertTrue(cw.scan_credentials(spec))
        code, _, err = self.run_cli("create", "coder7", "--repo", "ghp_" + "a" * 36 + "/x", "--branch", "main")
        self.assertEqual(code, 2, err)
        self.assertEqual(self.st().get("applied"), None)

    def test_missing_token_endpoint_refused(self):
        del self.env["FLEET_TOKEN_ENDPOINT"]
        code, _, _ = self.run_cli("create", "coder7", "--repo", "a/b", "--branch", "m")
        self.assertEqual(code, 2)

    def test_mint_lifetime_at_most_60_minutes_and_not_in_env(self):
        def serve(lifetime):
            class H(http.server.BaseHTTPRequestHandler):
                seen = []

                def do_POST(self):
                    H.seen.append(self.headers.get("Authorization"))
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    body = json.dumps({"token": "tok-" + "z" * 20, "expires_at": cw.iso(T0 + dt.timedelta(seconds=lifetime))}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass
            srv = http.server.HTTPServer(("127.0.0.1", 0), H)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            return srv, H
        ident = os.path.join(self.tmp.name, "identity")
        with open(ident, "w") as f:
            f.write("federated-identity-jwt")
        out = os.path.join(self.tmp.name, "tok")
        srv, H = serve(3600)
        url = "http://127.0.0.1:%d/mint" % srv.server_port
        self.assertEqual(token_mint.mint(url, ident, out, now=T0), 3600)
        self.assertEqual(oct(os.stat(out).st_mode & 0o777), "0o600")
        self.assertEqual(H.seen, ["Bearer federated-identity-jwt"])
        srv2, _ = serve(7200)
        with self.assertRaises(ValueError):
            token_mint.mint("http://127.0.0.1:%d/mint" % srv2.server_port, ident, out + "2", now=T0)
        self.assertFalse(os.path.exists(out + "2"))
        with open(out) as f:
            self.assertEqual(f.read(), "tok-" + "z" * 20)  # the token is only in the 0600 file
        srv3, _ = serve(3601)  # one second over the limit
        with self.assertRaises(ValueError):
            token_mint.mint("http://127.0.0.1:%d/mint" % srv3.server_port, ident, out + "3", now=T0)
        srv4, _ = serve(-5)  # already expired
        with self.assertRaises(ValueError):
            token_mint.mint("http://127.0.0.1:%d/mint" % srv4.server_port, ident, out + "4", now=T0)

    def test_mint_refuses_plain_http_off_loopback(self):
        with self.assertRaises(ValueError):
            token_mint.mint("http://tokens.example.internal/mint")


class S4Audit(Base):
    def test_four_actions_four_lines_and_failures(self):
        self.patch_state(hook_env={"FLEET_WORKDIR": make_repo(self.tmp.name, "r", False)[0], "FLEET_WORKER": "x"})
        self.create("coder7")
        self.clock.advance(hours=1)
        self.assertEqual(self.run_cli("extend", "coder7", "--hard", "+30m", "--reason", "more time")[0], 0)
        self.assertEqual(self.run_cli("stop", "coder7", "--reason", "done")[0], 0)
        self.create("coder8")
        self.assertEqual(self.run_cli("delete", "coder8", "--force", "--reason", "cleanup")[0], 0)
        lines = self.audit_lines()
        self.assertEqual([x["action"] for x in lines], ["create", "extend", "stop", "create", "delete"])
        for x in lines:
            self.assertIn(x["worker"], ("coder7", "coder8"))
            self.assertTrue(x["runtime_id"].startswith("pod/coder"))
            self.assertTrue(x["ok"])
        self.assertEqual(lines[0]["cost_estimate_usd"], 0.0)
        self.assertGreater(lines[2]["cost_estimate_usd"], 0)
        self.assertEqual(lines[1]["reason"], "more time")
        self.assertEqual(oct(os.stat(self.env["FLEET_CLOUD_AUDIT"]).st_mode & 0o777), "0o600")

    def test_failed_actions_write_a_line_with_ok_false(self):
        before = lambda: len(self.audit_lines()) if os.path.exists(self.env["FLEET_CLOUD_AUDIT"]) else 0
        self.patch_state(fail_apply=True)
        self.assertEqual(self.run_cli("create", "coder7", "--repo", "a/b", "--branch", "m")[0], 3)
        self.assertEqual(before(), 1)
        self.assertFalse(self.audit_lines()[-1]["ok"])
        self.assertEqual(self.run_cli("extend", "ghost", "--hard", "+1h")[0], 2)
        self.assertEqual(before(), 2)
        self.assertEqual(self.audit_lines()[-1]["action"], "extend")

    def test_never_ready_times_out_exit_4_and_is_cleaned_up(self):
        self.patch_state(never_ready=True)
        code, _, _ = self.run_cli("create", "coder7", "--repo", "a/b", "--branch", "m", "--wait", "10")
        self.assertEqual(code, 4)
        self.assertIsNone(self.pod())
        self.assertFalse(self.audit_lines()[-1]["ok"])

    def test_spend_cap_exit_5(self):
        self.env["FLEET_CLOUD_SPEND_CAP_USD"] = "0.05"
        self.create("coder7")
        self.clock.advance(hours=1)
        self.assertEqual(self.run_cli("create", "coder8", "--repo", "a/b", "--branch", "m")[0], 5)

    def test_idempotent_stop_and_delete_of_gone_worker(self):
        self.assertEqual(self.run_cli("stop", "nobody")[0], 0)
        self.assertEqual(self.run_cli("delete", "nobody")[0], 0)

    def test_bad_name_usage(self):
        self.assertEqual(self.run_cli("stop", "Bad Name")[0], 2)
        self.assertEqual(self.run_cli("bogus")[0], 2)

    def test_status(self):
        self.create("coder7")
        self.set_activity("coder7", False, T0)
        self.clock.advance(minutes=3)
        code, out, _ = self.run_cli("status")
        rec = json.loads(out)[0]
        self.assertEqual((rec["worker"], rec["phase"], rec["idle_since"]), ("coder7", "Running", "2026-10-14T12:00:00Z"))
        self.assertEqual(rec["deadlines"]["idle"], "2026-10-14T12:10:00Z")
        self.assertEqual(self.run_cli("status", "coder7")[0], 0)
        self.assertEqual(self.run_cli("status", "ghost")[0], 2)
        self.assertEqual(len(self.audit_lines()), 1)  # status is read only: no audit line


class S5EndHook(Base):
    def test_unpushed_work_goes_to_wip_branch_before_delete(self):
        work, origin = make_repo(self.tmp.name, "r", dirty=True)
        self.patch_state(hook_env={"FLEET_WORKDIR": work, "FLEET_WORKER": "coder7"})
        self.create("coder7")
        code, out, err = self.run_cli("stop", "coder7", "--reason", "operator")
        self.assertEqual(code, 0, err)
        res = json.loads(out)
        self.assertEqual((res["pushed"], res["wip_branch"]), (True, "coder7/wip"))
        self.assertIsNone(self.pod())
        files = subprocess.run(["git", "-C", origin, "show", "--stat", "--format=%s", "coder7/wip"], capture_output=True, text=True).stdout
        self.assertIn("WIP", files)
        self.assertIn("new.txt", files)
        self.assertNotIn(".env", files)  # secret-looking files are left out

    def test_deadline_idle_and_drain_all_use_the_same_hook(self):
        # the reaper's two stops run the hook through stop(); a drain notice is the operator's `stop` with a reason
        for reason in ("hard limit", "idle", "node drain"):
            work, origin = make_repo(self.tmp.name, "r-" + reason.replace(" ", ""), dirty=True)
            self.patch_state(hook_env={"FLEET_WORKDIR": work, "FLEET_WORKER": "coder7"})
            self.create("coder7")
            code, out, _ = self.run_cli("stop", "coder7", "--reason", reason)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["wip_branch"], "coder7/wip")
            self.assertEqual(self.audit_lines()[-1]["reason"], reason)

    def test_push_failure_keeps_the_pod_and_exits_1(self):
        work, origin = make_repo(self.tmp.name, "r", dirty=True)
        subprocess.run(["git", "-C", work, "remote", "set-url", "origin", os.path.join(self.tmp.name, "gone.git")], check=True)
        self.patch_state(hook_env={"FLEET_WORKDIR": work, "FLEET_WORKER": "coder7"})
        self.create("coder7")
        code, _, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 1)
        self.assertIn("pod kept", err)
        self.assertIsNotNone(self.pod())
        self.assertEqual(self.pod()["metadata"]["annotations"]["fleet/kept"], "true")
        self.assertFalse(self.audit_lines()[-1]["ok"])
        # delete is refused until the hook has run, then allowed with --force
        self.assertEqual(self.run_cli("delete", "coder7")[0], 6)
        self.assertIsNotNone(self.pod())
        self.assertEqual(self.run_cli("delete", "coder7", "--force")[0], 0)
        self.assertIsNone(self.pod())

    def test_clean_pushed_tree_pushes_nothing(self):
        work, _ = make_repo(self.tmp.name, "r", dirty=False)
        self.patch_state(hook_env={"FLEET_WORKDIR": work, "FLEET_WORKER": "coder7"})
        self.create("coder7")
        code, out, _ = self.run_cli("stop", "coder7")
        self.assertEqual((code, json.loads(out)["pushed"]), (0, False))
        self.assertEqual(self.run_cli("delete", "coder7")[0], 0)  # gone: idempotent

    def test_hook_reports_skipped_secret_files(self):
        work, _ = make_repo(self.tmp.name, "r", dirty=True)
        self.assertEqual(end_hook.run(work, "coder7")["skipped"], [".env"])

    def test_reaper_keeps_a_pod_whose_push_failed(self):
        work, _ = make_repo(self.tmp.name, "r", dirty=True)
        subprocess.run(["git", "-C", work, "remote", "set-url", "origin", os.path.join(self.tmp.name, "gone.git")], check=True)
        self.patch_state(hook_env={"FLEET_WORKDIR": work, "FLEET_WORKER": "coder7"})
        self.create("coder7", "--hard", "1h")
        self.set_activity("coder7", True, T0)
        self.clock.advance(hours=2)
        res = cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()
        self.assertEqual(res, [("coder7", "hard limit", 1)])
        self.assertIsNotNone(self.pod())
        self.assertEqual(self.run_cli("reap")[0], 1)


class U1Scope(unittest.TestCase):
    def test_only_new_paths_under_cloud_launcher_and_bin(self):
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, capture_output=True, text=True).stdout.strip()
        base = subprocess.run(["git", "merge-base", "HEAD", "origin/main"], cwd=root, capture_output=True, text=True)
        if base.returncode != 0:
            self.skipTest("no origin/main to diff against")
        diff = subprocess.run(["git", "diff", "--name-status", base.stdout.strip()], cwd=root, capture_output=True, text=True).stdout
        for line in diff.splitlines():
            status, path = line.split("\t", 1)
            if path.startswith("docs/cloud-worker-interface.md"):
                continue  # the contract itself is another PR
            self.assertEqual(status, "A", line)
            self.assertTrue(path.startswith("cloud/") or path == "bin/test-cloud-launcher", line)


if __name__ == "__main__":
    unittest.main()
