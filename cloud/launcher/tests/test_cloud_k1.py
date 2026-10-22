"""Slice K1 (#562): per-run secrets, the image pull secret, the namespace service account and the token-file login.

Scenarios S1-S3, S6-S10, U1. Offline: the fake kubectl beside this file records argv, the stdin of `create secret` and the
generated kubeconfig apart. The planted values below are synthetic.
"""
import base64, json, os, re, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

MODEL_VALUE = "planted-model-value-0123456789"
GIT_VALUE = "planted-git-value-9876543210"
TOKEN = "planted-sa-token-value-abcdef"
RBAC = os.path.join(ROOT, "cloud", "k8s", "operator-rbac.yaml")


def fd_with(value):
    f = tempfile.TemporaryFile()  # a file, not a pipe: a large value would block a pipe writer
    f.write(value.encode())
    f.flush()
    f.seek(0)
    fd = os.dup(f.fileno())
    f.close()
    return fd


class K1Base(base.Base):
    def setUp(self):
        super().setUp()
        self.env["FAKE_KUBE_CFGDUMP"] = os.path.join(self.tmp.name, "cfg.jsonl")

    def read_lines(self, path):
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [x for x in f.read().splitlines() if x]

    def calls(self):
        return [json.loads(x) for x in self.read_lines(self.env["FAKE_KUBE_CALLS"])]

    def create_with_secrets(self, worker="coder7", *extra):
        fds = [fd_with(MODEL_VALUE), fd_with(GIT_VALUE)]
        self.addCleanup(lambda: [self._close(f) for f in fds])
        return self.run_cli("create", worker, "--repo", "acme-org/widget-service", "--branch", "main",
                            "--pull-secret", "cw-pull", "--run-secret-fd", "model-key=%d" % fds[0],
                            "--run-secret-fd", "git-key=%d" % fds[1], *extra)

    @staticmethod
    def _close(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    def pod_spec(self):
        return [o for o in self.st()["applied"] if o["kind"] == "Pod"][0]


class S1RunSecrets(K1Base):
    def test_pull_secret_and_two_read_only_run_secret_files_and_no_value_anywhere(self):
        code, out, err = self.create_with_secrets()
        self.assertEqual(code, 0, err)
        spec = self.pod_spec()
        self.assertEqual(spec["spec"]["imagePullSecrets"], [{"name": "cw-pull"}])
        c = spec["spec"]["containers"][0]
        mount = [m for m in c["volumeMounts"] if m["name"] == "pw"][0]
        self.assertEqual((mount["mountPath"], mount["readOnly"]), ("/run/cw", True))
        vol = [v for v in spec["spec"]["volumes"] if v["name"] == "pw"][0]
        run = [s["secret"] for s in vol["projected"]["sources"] if s["secret"]["name"].endswith("-run")][0]
        self.assertEqual({i["path"] for i in run["items"]}, {"model-key", "git-key"})
        self.assertEqual({i["mode"] for i in run["items"]}, {0o400})
        self.assertNotIn("run-secret", " ".join(e["name"] for e in c["env"]))
        # the values reached kubectl on stdin only (positive control: they are in the recorded stdin)
        stdin = "\n".join(self.read_lines(self.state + ".stdin"))
        self.assertIn(GIT_VALUE, stdin)
        self.assertTrue(self.read_lines(self.state + ".stdin"))
        everywhere = {"spec": json.dumps(spec), "audit": json.dumps(self.audit_lines()), "stdout": out, "stderr": err,
                      "argv": json.dumps(self.calls()), "process env": json.dumps(dict(os.environ)) + json.dumps(self.env)}
        for where, text in everywhere.items():
            for v in (MODEL_VALUE, GIT_VALUE):
                self.assertNotIn(v, text, where)
        self.assertEqual(cw.scan_credentials(spec), [])

    def test_secret_is_made_from_stdin_with_both_keys_and_labelled(self):
        self.create_with_secrets()
        name = cw.pod_name("coder7") + "-run"
        st = self.st()
        self.assertEqual(set(st["secret_keys"][name]), {"model-key", "git-key"})
        self.assertEqual(st["secret_labels"][name], {"cw/pod": cw.pod_name("coder7")})
        body = "\n".join(self.read_lines(self.state + ".stdin"))
        self.assertIn(GIT_VALUE, body)  # positive control: the recorder would see a leak
        self.assertIn(base64.b64encode(MODEL_VALUE.encode()).decode(), body)  # later keys go in a patch body on stdin

    def test_bad_descriptor_or_name_or_empty_value_is_refused_before_anything_is_made(self):
        empty = fd_with("")
        self.addCleanup(self._close, empty)
        for arg in ("model-key=2", "Bad_Name=5", "server-password=5", "model-key", "model-key=%d" % empty, "model-key=999"):
            code, _, err = self.run_cli("create", "coder7", "--repo", "a/b", "--branch", "m", "--run-secret-fd", arg)
            self.assertEqual(code, 2, arg)
        self.assertEqual(self.st().get("applied"), None)
        self.assertEqual(self.st().get("secrets", {}), {})

    def test_three_keys_all_survive_and_limits_and_duplicates_are_refused(self):
        fds = [fd_with("v%d" % i) for i in range(3)]
        self.addCleanup(lambda: [self._close(f) for f in fds])
        args = sum([["--run-secret-fd", "k%d=%d" % (i, f)] for i, f in enumerate(fds)], [])
        code, _, err = self.run_cli("create", "coder7", "--repo", "a/b", "--branch", "m", *args)
        self.assertEqual(code, 0, err)
        self.assertEqual(set(self.st()["secret_keys"][cw.pod_name("coder7") + "-run"]), {"k0", "k1", "k2"})
        code, _, _ = self.run_cli("create", "coder8", "--repo", "a/b", "--branch", "m", "--run-secret-fd", "k0=5", "--run-secret-fd", "k0=6")
        self.assertEqual(code, 2)
        many = sum([["--run-secret-fd", "k%d=5" % i] for i in range(cw.MAX_RUN_SECRETS + 1)], [])
        self.assertEqual(self.run_cli("create", "coder9", "--repo", "a/b", "--branch", "m", *many)[0], 2)
        big = fd_with("x" * (cw.MAX_RUN_SECRET_BYTES + 1))
        self.addCleanup(self._close, big)
        code, _, err = self.run_cli("create", "coder9", "--repo", "a/b", "--branch", "m", "--run-secret-fd", "k0=%d" % big)
        self.assertEqual(code, 2)
        self.assertIn("cap", err)
        self.assertNotIn("coder9", " ".join(self.st()["pods"]))

    def test_failed_secret_creation_cleans_up_and_audits(self):
        self.patch_state(fail_secret=True)
        code, _, err = self.create_with_secrets()
        self.assertNotEqual(code, 0)
        self.assertEqual(self.st()["pods"], {})
        self.assertEqual(self.st().get("secrets", {}), {})
        self.assertFalse(self.audit_lines()[-1]["ok"])

    def test_run_secret_makes_the_token_endpoint_optional_but_not_otherwise(self):
        del self.env["CW_TOKEN_ENDPOINT"]
        code, _, err = self.create_with_secrets()
        self.assertEqual(code, 0, err)
        env = {e["name"] for e in self.pod_spec()["spec"]["containers"][0]["env"]}
        self.assertNotIn("CW_TOKEN_ENDPOINT", env)

    def test_pod_hardening_from_the_prototype(self):
        self.create_with_secrets()
        s = self.pod_spec()["spec"]
        self.assertEqual(s["serviceAccountName"], "cw-worker")
        self.assertIs(s["automountServiceAccountToken"], False)
        self.assertEqual((s["securityContext"]["runAsUser"], s["securityContext"]["runAsGroup"]), (1000, 1000))
        self.assertEqual(s["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
        self.assertEqual(s["containers"][0]["readinessProbe"], {"tcpSocket": {"port": 4096}})


class S2StopDelete(K1Base):
    def hook(self, dirty=False, break_origin=False):
        work, _ = base.make_repo(self.tmp.name, "r", dirty=dirty)
        if break_origin:
            subprocess.run(["git", "-C", work, "remote", "set-url", "origin", os.path.join(self.tmp.name, "gone.git")], check=True)
        self.patch_state(hook_env={"CW_WORKDIR": work, "CW_WORKER": "coder7"})

    def runs(self):
        return [k for k in self.st().get("secrets", {}) if k.endswith("-run")]

    def test_stop_deletes_the_run_secret(self):
        self.hook()
        self.assertEqual(self.create_with_secrets()[0], 0)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.run_cli("stop", "coder7")[0], 0)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.st()["secrets"], {})

    def test_delete_deletes_the_run_secret(self):
        self.assertEqual(self.create_with_secrets()[0], 0)
        self.assertEqual(self.run_cli("delete", "coder7", "--force")[0], 0)
        self.assertEqual(self.st()["secrets"], {})

    def test_failed_hook_keeps_pod_and_secret_and_exits_1(self):
        self.hook(dirty=True, break_origin=True)
        self.assertEqual(self.create_with_secrets()[0], 0)
        code, _, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 1, err)
        self.assertEqual(len(self.runs()), 1)
        self.assertIsNotNone(self.pod())

    def test_failed_create_and_interrupted_create_clean_the_run_secret(self):
        self.patch_state(never_ready=True)
        code, _, err = self.create_with_secrets("coder7", "--wait", "4")
        self.assertEqual(code, 4, err)
        self.assertEqual(self.st()["secrets"], {})
        self.assertEqual(self.st()["pods"], {})
        self.patch_state(never_ready=False)
        real = cw.Launcher._wait_ready

        def boom(self_, name, seconds):
            self.assertEqual(len(self.runs()), 1)
            raise cw.Interrupted("signal 2")
        cw.Launcher._wait_ready = boom
        self.addCleanup(setattr, cw.Launcher, "_wait_ready", real)
        code, _, err = self.create_with_secrets("coder8")
        self.assertEqual(code, 130)
        self.assertEqual(self.st()["secrets"], {})
        last = self.audit_lines()[-1]
        self.assertEqual(last["action"], "create.interrupted")
        self.assertIn("-run", last["reason"])

    def test_interrupt_during_run_secret_creation_cleans_it(self):
        real = cw.Kube.create_run_secret

        def boom(kube, name, pod, values):
            real(kube, name, pod, values)
            raise KeyboardInterrupt
        cw.Kube.create_run_secret = boom
        self.addCleanup(setattr, cw.Kube, "create_run_secret", real)
        code, _, _ = self.create_with_secrets()
        self.assertEqual(code, 130)
        self.assertEqual(self.st()["secrets"], {})
        self.assertEqual(self.audit_lines()[-1]["action"], "create.interrupted")

    def test_a_failed_pod_is_deleted_by_stop_with_its_secrets(self):
        self.assertEqual(self.create_with_secrets()[0], 0)
        st = self.st()
        st["pods"][cw.pod_name("coder7")]["status"]["phase"] = "Failed"
        self.write_state(st)
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 0, err)
        self.assertIn("Failed", err)
        self.assertIsNone(self.pod())
        self.assertEqual(self.st()["secrets"], {})

    def test_a_succeeded_pod_is_deleted_but_a_kept_failed_pod_is_not(self):
        self.assertEqual(self.create_with_secrets()[0], 0)
        st = self.st()
        pod = st["pods"][cw.pod_name("coder7")]
        pod["status"]["phase"] = "Failed"
        pod["metadata"]["annotations"]["cw/kept"] = "true"
        self.write_state(st)
        self.patch_state(hook_env={"CW_WORKDIR": os.path.join(self.tmp.name, "nowhere"), "CW_WORKER": "coder7"})
        self.run_cli("stop", "coder7")
        self.assertIsNotNone(self.pod())  # kept work is not discarded by the dead-pod branch
        st = self.st()
        st["pods"][cw.pod_name("coder7")]["metadata"]["annotations"].pop("cw/kept")
        st["pods"][cw.pod_name("coder7")]["status"]["phase"] = "Succeeded"
        self.write_state(st)
        self.assertEqual(self.run_cli("stop", "coder7")[0], 0)
        self.assertIsNone(self.pod())
        self.assertEqual(self.st()["secrets"], {})

    def test_end_hook_note_is_shown(self):
        work, _ = base.make_repo(self.tmp.name, "r", dirty=False)
        self.patch_state(hook_env={"CW_WORKDIR": work, "CW_WORKER": "coder7"})
        self.create()
        real = cw.Launcher.run_hook
        cw.Launcher.run_hook = lambda s, n: {"pushed": False, "wip_branch": None, "note": "no repository at the workdir"}
        self.addCleanup(setattr, cw.Launcher, "run_hook", real)
        code, _, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 0)
        self.assertIn("no repository at the workdir", err)


