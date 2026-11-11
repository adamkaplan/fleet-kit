"""Ask #2402 (phase 2, W0): BOUNDARY PINS. The boundaries that remain after the convenience rules go still hold.

Each pin is a checker that returns the list of problems it finds in a spec or a constant; the real value must give none, and a
weakened copy (the mutant control) must give some. Offline: no cloud, no network, no clock. Values are read from the code and the
manifests; the one literal set is the worker's token permissions, which the owner's plan names exactly."""
import copy
import datetime as dt
import json
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, HERE)
sys.path.insert(0, LAUNCHER)
import app_token  # noqa: E402
import cloud_worker as cw  # noqa: E402
import preflight  # noqa: E402
from test_cloud_hub_network import parse as parse_yaml  # noqa: E402
from test_cloud_stack import build  # noqa: E402
from test_cloud_stack import _args  # noqa: E402

K8S = os.path.join(ROOT, "cloud", "k8s")
WORKER_ACCOUNT = "cw-worker"


def read(name):
    with open(os.path.join(K8S, name), encoding="utf-8") as f:
        return f.read()


def run_names():
    """The per-run secret keys of a real launch: the two the run task passes (read from its source), plus nothing else."""
    with open(os.path.join(LAUNCHER, "run_task.py"), encoding="utf-8") as f:
        text = f.read()
    return tuple(re.findall(r'"--run-secret-fd", "([a-z-]+)=%d"', text))


def pod(names=None):
    return build(_args(), run_names() if names is None else names)


# ---- (1) the pod spec -------------------------------------------------------------------------------------------------------
def spec_problems(p):
    s, out = p["spec"], []
    if s.get("automountServiceAccountToken") is not False:
        out.append("automountServiceAccountToken is not false")
    if (s.get("securityContext") or {}).get("runAsNonRoot") is not True:
        out.append("runAsNonRoot is not true")
    for c in s["containers"]:
        sc = c.get("securityContext") or {}
        if sc.get("capabilities", {}).get("drop") != ["ALL"] or sc.get("capabilities", {}).get("add"):
            out.append("capabilities do not drop ALL")
        if sc.get("privileged"):
            out.append("privileged")
    if not s.get("serviceAccountName"):
        out.append("no service account name")
    return out


def rbac_problems(text):
    """The worker's account has no role and no binding: no Role or ClusterRole of its name, no binding that names it as a subject."""
    out, account = [], None
    for doc in re.split(r"(?m)^---\s*$", text):
        body = "\n".join(l for l in doc.splitlines() if not l.lstrip().startswith("#"))
        kind = re.search(r"(?m)^kind:\s*(\S+)", body)
        kind = kind.group(1) if kind else ""
        if kind == "ServiceAccount" and re.search(r"(?m)^  name:\s*%s\s*$" % WORKER_ACCOUNT, body):
            account = body
        if kind in ("Role", "ClusterRole") and re.search(r"(?m)^  name:\s*%s\s*$" % WORKER_ACCOUNT, body):
            out.append("a %s is named for the worker account" % kind)
        if kind in ("RoleBinding", "ClusterRoleBinding") and re.search(r"(?m)^\s*-?\s*name:\s*%s\s*$" % WORKER_ACCOUNT, body):
            out.append("a %s binds the worker account" % kind)
    if account is None:
        out.append("the worker account is not defined")
    elif not re.search(r"(?m)^automountServiceAccountToken:\s*false\s*$", account):
        out.append("the worker account does not refuse the token automount")
    return out


