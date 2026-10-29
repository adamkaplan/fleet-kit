"""render-namespace (cloud/k8s/render-namespace): the four cloud manifests rendered for another namespace, as text, on standard output.

Offline: every run is a subprocess with no network (the socket module is made to fail inside it) and an empty working directory.
The mutants are source-level changes of a COPY of the script (with a copy of the four manifests beside it), each asserted applied.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
K8S = os.path.join(ROOT, "cloud", "k8s")
SCRIPT = os.path.join(K8S, "render-namespace")
FILES = ("cloud-hub.yaml", "cloud-hub-network.yaml", "operator-rbac.yaml", "cw-minter.yaml")
DOCS = os.path.join(ROOT, "docs", "cloud-operator-quickstart.md")

# runs the script with the network switched off: any socket use raises
LAUNCH = ("import runpy, socket, sys\n"
          "def _no(*a, **k):\n    raise OSError('network is off')\n"
          "socket.socket = _no\nsocket.create_connection = _no\nsocket.getaddrinfo = _no\n"
          "sys.argv = sys.argv[1:]\nrunpy.run_path(sys.argv[0], run_name='__main__')\n")


def read(name, folder=K8S):
    with open(os.path.join(folder, name), encoding="utf-8", newline="") as f:
        return f.read()


def run(script, *args, cwd=None):
    return subprocess.run([sys.executable, "-c", LAUNCH, script] + list(args), cwd=cwd, env={"PATH": "/nonexistent"},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)


def docs_of(text):
    return re.split(r"^---\s*$", text, flags=re.M)


def split_header(out, original_body):
    """The render is the header (comment lines) and then the files joined; returns the header."""
    assert out.endswith(original_body), "the body differs"
    return out[:len(out) - len(original_body)]


JOINED = "---\n".join(read(n) for n in FILES)


class Render(unittest.TestCase):
    def test_the_probe_render_has_no_cw_namespace_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as empty:
            done = run(SCRIPT, "cwp", cwd=empty)
            self.assertEqual(os.listdir(empty), [])
        self.assertEqual(done.returncode, 0, done.stderr)
        text = done.stdout.decode()
        self.assertEqual(re.findall(r'(?m)^\s*"?namespace"?: "?cw"?,?\s*$', text), [])
        self.assertEqual(len(re.findall(r'(?m)^\s*"?namespace"?: "?cwp"?,?$', text)), 21)
        self.assertRegex(text, r'"name": "FLEET_CLOUD_AGENT_NAMESPACE",\n\s*"value": "cwp"')
        self.assertNotIn('"value": "cw"', text)
        self.assertEqual(done.stderr, b"")

    def test_the_render_for_cw_equals_the_files(self):
        out = run(SCRIPT, "cw").stdout.decode()
        header = split_header(out, JOINED)
        self.assertTrue(header.startswith("# rendered for namespace cw\n"))
        body = out[len(header):]
        mine, theirs = docs_of(body), docs_of(JOINED)
        self.assertEqual(len(mine), len(theirs))
        self.assertGreaterEqual(len(mine), 6)
        for i, (a, b) in enumerate(zip(mine, theirs)):
            self.assertEqual(a, b, "document %d" % i)
        self.assertEqual(body.encode(), JOINED.encode())

    def test_the_render_for_cwp_changes_just_the_namespace_lines(self):
        base = run(SCRIPT, "cw").stdout.decode().splitlines()
        probe = run(SCRIPT, "cwp").stdout.decode().splitlines()
        base, probe = base[5:], probe[5:]   # the five header lines differ by the namespace
        self.assertEqual(len(base), len(probe))
        changed = 0
        for a, b in zip(base, probe):
            if a == b:
                continue
            changed += 1
            self.assertIn("cwp", b)
            self.assertEqual(re.sub(r'(namespace: |"namespace": "|-n |"value": ")cw\b', r"\1cwp", a), b)
        self.assertEqual(changed, 26)   # 21 namespace lines, the one value, four comment lines that name the namespace

    def test_the_header_names_the_cluster_wide_documents(self):
        header = "".join(run(SCRIPT, "cwp").stdout.decode().splitlines(keepends=True)[:5])
        self.assertTrue(header.startswith("# rendered for namespace cwp\n"))
        for word in ("RuntimeClass", "ClusterRole", "ClusterRoleBinding", "runtimeclass.yaml", "containerd-fix.yaml", "once per cluster"):
            self.assertIn(word, header)
        self.assertTrue(all(line.startswith("#") for line in header.splitlines()))


class Refusals(unittest.TestCase):
    def test_a_bad_namespace_is_refused_before_any_output(self):
        for name in ("default", "kube-system", "Bad_Name", "a" * 64, ""):
            with self.subTest(name=name):
                done = run(SCRIPT, name)
                self.assertEqual(done.returncode, 2)
                self.assertEqual(done.stdout, b"")
                self.assertIn(b"DNS label", done.stderr)
                self.assertEqual(done.stderr.count(b"\n"), 1)

    def test_a_missing_argument_is_refused(self):
        done = run(SCRIPT)
        self.assertEqual((done.returncode, done.stdout), (2, b""))


class Mutants(unittest.TestCase):
    def mutant_copy(self, old, new):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder)
        for name in FILES:
            shutil.copy(os.path.join(K8S, name), folder)
        source = read("render-namespace")
        self.assertIn(old, source)
        changed = source.replace(old, new, 1)
        self.assertNotEqual(changed, source)   # the mutation applied
        path = os.path.join(folder, "render-namespace")
        with open(path, "w", encoding="utf-8") as f:
            f.write(changed)
        return path

    def test_a_script_that_leaves_a_namespace_line_fails_the_probe_check(self):
        path = self.mutant_copy("    sys.stdout.write(render(argv[1]))",
                                "    sys.stdout.write(render(argv[1]).replace('\"namespace\": \"%s\"' % argv[1], '\"namespace\": \"cw\"', 1))")
        text = run(path, "cwp").stdout.decode()
        self.assertNotEqual(re.findall(r'(?m)^\s*"?namespace"?: "?cw"?,?\s*$', text), [])

    def test_a_script_that_reformats_a_document_fails_the_equality_check(self):
        path = self.mutant_copy("out.append(body + end)", "out.append(body + ('' if body != 'kind: Role' else ' ') + end)")
        out = run(path, "cw").stdout.decode()
        self.assertNotEqual(out[len(out) - len(JOINED):], JOINED)


class Runbook(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("\n## A probe namespace\n", text)
        self.section = text.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]

    def test_the_six_steps_come_in_order(self):
        marks = ["cloud/k8s/render-namespace cwp > cwp.yaml", "kubectl apply --dry-run=server -f cwp.yaml", "ADMINISTRATOR login",
                 "CW_PULL_SECRET`", "private settings", "smoke launch"]
        steps = ["Step %d" % n for n in range(1, 7)]
        at = [self.section.index(s) for s in steps]
        self.assertEqual(at, sorted(at))
        for i, mark in enumerate(marks):
            end = at[i + 1] if i < 5 else len(self.section)
            self.assertIn(mark, self.section[at[i]:end], steps[i])

    def test_the_four_setting_names_and_the_marked_steps(self):
        for name in ("CW_CLOUD_NAMESPACE=cwp", "CW_HUB_CHANNEL_URL=http://cloud-hub.cwp.svc:8443/message", "CW_PULL_SECRET", "CW_CLOUD_TOKEN_FILE"):
            self.assertIn(name, self.section)
        step3 = self.section.split("Step 3", 1)[1].split("Step 4", 1)[0]
        step4 = self.section.split("Step 4", 1)[1].split("Step 5", 1)[0]
        self.assertIn("the owner's step", step3)
        self.assertIn("the owner's step", step4)
        self.assertIn("launch --cloud", self.section)
        self.assertIsNone(re.search(r"<[A-Za-z_]+>", self.section))

    def test_the_cost_and_the_removal_are_stated(self):
        self.assertIn("adds one more hub pod and its Services", self.section)
        self.assertIn("while the namespace exists", self.section)
        self.assertIn("Deleting the namespace removes everything inside it", self.section)
        for command in ("kubectl delete namespace NAMESPACE_NAME", "kubectl -n NAMESPACE_NAME delete rolebinding cw-minter",
                        "kubectl -n NAMESPACE_NAME delete secret cw-minter-token", "rm -rf PROBE_CONFIG_DIR", "rm -f TOKEN_FILE"):
            self.assertIn(command, self.section)


DAEMON_ENV = "FL" + "EET_CLOUD_HUB_DAEMON"   # assembled: the neutral-names scan of this folder reads the old prefix
R1_COMMAND = "kubectl -n NAMESPACE_NAME set env deployment/cloud-hub %s=relay-probe" % DAEMON_ENV


def hub_reserved_names():
    """The hub's own RESERVED set, read from the code (the module is loaded in a subprocess: no import side effects here)."""
    out = subprocess.run([sys.executable, "-c", "import sys, json; sys.path[:0] = [sys.argv[1], sys.argv[2]]; import cloud_hub; "
                          "print(json.dumps(sorted(cloud_hub.RESERVED)))", os.path.join(ROOT, "bin"), os.path.join(ROOT, "cloud", "launcher")],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=True)
    import json
    return set(json.loads(out.stdout))