def rbac_docs(text=None):
    """The manifest as (kind, name, namespace, rules, text) per document; a small reader so no YAML library is needed."""
    if text is None:
        with open(RBAC) as f:
            text = f.read()
    docs = []
    for d in re.split(r"^---\s*$", text, flags=re.M):
        body = "\n".join(l for l in d.splitlines() if not l.lstrip().startswith("#"))
        kind = re.search(r"^kind:\s*(\S+)", body, re.M)
        if not kind:
            continue
        rules = []
        for block in re.split(r"^  - apiGroups", body, flags=re.M)[1:]:
            res = re.search(r"resources:\s*\[(.*?)\]", block)
            verbs = re.search(r"verbs:\s*\[(.*?)\]", block)
            groups = re.match(r":\s*\[(.*?)\]", block)
            rules.append({"groups": [x.strip(' "') for x in groups.group(1).split(",")], "resources": [x.strip(' "') for x in res.group(1).split(",")],
                          "verbs": [x.strip(' "') for x in verbs.group(1).split(",")]})
        name = re.search(r"^  name:\s*(\S+)", body, re.M)
        ns = re.search(r"^  namespace:\s*(\S+)", body, re.M)
        docs.append({"kind": kind.group(1), "name": name.group(1), "ns": ns.group(1) if ns else None, "rules": rules, "text": body})
    return docs


