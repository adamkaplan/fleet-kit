"""#1157: a cloud pod can run the orchestrator agent. Offline: the fake kubectl beside this file is the cluster; the entrypoint runs with stand-in files.
Covers: the worker default unchanged, the selection, the refusals before any call, the exact environment names, the per-pod channel secret,
the hub registry (launch, stop, refused write), the entrypoint, the copies' drift, and mutants of the allow-lists."""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402
from test_cloud_worker import Base  # noqa: E402

REGISTRY = "cloud-hub-registry"
WORKER_ENV = {"CW_WORKER", "CW_REPO", "CW_BRANCH", "CW_IDLE_SECONDS", "CW_HARD_DEADLINE", "CW_AUDIT_RUNTIME_ID", "CW_TOKEN_ENDPOINT"}
NEW_NAMES = {"CW_AGENT", "FLEET_AGENT_NAME", "FLEET_BOSS", "FLEET_CLOUD_CHANNEL_URL", "FLEET_CLOUD_TOKEN_FILE"}
WORKER_VOLUMES = ["pw", "tokens", "scratch"]
OTHER = {"other-agent": "f" * 64}


class Orchestrator(Base):
    def setUp(self):
        super().setUp()
        self.patch_state(secrets={REGISTRY: 1, "cloud-hub-pw": 1}, secret_bodies={"cloud-hub-pw": {"keep": "kept-pw"}, REGISTRY: {"secrets.json": json.dumps(OTHER), "agents.json": json.dumps({"other-agent": {"url": "u"}})}},
                         hook_result={"pushed": False, "wip_branch": None})

    def launch(self, worker="orch1", *extra, expect=0):
        code, out, err = self.run_cli("create", worker, "--repo", "acme-org/widget-service", "--branch", "main", *extra)
        self.assertEqual(code, expect, err)
        return code, out, err

    def orch(self, worker="orch1"):
        return self.launch(worker, "--agent", "orchestrator", "--boss", "boss1")

    def env_of(self, worker="orch1"):
        return {e["name"]: e["value"] for e in self.pod(worker)["spec"]["containers"][0]["env"]}

    def registry(self):
        bodies = self.st()["secret_bodies"][REGISTRY]
        return json.loads(bodies["secrets.json"]), json.loads(bodies["agents.json"])

    def channel_secret(self, worker="orch1"):
        return self.st()["secret_bodies"][cw.pod_name(worker) + "-run"]["channel-secret"]

    def calls(self):
        with open(self.env["FAKE_KUBE_CALLS"]) as f:
            return f.read()

    # DW-2, U1: the worker default is the spec it always was
    def test_u1_the_worker_default_adds_no_name_volume_or_key(self):
        for extra in ((), ("--agent", "worker")):
            with self.subTest(extra=extra):
                self.write_state({"pods": {}})
                self.launch("coder7", *extra)
                spec = self.pod("coder7")["spec"]
                self.assertEqual({e["name"] for e in spec["containers"][0]["env"]}, WORKER_ENV)
                self.assertEqual([v["name"] for v in spec["volumes"]], WORKER_VOLUMES)
                self.assertFalse({"hostname", "subdomain"} & set(spec))
                self.assertNotIn(cw.pod_name("coder7") + "-run", self.st().get("secrets", {}))
                self.assertNotIn(REGISTRY, self.calls().replace('"get", "secret"', ""))

    # DW-3, S4, S6
    def test_s4_the_orchestrator_gets_exactly_the_five_names(self):
        self.orch()
        env = self.env_of()
        self.assertEqual(set(env) - WORKER_ENV, NEW_NAMES)
        self.assertEqual(len([n for n in env if n.startswith("FL" + "EET_")]), 4)
        self.assertEqual(env["CW_AGENT"], "orchestrator")
        self.assertEqual((env["FLEET_AGENT_NAME"], env["FLEET_BOSS"]), ("orch1", "boss1"))
        self.assertEqual(env["FLEET_CLOUD_CHANNEL_URL"], cw.DEFAULT_CHANNEL_URL)
        self.assertEqual(env["FLEET_CLOUD_CHANNEL_URL"], "http://cloud-hub.cw.svc:8443/message")
        self.assertTrue(env["FLEET_CLOUD_CHANNEL_URL"].endswith("/message"))
        self.assertEqual(env["FLEET_CLOUD_TOKEN_FILE"], "/run/cw-tmp/channel-token")

    def test_the_channel_address_is_the_settings_row(self):
        self.env["CW_HUB_CHANNEL_URL"] = "http://hub.example.internal:9000"
        self.orch()
        self.assertEqual(self.env_of()["FLEET_CLOUD_CHANNEL_URL"], "http://hub.example.internal:9000")

    def test_an_orchestrator_needs_a_boss(self):
        code, _, err = self.run_cli("create", "orch1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator")
        self.assertEqual(code, 2, err)
        self.assertIn("--boss", err)
        self.assertIsNone(self.pod("orch1"))

    # DW-4, S5
    def test_s5_another_agent_is_refused_before_any_cluster_call(self):
        for bad in ("coder", "boss", "Orchestrator", "worker,orchestrator"):
            with self.subTest(bad=bad):
                code, _, err = self.run_cli("create", "x1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", bad, "--boss", "b")
                self.assertEqual(code, 2, err)
                self.assertIn("--agent", err)
                self.assertFalse(os.path.exists(self.env["FAKE_KUBE_CALLS"]), "a cluster call was made")
        self.assertEqual(self.st()["pods"], {})

    def test_s5_the_wiring_hands_over_only_an_orchestrator_agent(self):
        text = open(os.path.join(ROOT, "bin", "cloud_worker.py")).read()
        body = text[text.index("def _launch("):]
        self.assertIn('if args.agent == "orchestrator":', body)
        self.assertNotIn("CLOUD_AGENTS", text)   # any other --agent stays the local role name, only recorded: the worker launch is unchanged

    # DW-5, S7
    def test_s7_the_channel_secret_is_one_random_key_in_the_memory_directory(self):
        self.orch("orch1")
        self.orch("orch2")
        a, b = self.channel_secret("orch1"), self.channel_secret("orch2")
        self.assertRegex(a, r"^[0-9a-f]{64}$")
        self.assertNotEqual(a, b)
        self.assertEqual(set(self.st()["secret_bodies"][cw.pod_name("orch1") + "-run"]), {"channel-secret"})
        spec = self.pod("orch1")["spec"]
        self.assertEqual([v["name"] for v in spec["volumes"]], WORKER_VOLUMES)
        mounts = {m["name"]: m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
        self.assertTrue(self.env_of()["FLEET_CLOUD_TOKEN_FILE"].startswith(mounts["tokens"] + "/"))
        items = spec["volumes"][0]["projected"]["sources"][1]["secret"]["items"]
        self.assertEqual([i["key"] for i in items], ["channel-secret"])
        self.assertEqual(items[0]["mode"], 0o400)

    def test_s15_the_secret_is_nowhere_but_the_per_run_secret(self):
        fixed = "c4" * 32
        old = cw.secrets.token_hex
        cw.secrets.token_hex = lambda n=32: fixed if n == 32 else old(n)
        self.addCleanup(setattr, cw.secrets, "token_hex", old)
        _, out, err = self.orch()
        pwd = self.st()["secret_bodies"][cw.pod_name("orch1") + "-pw"]["password"]
        _, sout, serr = self.run_cli("stop", "orch1", "--no-record")
        _, fout, ferr = self.run_cli("create", "orch1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "boss1")
        self.patch_state(fail_apply=True)
        _, fout2, ferr2 = self.run_cli("create", "orch3", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "boss1")
        with open(self.env["CW_CLOUD_AUDIT"]) as f:
            audit = f.read()
        for where in (out, err, sout, serr, fout, ferr, fout2, ferr2, self.calls(), audit, json.dumps(self.registry())):
            self.assertNotIn(fixed, where)
            self.assertNotIn(pwd, where)
        self.assertEqual(self.st()["secret_bodies"][cw.pod_name("orch1") + "-run"]["channel-secret"], fixed)

    # S12, S13, S14
    def test_s12_the_pod_is_registered_by_the_hash_alone(self):
        self.orch()
        sec, agents = self.registry()
        self.assertEqual(sec, dict(OTHER, orch1=hashlib.sha256(self.channel_secret().encode()).hexdigest()))
        self.assertEqual(len(sec["orch1"]), 64)
        self.assertEqual(set(agents["orch1"]), {"url", "session", "password_file"})
        self.assertEqual(agents["other-agent"], {"url": "u"})
        self.assertEqual(agents["orch1"]["password_file"], "/etc/hub/pw/orch1")
        pw = self.st()["secret_bodies"]["cloud-hub-pw"]
        self.assertEqual(set(pw), {"keep", "orch1"})
        self.assertEqual(pw["orch1"], self.st()["secret_bodies"][cw.pod_name("orch1") + "-pw"]["password"])

    def test_s13_a_stop_removes_both_entries_and_leaves_the_others(self):
        self.orch()
        code, _, err = self.run_cli("stop", "orch1", "--no-record")
        self.assertEqual(code, 0, err)
        sec, agents = self.registry()
        self.assertEqual((sec, agents), (OTHER, {"other-agent": {"url": "u"}}))
        self.assertEqual(self.st()["secret_bodies"]["cloud-hub-pw"], {"keep": "kept-pw"})

    def test_s14_a_refused_registration_refuses_the_launch_and_removes_what_it_made(self):
        self.patch_state(secrets={}, secret_bodies={})   # no registry Secret: the write is refused
        code, _, err = self.run_cli("create", "orch1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "boss1")
        self.assertNotEqual(code, 0)
        self.assertIn("registry", err)
        st = self.st()
        self.assertEqual((st["pods"], st["secrets"]), ({}, {}))

    def test_a_launch_that_fails_after_registering_removes_the_entries(self):
        self.patch_state(never_ready=True)
        code, _, err = self.run_cli("create", "orch1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "orchestrator", "--boss", "boss1", "--wait", "1")
        self.assertNotEqual(code, 0, err)
        sec, agents = self.registry()
        self.assertEqual((sec, list(agents)), (OTHER, ["other-agent"]))
        self.assertEqual(self.st()["pods"], {})

    # U2: no new token role: the service account and the token endpoint are the worker's
    def test_u2_the_pod_keeps_the_workers_account_and_token_names(self):
        self.orch()
        self.launch("coder7")
        a, b = self.pod("orch1")["spec"], self.pod("coder7")["spec"]
        for key in ("serviceAccountName", "automountServiceAccountToken", "securityContext"):
            self.assertEqual(a[key], b[key])
        self.assertEqual(self.env_of()["CW_TOKEN_ENDPOINT"], self.env_of("coder7")["CW_TOKEN_ENDPOINT"])

    # S6, S9: mutants
    def check_exact(self, spec):
        names = {e["name"] for e in spec["spec"]["containers"][0]["env"]}
        self.assertEqual(len(names - WORKER_ENV), 5)
        self.assertEqual(names - WORKER_ENV, NEW_NAMES)
        self.assertEqual([v["name"] for v in spec["spec"]["volumes"]], WORKER_VOLUMES)

    def test_s6_the_check_passes_the_real_spec_and_kills_each_mutant(self):
        self.orch()
        real = self.pod("orch1")
        self.check_exact(real)
        extra = json.loads(json.dumps(real))
        extra["spec"]["containers"][0]["env"].append({"name": "CW_EXTRA_NAME", "value": "x"})
        volume = json.loads(json.dumps(real))
        volume["spec"]["volumes"].append({"name": "sa", "projected": {}})
        for mutant in (extra, volume):
            with self.assertRaises(AssertionError):
                self.check_exact(mutant)

    def test_s6_mutant_an_allow_list_that_admits_any_agent_is_caught(self):
        old = cw.AGENTS
        cw.AGENTS = old + ("coder",)
        self.addCleanup(setattr, cw, "AGENTS", old)
        code, _, _ = self.run_cli("create", "x1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "coder", "--boss", "b")
        self.assertNotEqual(code, 2, "the mutant must differ from the real refusal")   # the refusal test above would then fail on it
        cw.AGENTS = old
        self.write_state({"pods": {}})
        os.remove(self.env["FAKE_KUBE_CALLS"])
        code, _, _ = self.run_cli("create", "x1", "--repo", "acme-org/widget-service", "--branch", "main", "--agent", "coder", "--boss", "b")
        self.assertEqual(code, 2)

    def test_s12_mutant_storing_the_secret_itself_is_caught(self):
        self.orch()
        sec, _ = self.registry()
        self.assertNotEqual(sec["orch1"], self.channel_secret())


class Entrypoint(unittest.TestCase):
    def run_entry(self, agent):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = tmp.name
        share, run, tmpdir = (os.path.join(d, x) for x in ("share", "run", "tmp"))
        for p in (share, run, tmpdir):
            os.mkdir(p)
        for name, text in (("worker.md", "worker agent\n"), ("orchestrator.md", "---\npermission:\n  question: allow\n---\norchestrator agent\n"), ("opencode-worker.json", "{\n}\n")):
            open(os.path.join(share, name), "w").write(text)
        with open(os.path.join(run, "channel-secret"), "w") as f:
            f.write("c" * 64)
        marker = os.path.join(d, "server-ran")
        env = {"PATH": os.environ["PATH"], "HOME": d, "WORKER_DOCKER": "0", "CW_RUN_DIR": run, "CW_TMP_DIR": tmpdir, "CW_SHARE_DIR": share}
        if agent is not None:
            env["CW_AGENT"] = agent
        p = subprocess.run(["sh", os.path.join(ROOT, "cloud", "worker", "entrypoint.sh"), "sh", "-c", "echo ran > " + marker], env=env, capture_output=True, text=True)
        agents = os.path.join(tmpdir, "config", "opencode", "agents")
        return p, sorted(os.listdir(agents)) if os.path.isdir(agents) else [], os.path.exists(marker), tmpdir

    def test_s2_unset_writes_the_worker_definition(self):
        p, files, ran, tmpdir = self.run_entry(None)
        self.assertEqual((p.returncode, files, ran), (0, ["worker.md"], True), p.stderr)

    def test_s2_orchestrator_writes_its_definition_and_installs_the_secret(self):
        p, files, ran, tmpdir = self.run_entry("orchestrator")
        self.assertEqual((p.returncode, files, ran), (0, ["orchestrator.md"], True), p.stderr)
        path = os.path.join(tmpdir, "channel-token")
        self.assertEqual(open(path).read(), "c" * 64)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o400)
        self.assertFalse(os.path.islink(path))
        self.assertNotIn("c" * 64, p.stdout + p.stderr)

    def test_s2_a_hub_worker_installs_the_channel_secret_when_the_launcher_wrote_it(self):
        _, _, _, tmpdir = self.run_entry("worker")
        self.assertEqual(open(os.path.join(tmpdir, "channel-token")).read(), "c" * 64)

    def test_s2_any_other_value_refuses_before_the_server(self):
        for bad in ("boss", "Orchestrator", "worker;x", " "):
            p, files, ran, _ = self.run_entry(bad)
            self.assertEqual(p.returncode, 1, bad)
            self.assertIn("CW_AGENT", p.stderr)
            self.assertFalse(ran)
            self.assertEqual(files, [])


# S3: each copy under cloud/worker equals its source when the source is in this checkout; a missing source is skipped by name
COPIES = {"bin/cloud_cli.py": "cloud/worker/bin/cloud_cli.py", "bin/cloud_client.py": "cloud/worker/bin/cloud_client.py",
          "agents/opencode/orchestrator.md": "cloud/worker/agents/orchestrator.md", "bin/cloud_hub.py": "cloud/worker/bin/cloud_hub.py",
          "bin/cloud_channel.py": "cloud/worker/bin/cloud_channel.py", "bin/cloud_channel_server.py": "cloud/worker/bin/cloud_channel_server.py",
          "bin/cloud_relay.py": "cloud/worker/bin/cloud_relay.py"}


def drift(root, source, copy):
    """None when equal; a sentence when they differ. The source is read from `root`; the caller decides what a missing source means."""
    with open(os.path.join(root, source), "rb") as a, open(os.path.join(root, copy), "rb") as b:
        return None if a.read() == b.read() else "%s differs from %s" % (copy, source)


class Drift(unittest.TestCase):
    def test_s3_each_copy_equals_its_source(self):
        for source, copy in COPIES.items():
            with self.subTest(copy=copy):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, copy)), copy + " is missing")
                if not os.path.isfile(os.path.join(ROOT, source)):
                    self.skipTest("%s is not in this checkout (it lands on the integration branch): the drift of %s is not checked here" % (source, copy))
                self.assertIsNone(drift(ROOT, source, copy))

    def test_s3_a_one_byte_edit_is_caught(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "a"), "wb").write(b"same bytes")
            open(os.path.join(d, "b"), "wb").write(b"same bytes")
            self.assertIsNone(drift(d, "a", "b"))
            open(os.path.join(d, "b"), "wb").write(b"same bytez")
            self.assertIn("differs", drift(d, "a", "b"))

    def test_the_dockerfile_copies_each_file(self):
        text = open(os.path.join(ROOT, "cloud", "worker", "Dockerfile")).read()
        for needle in ("worker/agents/orchestrator.md", "worker/bin/cloud_cli.py /usr/local/bin/cw-cloud", "worker/bin/cloud_client.py",
                       "worker/bin/cloud_hub.py", "worker/bin/cloud_channel.py", "worker/bin/cloud_channel_server.py", "worker/bin/cloud_relay.py"):
            self.assertIn(needle, text)