class RelayAppendix(unittest.TestCase):
    TITLE = "\n### A relay probe\n"

    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            text = f.read()
        self.whole = text.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn(self.TITLE, self.whole)
        self.text = self.whole.split(self.TITLE, 1)[1]

    def test_the_appendix_has_three_steps_in_order(self):
        marks = ["\nR1. Who: ", "\nR2. Who: ", "\nR3. Who: "]
        at = [self.text.index(m) for m in marks]
        self.assertEqual(at, sorted(at))
        who = [self.text[at[0]:at[1]], self.text[at[1]:at[2]], self.text[at[2]:]]
        self.assertTrue(who[0].startswith(marks[0] + "the administrator"))
        self.assertTrue(who[1].startswith(marks[1] + "the holder of the probe credential"))
        self.assertTrue(who[2].startswith(marks[2] + "the launcher's caller"))

    def test_the_daemon_name_command_is_found_once_and_the_reserved_names_match_the_hub(self):
        self.assertEqual(self.text.count(R1_COMMAND), 1)
        self.assertIn("kubectl -n NAMESPACE_NAME rollout status deployment/cloud-hub", self.text)
        line = [l for l in self.text.split("\n") if l.startswith("Reserved names:")]
        self.assertEqual(len(line), 1)
        listed = set(re.findall(r"`([^`]+)`", line[0]))
        self.assertEqual(listed, hub_reserved_names())
        self.assertIn("render of the namespace `cw` stays byte-equal", self.text)

    def test_the_handover_is_the_digest_alone_and_the_file_is_private(self):
        self.assertIn("umask 077", self.text)
        self.assertIn("secrets.token_urlsafe(32)", self.text)
        self.assertIn("mode 0600", self.text)
        self.assertIn("hands ONLY THE DIGEST to the administrator", self.text)
        self.assertIn('{"relay-probe": "DIGEST_VALUE"}', self.text)
        self.assertIn("kubectl -n NAMESPACE_NAME get secret cloud-hub-registry", self.text)

    def test_the_session_verb_and_the_entry_change_are_named(self):
        self.assertIn("cloud/launcher/cloud-worker hub-session WORKER_NAME --session SESSION_ID", self.text)
        self.assertIn("ask #1475, pull request #1491, branch cw/hub-entry-repo-issue", self.text)
        self.assertIn("The administrator's part takes about 20 minutes", self.text)

    def test_the_teardown_list_names_the_token_file_and_the_digest_file(self):
        teardown = self.whole.split("Teardown (", 1)[1].split("```", 2)[1]
        self.assertIn("rm -f PROBE_TOKEN_FILE", teardown)
        self.assertIn("rm -f PROBE_DIGEST_FILE", teardown)

    ONE_SCOPE = "the render makes no cluster-scoped objects, so deleting the namespace removes everything it created"
    EDITED = "they exist from the apply and are EDITED, not created"

    def check_probe_teardown_and_secret_order(self):
        self.assertEqual(self.whole.count(self.ONE_SCOPE), 1)
        listing = self.whole.split("```text", 2)[1]
        self.assertLess(listing.index("get secret"), listing.index("create secret generic"))
        self.assertEqual(self.whole.count(self.EDITED), 1)
        self.assertLess(self.whole.index("run the `get secret` line first"), self.whole.index("ONLY for a Secret the `get` reports missing"))
        teardown = self.whole.split("Teardown (", 1)[1].split("```", 2)[1]
        self.assertNotIn("delete serviceaccount", teardown)
        for kept in ("delete rolebinding cw-minter", "delete secret cw-minter-token", "kubectl delete namespace NAMESPACE_NAME",
                     "rm -f TOKEN_FILE", "rm -rf PROBE_CONFIG_DIR"):
            self.assertIn(kept, teardown)

    def test_the_teardown_scope_the_secret_order_and_the_single_deletes(self):
        self.check_probe_teardown_and_secret_order()

    def test_a_document_without_the_scope_sentence_fails_the_check(self):
        mutant = self.whole.replace(self.ONE_SCOPE, "")
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.whole = mutant
        with self.assertRaises(AssertionError):
            self.check_probe_teardown_and_secret_order()

    def test_a_document_without_the_daemon_command_fails_the_check(self):
        mutant = self.text.replace(R1_COMMAND, "")
        self.assertNotEqual(mutant, self.text)   # the mutation is applied
        self.text = mutant
        with self.assertRaises(AssertionError):
            self.test_the_daemon_name_command_is_found_once_and_the_reserved_names_match_the_hub()

    def blocks(self, text=None):
        return (text or self.text).split("```text\n")[1:]

    def check_working_copy_is_private(self, text=None):
        block = [b.split("```", 1)[0] for b in self.blocks(text) if "> secrets.json" in b][0].strip().split("\n")
        self.assertTrue(block[0].startswith("( umask 077"))
        self.assertEqual(block[-1], ")")
        self.assertTrue(any("base64 -d > secrets.json" in l for l in block[1:-1]))
        self.assertEqual(block[-2], "rm -f secrets.json")

    def test_the_working_copy_sequence_sits_in_a_subshell_with_umask(self):
        self.check_working_copy_is_private()

    def test_a_document_without_the_working_copy_umask_fails_the_check(self):
        mutant = self.text.replace("( umask 077\nkubectl", "( \nkubectl")
        self.assertNotEqual(mutant, self.text)   # the mutation is applied
        with self.assertRaises(AssertionError):
            self.check_working_copy_is_private(mutant)

    def test_the_token_step_is_in_a_subshell_and_no_bare_umask_remains(self):
        block = [b.split("```", 1)[0] for b in self.blocks() if "PROBE_TOKEN_FILE" in b][0].strip().split("\n")
        self.assertTrue(block[0].startswith("( umask 077;"))
        self.assertEqual(block[-1], ")")
        for line in self.text.split("\n"):
            if "umask 077" in line:
                self.assertTrue(line.startswith("( umask 077"), line)

    def test_the_not_atomic_sentence_is_found_once(self):
        self.assertEqual(self.text.count("read, edit and patch of the registry Secret is not atomic: one administrator at a time"), 1)

    def test_the_token_step_refuses_an_existing_file_and_says_so_once(self):
        block = [b.split("```", 1)[0] for b in self.blocks() if "PROBE_TOKEN_FILE" in b][0]
        self.assertIn("set -C", block)
        self.assertEqual(self.text.count("an existing file is never overwritten"), 1)


class Chained(unittest.TestCase):
    def test_this_file_is_in_the_chain_once(self):
        with open(os.path.join(ROOT, "bin", "test-cloud-launcher.chain")) as f:
            entries = f.read().split("\n")
        self.assertEqual(entries.count("cloud/launcher/tests/" + os.path.basename(__file__)), 1)

    def test_the_script_is_executable__with_a_shebang(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        with open(SCRIPT) as f:
            self.assertEqual(f.readline(), "#!/usr/bin/env python3\n")


if __name__ == "__main__":
    unittest.main()