class S3S7Manifest(unittest.TestCase):
    def test_role_resources_are_exactly_the_six_and_nothing_cluster_wide_or_wild(self):
        self.check_role(rbac_docs())

    def check_role(self, docs):
        kinds = [d["kind"] for d in docs]
        self.assertNotIn("ClusterRole", kinds)
        self.assertNotIn("ClusterRoleBinding", kinds)
        roles = [d for d in docs if d["kind"] == "Role"]
        self.assertEqual(len(roles), 1)
        role = roles[0]
        self.assertEqual(role["ns"], "cw")
        self.assertEqual(sorted(r for rule in role["rules"] for r in rule["resources"]),
                         sorted(["pods", "pods/log", "pods/exec", "pods/portforward", "secrets", "statefulsets"]))
        self.assertEqual({g for rule in role["rules"] for g in rule["groups"]}, {"", "apps"})
        self.assertNotIn("*", "".join(role["text"].split()).replace('apiGroups:[""]', ""))
        self.assertEqual([r["verbs"] for r in role["rules"] if r["resources"] == ["pods/portforward"]], [["create"]])
        self.assertEqual([(r["groups"], r["verbs"]) for r in role["rules"] if r["resources"] == ["statefulsets"]],
                         [(["apps"], ["get", "list", "watch", "create", "delete"])])
        for rule in role["rules"]:
            self.assertFalse({"*", "escalate", "bind", "impersonate"} & set(rule["verbs"]))

    def test_accounts_and_binding(self):
        docs = rbac_docs()
        sas = {d["name"]: d for d in docs if d["kind"] == "ServiceAccount"}
        self.assertEqual(set(sas), {"cw-operator", "cw-worker"})
        self.assertIn("automountServiceAccountToken: false", sas["cw-worker"]["text"])
        binding = [d for d in docs if d["kind"] == "RoleBinding"]
        self.assertEqual(len(binding), 1)
        self.assertIn("name: cw-operator", binding[0]["text"])
        self.assertNotIn("cw-worker", binding[0]["text"])  # the pod's account has no role
        for d in docs:
            self.assertEqual(d["ns"], "cw", d["name"])

    def test_a_wildcard_role_would_be_caught(self):
        # discriminator: the same checks, fed a role that grants everything, fail
        text = 'kind: Role\nmetadata:\n  name: x\n  namespace: cw\nrules:\n  - apiGroups: [""]\n    resources: ["*"]\n    verbs: ["*"]\n'
        with self.assertRaises(AssertionError):
            self.check_role(rbac_docs(text))


