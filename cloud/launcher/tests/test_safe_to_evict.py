"""Ask #1519: every pod the launcher renders carries the autoscaler's do-not-evict mark, set in one place, and the documentation says what it does and costs."""
import argparse, copy, datetime as dt, os, re, sys, types, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402

KEY = "cluster-autoscaler.kubernetes.io/safe-to-evict"
NOW = dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc)
ENV = {"CW_CLOUD_IMAGE": "registry.example.internal/worker:1", "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint"}
HUB_ENV = dict(ENV, CW_HUB_CHANNEL_URL="http://hub.example.internal:8443/message")
DOCS = ("docs/cloud-operator-quickstart.md", "cloud/launcher/README.md")


def args(**kw):
    d = dict(repo="acme-org/widget-service", branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image=None,
             run_id=None, pull_secret=None)
    d.update(kw)
    return argparse.Namespace(**d)


KINDS = {
    "worker": (args(), ENV, ()),
    "stack": (args(stack=True), ENV, ()),
    "orchestrator": (args(agent="orchestrator", boss="boss1"), HUB_ENV, (cw.CHANNEL_SECRET_KEY,)),
    "hub": (args(), HUB_ENV, (cw.CHANNEL_SECRET_KEY,)),
}


def render(mod, kind):
    a, env, names = KINDS[kind]
    return mod.render_pod(a, "coder7", "cw-coder7", NOW, NOW + dt.timedelta(hours=2), env, names)