def forbidden_problems(table):
    """Each shape of a refused pod must be named by the table (the real function is forbidden_pod_fields)."""
    base = pod()
    shapes = {
        "hostPath": lambda p: p["spec"]["volumes"].append({"name": "h", "hostPath": {"path": "/x"}}),
        "privileged": lambda p: p["spec"]["containers"][0]["securityContext"].update(privileged=True),
        "hostNetwork": lambda p: p["spec"].update(hostNetwork=True),
        "docker.sock": lambda p: p["spec"]["containers"][0]["volumeMounts"].append({"name": "scratch", "mountPath": "/var/run/docker.sock"}),
        "service-account token": lambda p: p["spec"]["volumes"].append(
            {"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "t", "audience": "x"}}]}}),
    }
    out = []
    with mock.patch.object(cw, "FORBIDDEN_POD_FIELDS", table):
        for name, mutate in shapes.items():
            p = copy.deepcopy(base)
            mutate(p)
            if not cw.forbidden_pod_fields(p):
                out.append("a pod with %s is not refused" % name)
    if cw.forbidden_pod_fields(base) and table is cw.FORBIDDEN_POD_FIELDS:
        out.append("the plain pod is refused")
    return out


class SpecPins(unittest.TestCase):
    def test_rendered_pod_holds(self):
        self.assertEqual(spec_problems(pod()), [])

    def test_mutants_of_the_spec_fail(self):
        for label, mutate in (("automount", lambda p: p["spec"].pop("automountServiceAccountToken")),
                              ("automount true", lambda p: p["spec"].update(automountServiceAccountToken=True)),
                              ("root", lambda p: p["spec"]["securityContext"].update(runAsNonRoot=False)),
                              ("caps", lambda p: p["spec"]["containers"][0]["securityContext"].update(capabilities={"drop": []})),
                              ("caps add", lambda p: p["spec"]["containers"][0]["securityContext"]["capabilities"].update(add=["NET_RAW"]))):
            p = pod()
            mutate(p)
            self.assertNotEqual(spec_problems(p), [], label)

    def test_worker_account_has_no_role(self):
        text = read("operator-rbac.yaml")
        self.assertEqual(rbac_problems(text), [])
        account = pod()["spec"]["serviceAccountName"]
        self.assertEqual(account, WORKER_ACCOUNT)

    def test_mutants_of_the_rbac_fail(self):
        text = read("operator-rbac.yaml")
        bind = ("\n---\napiVersion: rbac.authorization.k8s.io/v1\nkind: %s\nmetadata:\n  name: extra\nsubjects:\n"
                "  - kind: ServiceAccount\n    name: %s\n    namespace: cw\n")
        self.assertNotEqual(rbac_problems(text + bind % ("RoleBinding", WORKER_ACCOUNT)), [])
        self.assertNotEqual(rbac_problems(text + bind % ("ClusterRoleBinding", WORKER_ACCOUNT)), [])
        self.assertNotEqual(rbac_problems(text + "\n---\nkind: Role\nmetadata:\n  name: %s\n" % WORKER_ACCOUNT), [])
        self.assertNotEqual(rbac_problems(text.replace("automountServiceAccountToken: false", "automountServiceAccountToken: true")), [])

    def test_forbidden_fields_refuse(self):
        self.assertEqual(forbidden_problems(cw.FORBIDDEN_POD_FIELDS), [])

    def test_mutants_of_the_forbidden_table_fail(self):
        for name in ("hostPath", "privileged", "hostNetwork", "docker.sock", "service-account token"):
            self.assertIn(name, cw.FORBIDDEN_POD_FIELDS)
            weak = {k: v for k, v in cw.FORBIDDEN_POD_FIELDS.items() if k != name}
            self.assertNotEqual(forbidden_problems(weak), [], name)


# ---- (2) the mounted secrets ------------------------------------------------------------------------------------------------
def secret_problems(p, names):
    """The secrets of the pod: its password Secret (key password -> server-password) and the per-run Secret holding exactly `names`;
    nothing else is secret-shaped: no env valueFrom/envFrom, no other volume kind, no other mount."""
    out, s, c = [], p["spec"], p["spec"]["containers"][0]
    name = p["metadata"]["name"]
    vols = {v["name"]: v for v in s["volumes"]}
    for v in s["volumes"]:
        kinds = set(v) - {"name"}
        if v["name"] != "pw" and kinds != {"emptyDir"}:
            out.append("volume %s is not an emptyDir" % v["name"])
    plain = vols["pw"].get("secret")
    sources = (vols["pw"].get("projected") or {}).get("sources") or ([{"secret": {"name": plain["secretName"], "items": plain.get("items", [])}}] if plain else [])
    if "projected" not in vols["pw"] and "secret" not in vols["pw"]:
        out.append("the pw volume is neither a secret nor a projection")
    for src in sources:
        if set(src) != {"secret"}:
            out.append("a projected source that is not a Secret: %s" % sorted(src))
            continue
        if src["secret"]["name"] == name + "-pw":
            if [i["path"] for i in src["secret"].get("items", [])] != ["server-password"]:
                out.append("the password Secret mounts more than the server password")
        elif src["secret"]["name"] == name + "-run":
            if sorted(i["key"] for i in src["secret"]["items"]) != sorted(names):
                out.append("the run Secret mounts keys other than the named ones")
        else:
            out.append("an unnamed Secret is mounted: %s" % src["secret"]["name"])
    for e in c.get("env", []):
        if set(e) != {"name", "value"}:
            out.append("env %s has a source" % e.get("name"))
    if c.get("envFrom"):
        out.append("envFrom")
    if {m["name"] for m in c["volumeMounts"]} - set(vols):
        out.append("a mount of an undeclared volume")
    if s.get("imagePullSecrets"):
        out.append("image pull secrets on the plain pod")
    return out


class SecretPins(unittest.TestCase):
    def test_the_launch_names_git_token_model_auth_and_the_password(self):
        names = run_names()
        self.assertIn(cw.GIT_TOKEN_KEY, names)
        self.assertIn("model-auth", names)
        self.assertEqual(cw.RESERVED_RUN_NAMES, {"server-password"})

    def test_mounted_secrets_are_the_named_ones(self):
        names = run_names()
        self.assertEqual(secret_problems(pod(names), names), [])
        self.assertEqual(secret_problems(pod(()), ()), [])

    def test_mutants_of_the_secrets_fail(self):
        names = run_names()

        def extra_run_key(p):
            p["spec"]["volumes"][0]["projected"]["sources"][1]["secret"]["items"].append({"key": "vault", "path": "vault", "mode": 0o400})

        def extra_source(p):
            p["spec"]["volumes"][0]["projected"]["sources"].append({"secret": {"name": "vault-creds"}})

        def extra_volume(p):
            p["spec"]["volumes"].append({"name": "v", "secret": {"secretName": "vault"}})

        def env_secret(p):
            p["spec"]["containers"][0]["env"].append({"name": "X", "valueFrom": {"secretKeyRef": {"name": "n", "key": "k"}}})

        def env_from(p):
            p["spec"]["containers"][0]["envFrom"] = [{"secretRef": {"name": "n"}}]

        def sa_token(p):
            p["spec"]["volumes"][0]["projected"]["sources"].append({"serviceAccountToken": {"path": "t"}})

        for f in (extra_run_key, extra_source, extra_volume, env_secret, env_from, sa_token):
            p = pod(names)
            f(p)
            self.assertNotEqual(secret_problems(p, names), [], f.__name__)


# ---- (3) the git token ------------------------------------------------------------------------------------------------------
WORKER_SET = {"contents": "write", "pull_requests": "write", "issues": "read", "metadata": "read"}


def token_problems(perms, seconds, requested, target):
    out = []
    if perms != WORKER_SET:
        out.append("the worker permission set differs")
    if requested.get("permissions") != perms:
        out.append("the request does not carry exactly the set")
    if requested.get("repositories") != [target.split("/", 1)[1]]:
        out.append("the request is not for exactly one repository")
    if seconds > 61 * 60:
        out.append("the token may live longer than 61 minutes")
    return out


def minted_request(target="acme-org/widget-service"):
    """Run the real mint with the network and the vault stubbed; returns the request body it sent to GitHub."""
    seen = {}

    def gh(method, path, jwt, body=None, not_found=None, role="worker"):
        seen.update(body or {})
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"token": "t", "expires_at": expires, "permissions": dict(body["permissions"]),
                "repositories": [{"name": target.split("/", 1)[1], "full_name": target, "owner": {"id": 7}}]}

    with mock.patch.object(app_token, "vault", lambda env, name: "12345" if "id" in name else "k"), \
            mock.patch.object(app_token, "sign", lambda k, d: b"s"), \
            mock.patch.object(app_token, "find_installation", lambda t, j: (1, 7)), \
            mock.patch.object(app_token, "github_json", gh), \
            mock.patch.dict(os.environ, {"CW_TOKEN_SERVICE_URL": ""}):
        app_token.mint(target)
    return seen


class TokenPins(unittest.TestCase):
    def test_the_set_the_repository_and_the_life(self):
        target = "acme-org/widget-service"
        self.assertEqual(token_problems(app_token.EXPECTED_PERMISSIONS, app_token.MAX_TOKEN_SECONDS, minted_request(target), target), [])

    def test_mutants_of_the_token_fail(self):
        target = "acme-org/widget-service"
        req = minted_request(target)
        wide = dict(WORKER_SET, administration="write")
        self.assertNotEqual(token_problems(wide, 3600, dict(req, permissions=wide), target), [])
        self.assertNotEqual(token_problems(dict(WORKER_SET, issues="write"), 3600, req, target), [])
        self.assertNotEqual(token_problems(WORKER_SET, 3600, dict(req, repositories=["a", "b"]), target), [])
        self.assertNotEqual(token_problems(WORKER_SET, 61 * 60 + 1, req, target), [])

    def test_a_token_that_lives_too_long_or_covers_two_repositories_is_refused(self):
        target = "acme-org/widget-service"
        for field, value in (("expires_at", (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")),
                             ("repositories", [{"name": "widget-service", "full_name": target, "owner": {"id": 7}},
                                               {"name": "other", "full_name": "acme-org/other", "owner": {"id": 7}}])):
            def gh(method, path, jwt, body=None, not_found=None, role="worker", field=field, value=value):
                ans = {"token": "t", "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "permissions": dict(body["permissions"]), "repositories": [{"name": "widget-service", "full_name": target, "owner": {"id": 7}}]}
                ans[field] = value
                return ans
            with mock.patch.object(app_token, "vault", lambda env, name: "12345" if "id" in name else "k"), \
                    mock.patch.object(app_token, "sign", lambda k, d: b"s"), \
                    mock.patch.object(app_token, "find_installation", lambda t, j: (1, 7)), \
                    mock.patch.object(app_token, "github_json", gh), \
                    mock.patch.dict(os.environ, {"CW_TOKEN_SERVICE_URL": ""}), \
                    mock.patch.object(app_token, "revoke", lambda t: None):
                with self.assertRaises(SystemExit, msg=field):
                    app_token.mint(target)

    def test_a_widened_answer_is_refused(self):
        with self.assertRaises(SystemExit):
            app_token.check_permissions(dict(WORKER_SET, administration="write"))
        with self.assertRaises(SystemExit):
            app_token.check_permissions(dict(WORKER_SET, issues="write"))


# ---- (4) the egress policy --------------------------------------------------------------------------------------------------
PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")


def egress_problems(docs):
    out = []
    policies = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "NetworkPolicy"}
    deny = policies.get("default-deny")
    if not deny:
        return ["no default-deny policy"]
    spec = deny["spec"]
    if spec.get("podSelector") != {} or set(spec.get("policyTypes") or []) != {"Ingress", "Egress"} or spec.get("egress") or spec.get("ingress"):
        out.append("default-deny does not select every pod and deny both directions")
    open_blocks = [t["ipBlock"] for p in policies.values() for r in (p["spec"].get("egress") or []) for t in (r.get("to") or []) if "ipBlock" in t]
    if not open_blocks:
        out.append("no internet allow rule to check")
    for b in open_blocks:
        ex = b.get("except") or []
        for need in PRIVATE + (cw_metadata_ranges()):
            if need not in ex:
                out.append("the allow rule does not except %s" % need)
    return out


def cw_metadata_ranges():
    """The metadata addresses the manifest itself documents as excepted: the link-local range and the platform's wire address."""
    return ("169.254.0.0/16", "168.63.129.16/32")


class EgressPins(unittest.TestCase):
    def docs(self):
        return parse_yaml(read("cloud-hub-network.yaml"))

    def test_default_deny_and_excepts(self):
        self.assertEqual(egress_problems(self.docs()), [])

    def test_mutants_of_the_policy_fail(self):
        docs = self.docs()
        no_deny = [d for d in docs if d["metadata"]["name"] != "default-deny"]
        self.assertNotEqual(egress_problems(no_deny), [])
        half = copy.deepcopy(docs)
        for d in half:
            if d["metadata"]["name"] == "default-deny":
                d["spec"]["policyTypes"] = ["Ingress"]
        self.assertNotEqual(egress_problems(half), [])
        for gone in PRIVATE + cw_metadata_ranges():
            weak = copy.deepcopy(docs)
            for d in weak:
                for r in d["spec"].get("egress") or []:
                    for t in r.get("to") or []:
                        if "ipBlock" in t:
                            t["ipBlock"]["except"] = [x for x in t["ipBlock"]["except"] if x != gone]
            self.assertNotEqual(egress_problems(weak), [], gone)


# ---- (5) preflight and the repository allow-list ----------------------------------------------------------------------------
def preflight_problems():
    out = []
    try:
        missing, _, _, _ = preflight.evaluate([])
    except IndexError:   # a table with fewer rows than the code reads: the requirement is gone
        return ["the required-rule table is shorter than the code reads"]
    if len(missing) != len(preflight.NEED) or not preflight.NEED:
        out.append("an empty rule list is not reported as missing every required rule")
    for need in ("pull_request", "non_fast_forward", "deletion"):   # the three default-branch protections the plan names
        if not any(need in alts.split("|") for alts, _ in preflight.NEED):
            out.append("no required rule of type %s" % need)
    names = {a for alts, _ in preflight.NEED for a in alts.split("|")}
    full = [{"type": t, "parameters": {"required_approving_review_count": 1}} for t in names]
    missing, _, _, problems = preflight.evaluate(full)
    if missing or problems:
        out.append("a complete rule list is refused")
    for alts, what in preflight.NEED:
        part = [r for r in full if r["type"] not in alts.split("|")]
        if not preflight.evaluate(part)[0]:
            out.append("without %s the preflight does not object" % what)
    return out


class PreflightPins(unittest.TestCase):
    def test_default_branch_rules_are_required(self):
        self.assertEqual(preflight_problems(), [])

    def test_mutants_of_the_requirement_fail(self):
        self.assertNotEqual(self.with_need(()), [])
        self.assertNotEqual(self.with_need(preflight.NEED[:1]), [])
        self.assertNotEqual(self.with_need(tuple(("never_a_rule", w) if i == 1 else (a, w) for i, (a, w) in enumerate(preflight.NEED))), [])

    @staticmethod
    def with_need(need):
        with mock.patch.object(preflight, "NEED", need):
            return preflight_problems()

    def test_unlisted_repository_is_refused(self):
        env = {"CW_ALLOWED_REPOS": "acme-org/widget-service, Acme-Org/Other"}
        cw.check_repo_allowed("acme-org/widget-service", env)
        cw.check_repo_allowed("ACME-ORG/other", env)
        for bad_env, repo in ((env, "acme-org/gadget"), (env, "acme-org/widget-service-x"), ({}, "acme-org/widget-service"),
                              ({"CW_ALLOWED_REPOS": " "}, "acme-org/widget-service"), ({"CW_ALLOWED_REPOS": "acme-org"}, "acme-org/widget-service"),
                              (env, "not a repo")):
            with self.assertRaises(cw.CloudError, msg=repr((bad_env, repo))):
                cw.check_repo_allowed(repo, bad_env)

    def test_mutant_of_the_allow_check_fails(self):
        # a check that lets everything through is caught by the refusal cases above: run them against a stub
        with mock.patch.object(cw, "check_repo_allowed", lambda repo, env: None):
            with self.assertRaises(AssertionError):
                self.test_unlisted_repository_is_refused()


if __name__ == "__main__":
    unittest.main()