class TokenBase(K1Base):
    def setUp(self):
        super().setUp()
        self.token_file = os.path.join(self.tmp.name, "token")
        with open(self.token_file, "w") as f:
            f.write(TOKEN)
        os.chmod(self.token_file, 0o600)
        self.env.pop("CW_KUBE_CONTEXT")
        self.env.update({"CW_CLOUD_SERVER": "https://api.example.internal:6443", "CW_CLOUD_TOKEN_FILE": self.token_file,
                         "CW_CLOUD_CA_FILE": "/home/user/ca.pem"})
        os.environ["KUBECONFIG"] = "/home/user/admin.conf"  # an administrator kubeconfig that must never be used
        self.addCleanup(os.environ.pop, "KUBECONFIG", None)

    def cfgs(self):
        return [json.loads(x) for x in self.read_lines(self.env["FAKE_KUBE_CFGDUMP"])]

    def drive(self):
        work, _ = base.make_repo(self.tmp.name, "r", dirty=False)
        self.patch_state(hook_env={"CW_WORKDIR": work, "CW_WORKER": "x"})
        outs = [self.create_with_secrets("coder7")]
        self.set_activity("coder7", False, self.clock())
        outs += [self.run_cli("status"), self.run_cli("extend", "coder7", "--hard", "+10m"), self.run_cli("reap"),
                 self.run_cli("stop", "coder7"), self.create_with_secrets("coder8"), self.run_cli("delete", "coder8", "--force")]
        return outs