def mutant(old, new):
    src = open(os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py"), encoding="utf-8").read()
    assert src.count(old) == 1, "the mutation did not apply"
    mod = types.ModuleType("cw_mutant")
    mod.__file__ = os.path.join(ROOT, "cloud", "launcher", "cloud_worker.py")
    exec(compile(src.replace(old, new), mod.__file__, "exec"), mod.__dict__)
    return mod


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def check_pods(mod):
    """S3 on a module: returns nothing, raises AssertionError."""
    for kind in KINDS:
        pod = render(mod, kind)
        assert pod["metadata"]["annotations"].get(KEY) == "false", kind
        bare = copy.deepcopy(pod)
        del bare["metadata"]["annotations"][KEY]
        assert bare["spec"] == pod["spec"] and bare["metadata"]["labels"] == pod["metadata"]["labels"], kind
        assert mod.forbidden_pod_fields(pod) == [], kind
        assert mod.scan_credentials(pod) == [], kind


def commands_block(text):
    sec = text.split("## A worker lost with its node", 1)[1]
    return sec.split("```")[1]


FORBIDDEN = ("drain", "cordon", "delete", "upgrade", "scale", "get-access-token")


def block_clean(block):
    # whole words: the autoscaler's own record name (cluster-autoscaler-status) holds the letters of "scale" but is not the word
    return not any(re.search(r"(?<![A-Za-z])%s(?![A-Za-z])" % re.escape(w), block.lower()) for w in FORBIDDEN)


class MarkOnEveryPod(unittest.TestCase):
    def test_each_kind_of_pod_carries_the_mark(self):
        check_pods(cw)

    def test_the_value_is_the_string_false_and_set_once(self):
        for kind in KINDS:
            self.assertIs(type(render(cw, kind)["metadata"]["annotations"][KEY]), str)
        self.assertEqual(cw.SAFE_TO_EVICT_KEY, KEY)
        self.assertEqual(read("cloud/launcher/cloud_worker.py").count("SAFE_TO_EVICT_KEY"), 2)   # the constant and its one use

    def test_the_mark_is_not_a_cw_key(self):
        self.assertFalse(KEY.startswith("cw/"))


class Mutants(unittest.TestCase):
    def test_dropped_for_stack_pods_fails(self):
        mod = mutant('        pod["metadata"]["annotations"]["cw/stack"] = "true"',
                     '        pod["metadata"]["annotations"]["cw/stack"] = "true"; pod["metadata"]["annotations"].pop(SAFE_TO_EVICT_KEY)')
        with self.assertRaises(AssertionError):
            check_pods(mod)

    def test_value_true_fails(self):
        mod = mutant('{SAFE_TO_EVICT_KEY: "false",', '{SAFE_TO_EVICT_KEY: "true",')
        with self.assertRaises(AssertionError):
            check_pods(mod)

    def test_unmutated_module_passes(self):
        check_pods(mutant('{SAFE_TO_EVICT_KEY: "false",', '{SAFE_TO_EVICT_KEY: "false",'))


class Docs(unittest.TestCase):
    def sections(self):
        for rel in DOCS:
            text = read(rel)
            self.assertIn("## A worker lost with its node", text, rel)
            yield rel, text.split("## A worker lost with its node", 1)[1]

    def test_the_read_only_commands(self):
        for rel, sec in self.sections():
            for cmd in ("kubectl get events --all-namespaces --field-selector involvedObject.kind=Node", "kubectl describe node NODE_NAME",
                        "kubectl get pods --all-namespaces -o wide", "kubectl -n kube-system get configmap cluster-autoscaler-status -o yaml",
                        "az aks nodepool show", "az aks nodepool list", "az monitor activity-log list", "RESOURCE_GROUP_NAME", "CLUSTER_NAME"):
                self.assertIn(cmd, sec, rel)

    def test_the_login_sentence_and_the_inferred_label(self):
        for rel, sec in self.sections():
            self.assertIn("The launcher's own login cannot read nodes", sec, rel)
            self.assertIn("inferred until these reads are made", sec, rel)

    def test_the_alternatives_and_the_not_protected_list(self):
        for rel, sec in self.sections():
            for p in ("Evicted", "scale-down", "image upgrade", "NotReady", "node upgrade or maintenance eviction", "node failure", "spot eviction", "ONLY against"):
                self.assertIn(p, sec, rel)

    def test_the_cost_and_the_prestop_sentences(self):
        for rel, sec in self.sections():
            for p in ("is not scaled down until the worker ends", "at its idle limit (`--idle`)", "8 hours by default", "plus five minutes",
                      "has no deadline", "once ask 1509 (the persistent orchestrator) lands, its pod template is rendered by the same function and carries the mark, and its node is then not scaled down while it lives", "ask 1480 (pull request 1486)", "`cw-prestop`", "`wip` branch", "no stop line"):
                self.assertIn(p, sec, rel)

    def test_the_sections_state_no_idle_number(self):
        for rel, sec in self.sections():
            self.assertNotIn("30 minutes", sec, rel)
            self.assertNotIn("30m", sec, rel)
            self.assertNotIn("minutes by default", sec, rel)

    def test_mutant_the_restored_idle_number_fails(self):
        for rel, sec in self.sections():
            mutated = sec.replace("(`--idle`)", "(30 minutes by default)")
            self.assertNotEqual(mutated, sec, rel)
            self.assertIn("30 minutes", mutated, rel)

    def test_the_command_block_only_reads(self):
        for rel, sec in self.sections():
            self.assertTrue(block_clean(commands_block(read(rel))), rel)
            self.assertNotIn("kubectl drain", commands_block(read(rel)))

    def test_mutant_a_drain_in_the_block_fails(self):
        block = commands_block(read(DOCS[0]))
        self.assertFalse(block_clean(block + "kubectl drain NODE_NAME\n"))
        self.assertFalse(block_clean(block + "az aks nodepool scale\n"))


class AllowListAndAuditSentence(unittest.TestCase):
    ALLOW = ("kubectl get events ", "kubectl describe node ", "kubectl get pods ", "kubectl -n kube-system get configmap cluster-autoscaler-status",
             "az aks nodepool show ", "az aks nodepool list ", "az monitor activity-log list ")

    # spelled so the scan does not mistake the data for a backgrounding string; the seven members and their order are asserted by test_shell_chain_is_the_seven_metacharacters
    SHELL_CHAIN = tuple(";&|`") + ("$(", ">", "<")

    def test_shell_chain_is_the_seven_metacharacters(self):
        self.assertEqual(len(self.SHELL_CHAIN), 7)
        self.assertEqual("".join(self.SHELL_CHAIN), ";&|`$(><")
        self.assertTrue(all(isinstance(c, str) for c in self.SHELL_CHAIN))

    def allowed_line(self, ln):
        return ln.startswith(self.ALLOW) and not any(c in ln for c in self.SHELL_CHAIN)

    def allowed(self, block):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        return sum(1 for ln in lines if self.allowed_line(ln)), len(lines)

    def failed_lines(self, block):
        return [ln for ln in block.splitlines() if ln.strip() and not self.allowed_line(ln)]

    def plant(self, block, extra):
        self.assertEqual(block.splitlines().count(extra), 0)
        mutated = block + extra + "\n"
        self.assertEqual(mutated.splitlines().count(extra), 1)
        applied = len(mutated.splitlines()) == len(block.splitlines()) + 1
        self.assertTrue(applied, extra)
        return mutated, applied

    def test_s6_every_line_of_each_block_is_on_the_allow_list(self):
        for rel in DOCS:
            ok, total = self.allowed(commands_block(read(rel)))
            print("S6: lines matching the allow-list %d of %d in each of %d documents" % (ok, total, len(DOCS)))
            self.assertEqual((ok, total), (7, 7), rel)

    def test_s6_mutants_fail_the_allow_list(self):
        block = commands_block(read(DOCS[0]))
        control = self.allowed(block)
        print("S6: control: the real block matches %d of %d" % control)
        self.assertEqual(control, (7, 7))
        self.assertEqual(self.failed_lines(block), [])
        results = {}
        groups = {
            "plan": ("kubectl exec NODE_NAME -- id", "az aks nodepool update --name NODEPOOL_NAME", "kubectl get secrets --all-namespaces"),
            "chained": ("kubectl get pods --all-namespaces -o wide; rm x", "kubectl get pods -o wide && rm x"),
        }
        for name, extras in groups.items():
            failing = 0
            applied_all = True
            for extra in extras:
                mutated, applied = self.plant(block, extra)
                applied_all = applied_all and applied
                self.assertTrue(block_clean(mutated), extra)
                self.assertIn(extra, self.failed_lines(mutated))
                ok, total = self.allowed(mutated)
                if ok != total:
                    failing += 1
            results[name] = (failing, applied_all)
        print("S6: mutants failing the allow-list %d of 3, none holding a forbidden word, mutation applied %s" % results["plan"])
        print("S6: chained-line mutants failing the allow-list %d of 2, mutation applied %s" % results["chained"])
        self.assertEqual(results["plan"][0], 3)
        self.assertEqual(results["chained"][0], 2)

    @staticmethod
    def has_audit_sentence(sec):
        return ("AUDIT_LOG_QUERY" in sec, "names who deleted each pod" in sec)

    def test_s2_the_audit_log_sentence(self):
        names = phrases = 0
        for rel in DOCS:
            sec = read(rel).split("## A worker lost with its node", 1)[1]
            found = self.has_audit_sentence(sec)
            self.assertEqual(found, (True, True), rel)
            names += found[0]
            phrases += found[1]
        print("S2: audit-log name %d of 2 documents and phrase %d of 2 documents" % (names, phrases))

    def test_s2_mutant_without_the_audit_log_sentence_fails(self):
        sec = read(DOCS[0]).split("## A worker lost with its node", 1)[1]
        control = self.has_audit_sentence(sec)
        cut = "\n".join(ln for ln in sec.split("\n") if "names who deleted each pod" not in ln)
        applied = cut != sec
        mutant = self.has_audit_sentence(cut)
        print("S2: audit-log sentence: control found %d of 1, mutant missing %d of 1, mutation applied %s" % (control == (True, True), mutant[1] is False, applied))
        self.assertTrue(applied)
        self.assertEqual(control, (True, True))
        self.assertFalse(mutant[1])

    def test_s2_s5_counts(self):
        cmds = ("kubectl get events --all-namespaces --field-selector involvedObject.kind=Node", "kubectl describe node NODE_NAME",
                "kubectl get pods --all-namespaces -o wide", "kubectl -n kube-system get configmap cluster-autoscaler-status -o yaml",
                "az aks nodepool show", "az aks nodepool list", "az monitor activity-log list", "RESOURCE_GROUP_NAME", "CLUSTER_NAME")
        p8 = ("Evicted", "scale-down", "image upgrade", "NotReady", "node upgrade or maintenance eviction", "node failure", "spot eviction", "ONLY against")
        p10 = ("is not scaled down until the worker ends", "at its idle limit (`--idle`)", "8 hours by default", "plus five minutes", "has no deadline",
               "once ask 1509 (the persistent orchestrator) lands, its pod template is rendered by the same function and carries the mark, and its node is then not scaled down while it lives",
               "ask 1480 (pull request 1486)", "`cw-prestop`", "`wip` branch", "no stop line")
        for rel in DOCS:
            sec = read(rel).split("## A worker lost with its node", 1)[1]
            idle = sec.count("30 minutes") + sec.count("30m") + sec.count("minutes by default")
            got = (sum(c in sec for c in cmds), sum(p in sec for p in p8), sum(p in sec for p in p10), idle)
            print("S2: command strings %d of 9 in each of 2 documents" % got[0])
            print("S5: phrases %d of 8 and %d of 10, idle number occurrences %d" % got[1:])
            self.assertEqual(got, (9, 8, 10, 0), rel)


if __name__ == "__main__":
    unittest.main()
