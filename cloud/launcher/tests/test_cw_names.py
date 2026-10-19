"""Neutral names (#404, S5 and S6): one old->new table, no old name left, every emitted name is in the table.

The old names are assembled from a fragment so this file itself carries none of them.
"""
import json, os, re, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
import test_cloud_worker as base  # noqa: E402
cw = base.cw

P = "fl" + "eet"  # the retired prefix
# THE table: old name -> new name, for everything the launcher sends to the cluster or the pod.
NAMES = {
    P.upper() + "_KUBECTL": "CW_KUBECTL", P.upper() + "_KUBE_CONTEXT": "CW_KUBE_CONTEXT",
    P.upper() + "_KUBECONFIG": "CW_KUBECONFIG", P.upper() + "_CLOUD_NAMESPACE": "CW_CLOUD_NAMESPACE",
    P.upper() + "_CLOUD_IMAGE": "CW_CLOUD_IMAGE", P.upper() + "_CLOUD_AUDIT": "CW_CLOUD_AUDIT",
    P.upper() + "_CLOUD_SPEND_CAP_USD": "CW_CLOUD_SPEND_CAP_USD",
    P.upper() + "_WORKER": "CW_WORKER", P.upper() + "_REPO": "CW_REPO", P.upper() + "_BRANCH": "CW_BRANCH",
    P.upper() + "_IDLE_SECONDS": "CW_IDLE_SECONDS", P.upper() + "_HARD_DEADLINE": "CW_HARD_DEADLINE",
    P.upper() + "_AUDIT_RUNTIME_ID": "CW_AUDIT_RUNTIME_ID", P.upper() + "_WORKDIR": "CW_WORKDIR",
    P.upper() + "_STARTED": "CW_STARTED", P.upper() + "_TOKEN_ENDPOINT": "CW_TOKEN_ENDPOINT",
    P + "-worker": "cw-worker", P + "-workers": "cw",
    P + "/worker": "cw/worker", P + "/started": "cw/started", P + "/hard-deadline": "cw/hard-deadline",
    P + "/idle-seconds": "cw/idle-seconds", P + "/shape": "cw/shape", P + "/max-hard": "cw/max-hard",
    P + "/hook-ran": "cw/hook-ran", P + "/kept": "cw/kept",
    P + "-end-hook": "cw-end-hook", P + "-worker-activity": "cw-worker-activity", P + "-token-mint": "cw-token-mint",
}
NEW = set(NAMES.values())
STANDARD_TOOLS = {"cat", "git", "head", "ls", "sh"}   # the image's own tools, run by the record and the report read (not names this project coined)
OLD = re.compile(P, re.I)


def files():
    for top in ("cloud", "bin/test-cloud-launcher", "docs/cloud-worker-interface.md"):
        full = os.path.join(ROOT, top)
        if os.path.isfile(full):
            yield top, full
        for d, ds, fs in os.walk(full):
            ds[:] = [x for x in ds if x != "__pycache__"]
            for f in fs:
                yield os.path.relpath(os.path.join(d, f), ROOT), os.path.join(d, f)


# Platform and third-party keys the launcher sets: never renamed, never in the table above.
PLATFORM_KEYS = ["azure.workload.identity/use"]


class S5Names(base.Base):
    def test_platform_keys_are_not_renamed_and_are_on_the_pod(self):
        self.create("coder7")
        pod = [p for p in self.st()["applied"] if p["kind"] == "Pod"][0]
        keys = set(pod["metadata"]["labels"]) | set(pod["metadata"]["annotations"])
        for k in PLATFORM_KEYS:
            self.assertIn(k, keys)
            self.assertNotIn(k, NAMES)
            self.assertNotIn(k, NAMES.values())
        self.assertEqual(pod["metadata"]["labels"]["azure.workload.identity/use"], "true")

    def test_no_old_name_remains(self):
        bad = []
        for rel, p in files():
            with open(p, errors="replace") as f:
                if OLD.search(f.read()):
                    bad.append(rel)
        self.assertEqual(bad, [])

    def test_table_maps_only_to_cw_names(self):
        self.assertTrue(False, "s3 deliberate red")
        for old, new in NAMES.items():
            self.assertTrue(new.lower().startswith("cw") and not OLD.search(new), new)

    def test_every_emitted_name_is_in_the_table(self):
        self.patch_state(hook_env={"CW_WORKDIR": base.make_repo(self.tmp.name, "r", False)[0], "CW_WORKER": "x"})
        self.create("coder7")
        self.run_cli("extend", "coder7", "--hard", "+10m")
        self.run_cli("stop", "coder7")
        pod = [p for p in self.st()["applied"] if p["kind"] == "Pod"][0]
        c = pod["spec"]["containers"][0]
        emitted = {e["name"] for e in c["env"]}
        emitted |= (set(pod["metadata"]["labels"]) - {"app"} - set(PLATFORM_KEYS)) | set(pod["metadata"]["annotations"])
        emitted.add(pod["metadata"]["labels"]["app"])
        emitted.add(pod["spec"]["serviceAccountName"])
        calls = [json.loads(x) for x in open(self.env["FAKE_KUBE_CALLS"])]
        emitted |= {a[a.index("-n") + 1] for a in calls}
        emitted |= {a[a.index("--") + 1] for a in calls if "exec" in a and "--" in a}
        emitted |= {a.split("=", 1)[0] for a in sum(calls, []) if a.startswith("cw/")}
        self.assertEqual(sorted(emitted - NEW - STANDARD_TOOLS), [])
        self.assertTrue({"CW_WORKER", "cw/worker", "cw-worker", "cw", "cw-end-hook"} <= emitted, emitted)

    def test_exec_names_are_installed_by_the_image(self):
        df = os.path.join(ROOT, "cloud", "worker", "Dockerfile")
        if not os.path.exists(df):
            self.skipTest("cloud/worker/Dockerfile is not on this branch yet; the image half of this check is skipped")
        text = open(df).read()
        for tool in ("cw-end-hook", "cw-worker-activity"):
            self.assertTrue(any(tool in l and re.search(r"COPY|chmod|install", l) for l in text.splitlines()), tool)


class S6Reaper(base.Base):
    def test_reaper_stops_the_new_label_worker_and_ignores_a_pod_without_it(self):
        self.patch_state(hook_env={"CW_WORKDIR": self.clean(), "CW_WORKER": "x"})
        self.create("coder7", "--hard", "1h")
        # a pod that lacks the new label (such as one left under a retired name): never listed, never touched
        st = self.st()
        other = json.loads(json.dumps(st["pods"][cw.pod_name("coder7")]))
        other["metadata"]["name"] = "stray"
        other["metadata"]["labels"] = {"app": P + "-worker"}
        st["pods"]["stray"] = other
        self.write_state(st)
        self.set_activity("coder7", True, base.T0)
        self.clock.advance(hours=2)
        res = cw.Launcher(cw.Kube(self.env), self.env, self.clock).reap()
        self.assertEqual([r[:2] for r in res], [("coder7", "hard limit")])
        self.assertIsNone(self.pod())
        self.assertIn("stray", self.st()["pods"])
        listing = [json.loads(x) for x in open(self.env["FAKE_KUBE_CALLS"])]
        self.assertTrue(any("-l" in c and "app=cw-worker" in c for c in listing))

    def clean(self):
        return base.make_repo(self.tmp.name, "clean", dirty=False)[0]


if __name__ == "__main__":
    unittest.main()