class S8TokenFileLogin(TokenBase):
    def test_every_call_uses_the_generated_kubeconfig_with_only_a_token_file(self):
        outs = self.drive()
        for code, _, err in outs:
            self.assertEqual(code, 0, err)
        calls, cfgs = self.calls(), self.cfgs()
        self.assertGreater(len(calls), 15)
        self.assertEqual(len(calls), len(cfgs))
        paths = {c["path"] for c in cfgs}
        self.assertEqual(len(paths), 7)  # one generated file per launcher run (7 commands), none shared
        for argv, c in zip(calls, cfgs):
            self.assertEqual(argv[:2], ["--kubeconfig", c["path"]])
            self.assertEqual(len([a for a in argv if a.startswith("--kubeconfig")]), 1)
            self.assertFalse([a for a in argv if a.startswith("--token") or a.startswith("--server") or "admin.conf" in a])
            self.assertEqual((c["mode"], c["dirmode"]), (0o600, 0o700))
            cfg = json.loads(c["config"])
            self.assertEqual(cfg["users"], [{"name": "cw", "user": {"tokenFile": self.token_file}}])
            self.assertEqual(cfg["clusters"][0]["cluster"], {"server": "https://api.example.internal:6443", "certificate-authority": "/home/user/ca.pem"})
            self.assertNotIn(TOKEN, c["config"])
        for path in paths:
            self.assertFalse(os.path.exists(path))  # removed at exit
            self.assertFalse(os.path.exists(os.path.dirname(path)))

    def test_without_the_ca_setting_no_certificate_authority_is_written(self):
        del self.env["CW_CLOUD_CA_FILE"]
        self.assertEqual(self.run_cli("status")[0], 0)
        self.assertEqual(json.loads(self.cfgs()[0]["config"])["clusters"][0]["cluster"], {"server": "https://api.example.internal:6443"})

    def test_two_logins_are_never_mixed(self):
        for k, v in (("CW_KUBECONFIG", "/home/user/other.conf"), ("CW_KUBE_CONTEXT", "acme-cw")):
            env = dict(self.env, **{k: v})
            self.env, saved = env, self.env
            code, _, err = self.run_cli("status")
            self.env = saved
            self.assertEqual(code, 2, k)
        self.assertEqual(self.calls(), [])

    def test_without_the_token_settings_the_explicit_context_rule_is_unchanged(self):
        for k in ("CW_CLOUD_SERVER", "CW_CLOUD_TOKEN_FILE", "CW_CLOUD_CA_FILE"):
            del self.env[k]
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 2)
        self.assertIn("CW_KUBE_CONTEXT", err)
        self.assertEqual(self.calls(), [])
        self.env["CW_KUBE_CONTEXT"] = "acme-cw"
        self.assertEqual(self.run_cli("status")[0], 0)
        self.assertEqual(self.calls()[0][:2], ["--context", "acme-cw"])

    def test_platform_label_and_audit_actor_are_unchanged(self):
        import pwd
        self.assertEqual(self.create_with_secrets()[0], 0)
        self.assertEqual(self.pod_spec()["metadata"]["labels"]["azure.workload.identity/use"], "true")
        self.assertEqual({x["actor"] for x in self.audit_lines()}, {pwd.getpwuid(os.getuid()).pw_name})


class S9TokenNeverShown(TokenBase):
    def test_token_value_is_absent_from_logs_audit_specs_and_argv(self):
        outs = self.drive()
        text = json.dumps(outs) + json.dumps(self.audit_lines()) + json.dumps(self.st().get("applied")) + json.dumps(self.calls())
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN, json.dumps(self.cfgs()))
        self.assertNotIn(TOKEN, "".join(self.read_lines(self.state + ".stdin")))

    def test_a_refusal_message_does_not_carry_the_token(self):
        os.chmod(self.token_file, 0)
        self.addCleanup(os.chmod, self.token_file, 0o600)
        code, out, err = self.run_cli("status")
        self.assertNotIn(TOKEN, out + err)


class S10TokenUnavailable(TokenBase):
    def assert_refused(self):
        for argv in (("status",), ("stop", "coder7"), ("create", "coder7", "--repo", "a/b", "--branch", "m"), ("reap",)):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 3, argv)
            self.assertIn("token unavailable", err)
        self.assertEqual(self.calls(), [])  # no call, so no other credential tried
        self.assertEqual(self.cfgs(), [])

    def test_missing_token_file(self):
        os.unlink(self.token_file)
        self.assert_refused()

    def test_empty_token_file(self):
        open(self.token_file, "w").close()
        self.assert_refused()

    def test_unreadable_token_file(self):
        if os.getuid() == 0:
            self.skipTest("root reads any file")
        os.chmod(self.token_file, 0)
        self.addCleanup(os.chmod, self.token_file, 0o600)
        self.assert_refused()

    def test_server_without_a_token_file_setting_refuses_and_does_not_use_the_context(self):
        del self.env["CW_CLOUD_TOKEN_FILE"]
        code, _, err = self.run_cli("status")
        self.assertEqual(code, 3)
        self.assertIn("token unavailable", err)
        self.assertEqual(self.calls(), [])


