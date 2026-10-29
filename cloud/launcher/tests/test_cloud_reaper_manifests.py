"""Manifests of the in-cluster reaper and its token service (ask #1582): the rights match the listed pairs, the workload is one
restarting replica with no key, and each guard has a mutant that is asserted applied. Nothing is applied to a cluster."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_cw_minter_manifest import parse  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
REAPER = os.path.join(ROOT, "cloud", "k8s", "cw-cloud-reaper.yaml")
SERVICE = os.path.join(ROOT, "cloud", "k8s", "cw-token-service.yaml")
# The (verb, resource) pairs of the reaper's pass, written from the code: list and read pods, stop through the end hook (exec), delete,
# patch a per-run Secret, hold the lease. test_cloud_reaper_pass_pairs derives the same set from a recorded fake pass.
LISTED = {("get", "pods"), ("list", "pods"), ("patch", "pods"), ("delete", "pods"), ("create", "pods/exec"), ("get", "secrets"),
          ("patch", "secrets"), ("delete", "secrets"), ("get", "leases"), ("create", "leases"), ("update", "leases")}
FORBIDDEN = ["app key", "app-key", "app_key", "app id", "app_id", "vault", "repository token", "repo-token", "git_token",
             "personal login", "signing key", "az login"]


def read(path):
    with open(path) as f:
        return f.read()


def pairs(text):
    role = [d for d in parse(text) if d.get("kind") == "Role"][0]
    return {(v, r) for rule in role["rules"] for r in rule["resources"] for v in rule["verbs"]}


def workload_problems(text):
    docs = parse(text)
    found = []
    deps = [d for d in docs if d.get("kind") == "Deployment"]
    if len(deps) != 1:
        return ["workloads: %d" % len(deps)]
    spec = deps[0]["spec"]
    if str(spec.get("replicas")) != "1":
        found.append("replicas")
    pod = spec["template"]["spec"]
    if pod.get("restartPolicy") != "Always":
        found.append("restart rule")
    sa = pod.get("serviceAccountName")
    if sa != "cw-cloud-reaper" or sa not in [d["metadata"]["name"] for d in docs if d.get("kind") == "ServiceAccount"]:
        found.append("own service account")
    low = text.lower()
    found += ["forbidden name: " + n for n in FORBIDDEN if n in "\n".join(l for l in low.splitlines() if not l.lstrip().startswith("#"))]
    return found


class TestRightsMatch(unittest.TestCase):
    def test_rights_pairs_match_the_list(self):
        got = pairs(read(REAPER))
        self.assertEqual(got - LISTED, set())
        self.assertEqual(LISTED - got, set())

    def test_no_wildcard_and_lease_pairs_present(self):
        self.assertNotIn('"*"', read(REAPER))
        self.assertLessEqual({("get", "leases"), ("create", "leases"), ("update", "leases")}, pairs(read(REAPER)))

    def test_mutant_extra_right_is_caught(self):
        text = read(REAPER)
        mutant = text.replace('verbs: ["get", "list", "patch", "delete"]', 'verbs: ["get", "list", "patch", "delete", "create"]', 1)
        self.assertNotEqual(mutant, text)
        self.assertEqual(pairs(mutant) - LISTED, {("create", "pods")})

    def test_mutant_lease_removed_is_caught(self):
        text = read(REAPER)
        mutant = text.replace('resources: ["leases"]', 'resources: []', 1)
        self.assertNotEqual(mutant, text)
        self.assertTrue(LISTED - pairs(mutant))


class TestWorkload(unittest.TestCase):
    def test_workload_is_clean(self):
        self.assertEqual(workload_problems(read(REAPER)), [])

    def test_mutant_key_mount_is_caught(self):
        text = read(REAPER)
        mutant = text.replace("value: /var/run", "value: /etc/app-key/x /var/run", 1)
        self.assertNotEqual(mutant, text)
        self.assertEqual(len(workload_problems(mutant)), 1)

    def test_comments_say_applied_by_the_administrator(self):
        for p in (REAPER, SERVICE):
            self.assertIn("APPLIED BY THE ADMINISTRATOR, NEVER BY THIS CHANGE", read(p))


class TestTokenServiceManifest(unittest.TestCase):
    def test_service_mounts_the_key_and_policy_matches_the_reaper(self):
        self.assertNotIn("cw-app-key", read(REAPER))
        docs = parse(read(SERVICE))
        self.assertIn("cw-app-key", read(SERVICE))
        pol = [d for d in docs if d.get("kind") == "NetworkPolicy"][0]
        self.assertEqual(pol["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]["app"], "cw-cloud-reaper")
        self.assertEqual(len(pol["spec"]["ingress"][0]["from"]), 1)


if __name__ == "__main__":
    unittest.main()