class HubForEveryLaunch(Orchestrator):
    """#1157 clause 8: with the hub setting set, a worker and an orchestrator are both registered."""
    def setUp(self):
        super().setUp()
        self.env["CW_HUB_CHANNEL_URL"] = "http://hub.example.internal:9000"

    for _n in [n for n in dir(Orchestrator) if n.startswith("test_")]:
        locals()[_n] = None   # only this class's own tests run here

    def worker(self, name="wk1", *extra):
        return self.launch(name, *extra)

    def test_s18_worker_and_orchestrator_both_registered(self):
        self.worker("wk1", "--boss", "boss1")
        self.orch("orch1")
        sec, agents = self.registry()
        self.assertEqual(set(agents) - set(OTHER), {"wk1", "orch1"})
        self.assertEqual(len(set(sec) - set(OTHER)), 2)
        for w in ("wk1", "orch1"):
            self.assertIn("channel-secret", self.st()["secret_bodies"][cw.pod_name(w) + "-run"])
            env = self.env_of(w)
            self.assertEqual({n for n in env if n.startswith("FL" + "EET_")}, {"FLEET_AGENT_NAME", "FLEET_BOSS", "FLEET_CLOUD_CHANNEL_URL", "FLEET_CLOUD_TOKEN_FILE"})
        self.assertEqual(self.env_of("wk1")["CW_AGENT"], "worker")
        self.assertEqual(self.env_of("orch1")["CW_AGENT"], "orchestrator")
        self.assertEqual(sec["wk1"], hashlib.sha256(self.channel_secret("wk1").encode()).hexdigest())

    def hub_accepts(self, worker):
        """Feed the registered record to the hub's OWN validator (the copy under cloud/worker/bin): True when it would deliver."""
        sys.path.insert(0, os.path.join(ROOT, "cloud", "worker", "bin"))
        try:
            import cloud_hub
        finally:
            sys.path.pop(0)
        got = []

        class Probe(cloud_hub.Inbox):
            def _post(self, parts, session, record, item):
                got.append(parts.hostname)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "agents.json")
            record = dict(self.registry()[1][worker], session="ses_abc123")
            with open(path, "w") as f:
                json.dump({worker: record}, f)
            try:
                return Probe(path).put_if_absent(worker, "k", {"sender": "s", "key": "k", "text": "t"}) and bool(got)
            except ValueError:
                return False

    def test_s19_the_url_is_the_in_cluster_name_the_hub_accepts(self):
        self.worker("wk1", "--boss", "boss1")
        self.orch("orch1")
        _, agents = self.registry()
        for w in ("wk1", "orch1"):
            self.assertEqual(agents[w]["url"], "http://%s.cw-agents.cw.svc.cluster.local:4096" % cw.pod_name(w))
            self.assertTrue(self.hub_accepts(w), w)
            spec = self.pod(w)["spec"]
            self.assertEqual((spec["hostname"], spec["subdomain"]), (cw.pod_name(w), "cw-agents"))

    def test_s19_the_namespace_comes_from_the_setting(self):
        self.assertEqual(cw.hub_agent_url("p1", "acme"), "http://p1.cw-agents.acme.svc.cluster.local:4096")

    def test_s19_the_hub_refuses_the_old_forms(self):   # kills: a mutant that registers the pod IP's form, or a dotted name that is not .svc
        orig = cw.hub_agent_url
        try:
            for url in ("http://10.1.2.3:4096", "http://worker-x.example.internal:4096", "http://p1.cw-agents.cw.example.com:4096", "http://p1.cw-agents.cw.svc.cluster.example:4096"):
                with self.subTest(url=url):
                    w = "wk%d" % (9 + len(url) % 7)
                    cw.hub_agent_url = lambda n, ns, u=url: u
                    self.worker(w, "--boss", "boss1")
                    self.assertEqual(self.registry()[1][w]["url"], url)
                    self.assertFalse(self.hub_accepts(w))
        finally:
            cw.hub_agent_url = orig

    def test_s19_the_stack_allow_list_admits_exactly_the_two_keys(self):
        import test_cloud_stack as ts
        pod = ts.build(ts._args(stack=True))
        self.assertFalse(cw._stack_rest_differs(pod))
        name = pod["metadata"]["name"]
        pod["spec"].update(hostname=name, subdomain="cw-agents")
        self.assertFalse(cw._stack_rest_differs(pod))
        for h, sd in ((name, "other"), ("h", "cw-agents")):
            bad = json.loads(json.dumps(pod))
            bad["spec"].update(hostname=h, subdomain=sd)
            self.assertTrue(cw._stack_rest_differs(bad), (h, sd))
        bad = json.loads(json.dumps(pod))
        del bad["spec"]["hostname"]
        self.assertTrue(cw._stack_rest_differs(bad))
        self.assertEqual(cw.STACK_SPEC_KEYS & {"hostname", "subdomain"}, {"hostname", "subdomain"})

    def test_s20_the_session_verb_writes_the_id(self):
        self.worker("wk1", "--boss", "boss1")
        self.assertEqual(self.registry()[1]["wk1"]["session"], "")
        code, out, err = self.run_cli("hub-session", "wk1", "--session", "ses_abc123", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registry()[1]["wk1"]["session"], "ses_abc123")
        self.assertEqual(self.registry()[1]["wk1"]["url"], "http://%s.cw-agents.cw.svc.cluster.local:4096" % cw.pod_name("wk1"))
        self.assertEqual(self.registry()[1]["other-agent"], {"url": "u"})

    def test_s21_stop_removes_both_entries_of_a_worker(self):
        self.worker("wk1", "--boss", "boss1")
        code, _, err = self.run_cli("stop", "wk1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.registry(), (OTHER, {"other-agent": {"url": "u"}}))
        self.assertNotIn("wk1", self.st()["secret_bodies"]["cloud-hub-pw"])

    def test_s21_a_failed_launch_removes_the_entries(self):
        self.patch_state(never_ready=True)
        code, _, err = self.run_cli("create", "wk1", "--repo", "acme-org/widget-service", "--branch", "main", "--boss", "boss1", "--wait", "1")
        self.assertNotEqual(code, 0)
        self.assertEqual(self.registry(), (OTHER, {"other-agent": {"url": "u"}}))
        self.assertIsNone(self.pod("wk1"))

    def test_s22_nothing_cluster_wide_is_written(self):
        self.worker("wk1", "--boss", "boss1")
        self.assertFalse(re.search(r"clusterrole|namespace[^s]|configmap|serviceaccount", self.calls(), re.I), self.calls())

    def test_u3_unset_is_the_worker_default(self):
        del self.env["CW_HUB_CHANNEL_URL"]
        self.launch("coder7")
        spec = self.pod("coder7")["spec"]
        self.assertEqual({e["name"] for e in spec["containers"][0]["env"]}, WORKER_ENV)
        self.assertEqual([v["name"] for v in spec["volumes"]], WORKER_VOLUMES)
        self.assertEqual(self.registry(), (OTHER, {"other-agent": {"url": "u"}}))
        self.assertNotIn(cw.pod_name("coder7") + "-run", self.st().get("secrets", {}))

    def test_mutant_dropping_the_message_path_fails(self):
        orig = cw.DEFAULT_CHANNEL_URL
        try:
            cw.DEFAULT_CHANNEL_URL = "http://cloud-hub.cw.svc:8443"   # mutant: the bare service address
            self.orch("orch1")
            self.assertFalse(self.env_of()["FLEET_CLOUD_CHANNEL_URL"].endswith("/message"))
        finally:
            cw.DEFAULT_CHANNEL_URL = orig

    def test_mutants_fail(self):
        orig_on = cw.hub_on
        try:
            cw.hub_on = lambda env: False   # mutant: only the orchestrator is registered
            self.worker("wk1", "--boss", "boss1")
            self.assertNotIn("wk1", self.registry()[1])
            cw.hub_on = orig_on
        finally:
            cw.hub_on = orig_on


class Docs(unittest.TestCase):
    def test_s10_the_docs_say_it(self):
        readme = open(os.path.join(ROOT, "cloud", "README.md")).read()
        self.assertIn("CW_AGENT", readme)
        self.assertIn("rebuild", readme)
        self.assertIn("tightening-only", readme)
        self.assertIn("CW_AGENT", open(os.path.join(ROOT, "cloud", "worker", "agents", "worker.md")).read())


class RepoRootClean(unittest.TestCase):
    def test_s13_the_fixtures_leave_no_scratch_in_the_repository(self):
        def status():
            return subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=ROOT, capture_output=True, text=True).stdout
        before = status()
        suite = unittest.TestSuite()
        loader = unittest.defaultTestLoader
        for cls in (Orchestrator, Entrypoint):
            suite.addTests(loader.loadTestsFromTestCase(cls))
        with open(os.devnull, "w") as null:
            unittest.TextTestRunner(stream=null).run(suite)
        self.assertEqual(status(), before, "scratch left in the repository root")


if __name__ == "__main__":
    unittest.main()