class S6Names(K1Base):
    def test_new_names_reaching_the_cloud_carry_the_prefix_and_the_check_passes(self):
        self.assertEqual(self.create_with_secrets()[0], 0)
        st, spec = self.st(), self.pod_spec()
        names = set(st["secrets"]) | set(st["secret_labels"][cw.pod_name("coder7") + "-run"]) | {spec["spec"]["serviceAccountName"]}
        names |= {i["path"] for v in spec["spec"]["volumes"] for s in v.get("projected", {}).get("sources", [])
                  for i in s["secret"]["items"] if i["path"] != "server-password"} - {"model-key", "git-key"}
        for n in names:
            self.assertTrue(n.startswith("cw") or n.endswith("-run") or n.endswith("-pw"), n)
        t = os.path.join(self.tmp.name, "list")
        with open(t, "w") as f:
            f.write("standinword\n")
        p = subprocess.run([os.path.join(ROOT, "cloud", "tests", "check-neutral-names")], env=dict(os.environ, CLOUD_BANNED_WORDS_FILE=t),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


SA_TOKEN_SHAPED = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzeXN0ZW06c2Vy.c2lnbmF0dXJl"


class M1Scrub(TokenBase):
    """Every failure path echoes each planted value on stderr AND stdout: none may reach output, audit or exception text."""
    PLANTED = [MODEL_VALUE, GIT_VALUE, TOKEN]

    def setUp(self):
        super().setUp()
        self.env["FAKE_KUBE_ECHO"] = " ".join(self.PLANTED + [GIT_VALUE[3:] + "x", "ghp_" + "a" * 36])

    def everything(self, outs):
        return json.dumps(outs) + json.dumps(self.audit_lines())

    def assert_clean(self, outs):
        text = self.everything(outs)
        for v in self.PLANTED + ["ghp_" + "a" * 36]:
            self.assertNotIn(v, text)

    def test_create_failure_paths(self):
        for cmd in ("apply", "get", "create", "patch", "label"):
            self.write_state({"pods": {}})
            for f in (self.env["FAKE_KUBE_CALLS"], self.env["CW_CLOUD_AUDIT"]):
                if os.path.exists(f):
                    os.unlink(f)
            self.patch_state(fail_cmd=cmd)
            out = self.create_with_secrets("coder7")
            self.assertNotEqual(out[0], 0, cmd)
            self.assert_clean([out])

    def test_create_cleanup_delete_failure_path(self):
        self.patch_state(never_ready=True, fail_cmd="delete")
        out = self.create_with_secrets("coder7", "--wait", "4")
        self.assertNotEqual(out[0], 0)
        self.assert_clean([out])

    def test_stop_delete_exec_annotate_status_reap_failure_paths(self):
        for cmd in ("exec", "patch", "delete", "get"):
            self.write_state({"pods": {}})
            if os.path.exists(self.env["CW_CLOUD_AUDIT"]):
                os.unlink(self.env["CW_CLOUD_AUDIT"])
            self.assertEqual(self.create_with_secrets("coder7")[0], 0)
            self.patch_state(fail_cmd=cmd)
            outs = [self.run_cli("stop", "coder7"), self.run_cli("delete", "coder7", "--force"), self.run_cli("status"), self.run_cli("reap")]
            self.assert_clean(outs)

    def test_hook_error_text_is_scrubbed_of_the_token(self):
        self.assertEqual(self.create_with_secrets("coder7")[0], 0)
        self.patch_state(fail_cmd="exec")
        code, out, err = self.run_cli("stop", "coder7")
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, out + err + json.dumps(self.audit_lines()))
        self.assertIn("[redacted]", err)

    def test_scrub_function(self):
        sec = {"abcdefgh-secret-value-123"}
        self.assertNotIn("secret-value", cw.scrub("x abcdefgh-secret-value-123 y", sec))
        self.assertNotIn("secret-value", cw.scrub("part secret-value-12 here", sec))  # a 8+ char piece
        self.assertEqual(cw.scrub("short abc", sec), "short abc")  # a short piece is not a secret
        for shaped in ("ghp_" + "b" * 30, "github_pat_" + "c" * 30, SA_TOKEN_SHAPED, "A" * 44, "0f" * 25):
            self.assertNotIn(shaped, cw.scrub("e: %s" % shaped, ()))
        self.assertLessEqual(len(cw.scrub("z " * 1000, ())), 300)
        self.assertNotIn("\n", cw.scrub("a\nb", ()))


def gen_value(n, tag):
    """A deterministic synthetic value of n characters, with characters that encode differently."""
    import hashlib
    s, i = "", 0
    while len(s) < n:
        s += hashlib.sha256(("%s%d" % (tag, i)).encode()).hexdigest()
        i += 1
    s = list(s[:n])
    for pos, ch in ((3, "+"), (5, "/"), (7, " "), (9, "\u00e9")):
        if pos < n:
            s[pos] = ch
    return "".join(s)


def encoded_pieces(value, window=8):
    """Independent of the code under test: every encoded form of the value, as 8-character windows. Base64 is taken at
    the three alignments of a stream (filler before, text after), keeping only the characters the value alone fixes."""
    raw = value.encode()
    forms = [value, raw.hex(), raw.hex().upper(), json.dumps(value)[1:-1]]
    import urllib.parse
    forms += [urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value)]
    for k in (0, 1, 2):
        for enc in (base64.b64encode(b"\xa5" * k + raw + b"Z" * 5).decode(),
                    base64.urlsafe_b64encode(b"\x5a" * k + raw + b"Q" * 4).decode()):
            forms.append(enc[4:-4])
    forms += [base64.b64encode(raw).decode(), base64.b64encode(raw).decode().rstrip("="),
              base64.urlsafe_b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("=")]
    return {f[i:i + window] for f in forms for i in range(max(0, len(f) - window + 1)) if len(f) >= window}


class M3EncodedForms(K1Base):
    """The base64, hex, percent and JSON-escaped forms of a secret, and the pieces of them, never show (kubectl prints
    Secret data in base64)."""

    def encoded_echo(self, values):
        parts = []
        for v in values:
            raw = v.encode()
            parts += [v, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode().rstrip("="), raw.hex(),
                      raw.hex().upper(), json.dumps(v)[1:-1]]
            for k in (1, 2):
                parts.append(base64.b64encode(b"x" * k + raw + b"y").decode()[4:-4])
        yaml = "apiVersion: v1\\ndata:\\n" + "".join("  k%d: %s\\n" % (i, base64.b64encode(v.encode()).decode()) for i, v in enumerate(values))
        return " ".join(parts) + " " + yaml

    def run_length(self, n):
        a, r, g = gen_value(n, "a"), gen_value(n, "r"), gen_value(n, "g")
        model = json.dumps({"access": a, "refresh": r, "type": "oauth"})
        self.env["FAKE_KUBE_ECHO"] = self.encoded_echo([model, a, r, g])
        pieces = set()
        for v in (model, a, r, g):
            pieces |= encoded_pieces(v)
        fds = [fd_with(model), fd_with(g)]
        self.addCleanup(lambda: [self._close(f) for f in fds])

        def create(w):
            return self.run_cli("create", w, "--repo", "acme-org/widget-service", "--branch", "main", "--pull-secret", "cw-pull",
                                "--run-secret-fd", "model-key=%d" % fds[0], "--run-secret-fd", "git-key=%d" % fds[1])

        def check(outs):
            text = "\n".join(str(x) for o in outs for x in o) + "\n" + "\n".join(self.read_lines(self.env["CW_CLOUD_AUDIT"]))
            text = text + "\n" + json.dumps(outs)
            for p in pieces:
                self.assertNotIn(p, text, "length %d: an encoded piece shows" % n)
        return create, check

    def test_each_encoded_form_is_gone_in_create_failures(self):
        for n in (12, 24, 39, 200):
            for cmd in ("apply", "get", "create", "patch", "label"):
                self.write_state({"pods": {}})
                if os.path.exists(self.env["CW_CLOUD_AUDIT"]):
                    os.unlink(self.env["CW_CLOUD_AUDIT"])
                create, check = self.run_length(n)
                self.patch_state(fail_cmd=cmd)
                out = create("coder7")
                self.assertNotEqual(out[0], 0)
                check([out])

    def test_forms_are_registered(self):
        forms = cw.secret_forms('{"access": "abcdefgh1234", "type": "oauth"}')
        for f in ("abcdefgh1234", base64.b64encode(b"abcdefgh1234").decode(), b"abcdefgh1234".hex().upper()):
            self.assertIn(f, forms)
        self.assertNotIn("access", forms)  # a key name is not a secret
        for sec in forms:
            self.assertTrue(sec)
        text = "data: " + base64.b64encode(b"xx" + b"abcdefgh1234" + b"yy").decode()
        self.assertNotIn(base64.b64encode(b"xx" + b"abcdefgh1234" + b"yy").decode()[4:-4], cw.scrub(text, forms))


class M3TokenEncodedForms(TokenBase):
    """The same for the service account's token, which every command knows: stop, delete, status and reap, with exec and get
    failing, print each encoded form of it."""

    def test_each_encoded_form_of_the_token_is_gone(self):
        for n in (12, 24, 39, 200):
            tok = gen_value(n, "t")
            with open(self.token_file, "w") as f:
                f.write(tok)
            self.env["FAKE_KUBE_ECHO"] = M3EncodedForms.encoded_echo(None, [tok])
            pieces = encoded_pieces(tok)
            for cmd in ("exec", "get", "delete", "patch"):
                self.write_state({"pods": {}})
                if os.path.exists(self.env["CW_CLOUD_AUDIT"]):
                    os.unlink(self.env["CW_CLOUD_AUDIT"])
                self.assertEqual(self.create_with_secrets("coder7")[0], 0)
                self.patch_state(fail_cmd=cmd)
                outs = [self.run_cli("stop", "coder7"), self.run_cli("delete", "coder7", "--force"), self.run_cli("status"),
                        self.run_cli("reap")]
                text = "\n".join(str(x) for o in outs for x in o) + "\n" + json.dumps(outs) + "\n" + \
                       "\n".join(self.read_lines(self.env["CW_CLOUD_AUDIT"]))
                for p in pieces:
                    self.assertNotIn(p, text, "token length %d: an encoded piece shows" % n)


class M2CreateAndDeleteFailureSaysSo(K1Base):
    def hook_ok(self, tag="r"):
        work, _ = base.make_repo(self.tmp.name, tag, dirty=False)
        self.patch_state(hook_env={"CW_WORKDIR": work, "CW_WORKER": "coder7"})

    def delete_cmd(self, name):
        return "%s --context acme-cw -n cw delete secret %s" % (self.env["CW_KUBECTL"], name)

    def test_failed_create_with_cleanup_ok_says_cleaned_once_in_audit_and_output(self):
        self.patch_state(never_ready=True)
        code, _, err = self.create_with_secrets("coder7", "--wait", "4")
        run = cw.pod_name("coder7") + "-run"
        phrase = "create failed; per-run secret %s cleaned" % run
        self.assertEqual(code, 4)
        self.assertEqual(err.count(phrase), 1)
        creates = [x for x in self.audit_lines() if x["action"] == "create"]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0]["reason"].count(phrase), 1)

    def test_failed_create_with_failed_cleanup_says_remains_with_name_and_command(self):
        self.patch_state(never_ready=True, fail_cmd="delete", fail_cmd_only_run=True)
        self.env["FAKE_KUBE_ECHO"] = "noise"
        code, _, err = self.create_with_secrets("coder7", "--wait", "4")
        run = cw.pod_name("coder7") + "-run"
        self.assertEqual(code, 4)
        cmd = self.delete_cmd(run)
        for where, text in (("stderr", err), ("audit", self.audit_lines()[-1]["reason"])):
            self.assertIn("per-run secret %s REMAINS: delete it with: %s" % (run, cmd), text, where)
            self.assertNotIn(MODEL_VALUE, text)
            self.assertNotIn(GIT_VALUE, text)
        self.assertEqual(len([x for x in self.audit_lines() if x["action"] == "create"]), 1)

    def test_interrupted_create_names_the_remaining_run_secret_and_command(self):
        real = cw.Launcher._wait_ready

        def boom(self_, name, seconds):
            self.patch_state(fail_cmd="delete", fail_cmd_only_run=True)
            raise cw.Interrupted("signal 15")
        cw.Launcher._wait_ready = boom
        self.addCleanup(setattr, cw.Launcher, "_wait_ready", real)
        code, _, err = self.create_with_secrets("coder7")
        run = cw.pod_name("coder7") + "-run"
        self.assertEqual(code, 130)
        self.assertIn(self.delete_cmd(run), err)
        self.assertIn(self.delete_cmd(run), self.audit_lines()[-1]["reason"])
        self.assertIn("per-run secret %s REMAINS" % run, self.audit_lines()[-1]["reason"])

    def test_stop_and_delete_where_the_secret_delete_fails_after_the_pod_is_gone(self):
        run = cw.pod_name("coder7") + "-run"
        for argv in (("stop", "coder7"), ("delete", "coder7", "--force")):
            self.write_state({"pods": {}})
            self.hook_ok("r" + argv[0])
            self.assertEqual(self.create_with_secrets("coder7")[0], 0)
            self.patch_state(fail_cmd="delete", fail_cmd_only_run=True)
            self.env["FAKE_KUBE_ECHO"] = "noise"
            code, _, err = self.run_cli(*argv)
            self.assertNotEqual(code, 0, argv)
            self.assertIsNone(self.pod())  # the pod really is gone
            self.assertIn("REMAINS: secret/%s" % run, err)
            self.assertIn(self.delete_cmd(run), err)
            last = self.audit_lines()[-1]
            self.assertFalse(last["ok"])
            self.assertIn(self.delete_cmd(run), last["reason"])
            self.assertEqual(last["reason"].count("REMAINS: secret/%s" % run), 1)


if __name__ == "__main__":
    unittest.main()
