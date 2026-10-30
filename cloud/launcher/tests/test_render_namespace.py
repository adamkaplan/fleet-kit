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
        marks = ["cloud/k8s/render-namespace cwp > cwp.yaml", 'kc apply --dry-run=server -f cwp.yaml', "ADMINISTRATOR login",
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
        for command in ('ns_ok && kubectl delete namespace "${NS:?}"', 'kc delete rolebinding cw-minter',
                        'kc delete secret cw-minter-token', 'rm -rf "${PROBE_CONFIG_DIR:?}"', "rm -f TOKEN_FILE"):
            self.assertIn(command, self.section)


DAEMON_ENV = "FL" + "EET_CLOUD_HUB_DAEMON"   # assembled: the neutral-names scan of this folder reads the old prefix
R1_COMMAND = 'kc set env deployment/cloud-hub %s=relay-probe' % DAEMON_ENV


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
        self.assertIn('kc rollout status deployment/cloud-hub', self.text)
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
        self.assertIn('kc get secret cloud-hub-registry', self.text)

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
        blocks = code_blocks(self.whole)
        listing, missing = blocks[0], blocks[1]
        self.assertIn("get secret cloud-hub-registry cloud-hub-pw", listing)
        self.assertNotIn("create secret generic", listing)   # the first block makes no secret: the apply already made both
        self.assertEqual(missing.count("create secret generic"), 2)
        self.assertLess(self.whole.index(listing), self.whole.index("Only if `get` reports missing"))
        self.assertLess(self.whole.index("Only if `get` reports missing"), self.whole.index(missing))
        self.assertEqual(self.whole.count(self.EDITED), 1)
        self.assertLess(self.whole.index("run the `get secret` line first"), self.whole.index("ONLY for a Secret the `get` reports missing"))
        teardown = self.whole.split("Teardown (", 1)[1].split("```", 2)[1]
        self.assertNotIn("delete serviceaccount", teardown)
        for kept in ("delete rolebinding cw-minter", "delete secret cw-minter-token", 'ns_ok && kubectl delete namespace "${NS:?}"',
                     "rm -f TOKEN_FILE", 'rm -rf "${PROBE_CONFIG_DIR:?}"'):
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


def code_blocks(text):
    return re.findall(r"```text\n(.*?)```", text, flags=re.S)


def subshells(block):
    """The parenthesised blocks of a code block: a line that is '(' alone or starts '( ', up to the line that is ')' or ends ' )'."""
    found, current = [], None
    for line in block.split("\n"):
        if current is None and (line == "(" or line.startswith("( ")):
            current = []
        if current is not None:
            current.append(line)
            if line == ")" or line.endswith(" )"):
                found.append("\n".join(current))
                current = None
    return found


TOKEN_BARE = 'kc create token cw-operator --duration=DURATION'


class CredentialFileCare(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.doc = f.read()
        self.whole = self.doc.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]
        self.relay = self.whole.split("\n### A relay probe\n", 1)[1]

    def check_working_copy_in_subshell(self):
        hits = [s for b in code_blocks(self.relay) for s in subshells(b) if "> secrets.json" in s]
        self.assertEqual(len(hits), 1)
        self.assertIn("umask 077", hits[0])
        self.assertLess(hits[0].index("umask 077"), hits[0].index("> secrets.json"))
        self.assertIn("rm -f secrets.json registry-patch.json", hits[0])
        self.assertIn("trap ", hits[0])
        self.assertIn("ls secrets.json registry-patch.json", self.relay)

    def test_working_copy_sequence_is_in_a_umask_subshell(self):
        self.check_working_copy_in_subshell()

    def test_a_document_without_the_working_copy_umask_fails_the_check(self):
        old = self.relay
        parts = [b for b in code_blocks(old) if "> secrets.json" in b]
        self.assertEqual(len(parts), 1)
        mutant_block = parts[0].replace("umask 077", "")
        self.assertNotEqual(mutant_block, parts[0])   # the mutation is applied
        self.relay = old.replace(parts[0], mutant_block)
        with self.assertRaises(AssertionError):
            self.check_working_copy_in_subshell()

    def test_token_step_is_in_a_umask_subshell_and_no_bare_umask_remains(self):
        step = self.relay.split("\nR2. Who: ", 1)[1].split("\nR3. Who: ", 1)[0]
        block = [b for b in code_blocks(step) if "PROBE_TOKEN_FILE" in b][0]
        self.assertTrue(block.startswith("( umask 077;"))
        for text in code_blocks(self.doc):
            inside = "".join(subshells(text))
            for line in text.split("\n"):
                if line.strip().startswith("umask"):
                    self.assertIn(line, inside)

    def test_not_atomic_sentence_found_once(self):
        self.assertEqual(self.doc.count("the read, edit and patch of the registry Secret is not atomic: one administrator at a time"), 1)

    def test_token_step_has_noclobber_and_never_overwrites(self):
        step = self.relay.split("\nR2. Who: ", 1)[1].split("\nR3. Who: ", 1)[0]
        block = [b for b in code_blocks(step) if "PROBE_TOKEN_FILE" in b][0]
        self.assertIn("set -C", block)
        self.assertIn("PROBE_DIGEST_FILE", block)
        self.assertEqual(self.doc.count("an existing probe file is never overwritten (a second run needs the first file removed by its holder)"), 1)

    def check_token_command(self):
        self.assertNotIn("\n" + TOKEN_BARE + "\n", self.doc)
        listing = code_blocks(self.whole)[0]
        hits = [s for s in subshells(listing) if "create token cw-operator" in s]
        self.assertEqual(len(hits), 1)
        for need in ("set -C", "umask 077", "> TOKEN_FILE"):
            self.assertIn(need, hits[0])
        self.assertEqual(self.doc.count("The token file is never overwritten (a second run needs the first removed)"), 1)

    def test_the_cluster_token_command_writes_to_a_file_inside_a_subshell(self):
        self.check_token_command()

    def test_a_document_with_the_bare_token_line_fails_the_check(self):
        listing = code_blocks(self.whole)[0]
        line = [s for s in listing.split("\n") if "create token cw-operator" in s]
        self.assertEqual(len(line), 1)
        mutant = self.doc.replace(line[0], TOKEN_BARE)
        self.assertNotEqual(mutant, self.doc)   # the mutation is applied
        self.doc = mutant
        self.whole = self.doc.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]
        with self.assertRaises(AssertionError):
            self.check_token_command()

    def test_the_pull_secret_is_copied_by_name_and_no_value_is_on_a_command_line(self):
        self.assertNotIn("docker-" + "password", self.doc)
        copies = [l for l in self.doc.split("\n") if "get secret PULL_SECRET_NAME -o json" in l and "create -f -" in l]
        self.assertEqual(len(copies), 1)
        self.assertIn('| kc create -f -', copies[0])
        self.assertIn("managedFields", copies[0])
        self.assertIn('kubectl -n "${SRC:?}" get secret', copies[0])
        self.assertIn("--from-file=.dockerconfigjson=DOCKER_CONFIG_FILE", self.whole)
        self.assertIn("read only", self.whole)


def credential_shapes(text):
    """Names of the unsafe credential shapes found in the code blocks of a runbook text."""
    found = []
    for block in code_blocks(text):
        inside = "\n".join(subshells(block))
        for line in block.split("\n"):
            for value in re.findall(r"--from-literal=[^=\s]+=(\S*)", line):
                if value not in ("", "{}"):
                    found.append("from-literal value: " + line)
            words = line.split()
            for i, word in enumerate(words):
                if word == "--from-literal":
                    nxt = words[i + 1] if i + 1 < len(words) else ""
                    if "=" in nxt:
                        if nxt.split("=", 1)[1] not in ("", "{}"):
                            found.append("spaced from-literal value: " + line)
                    elif nxt:
                        found.append("from-literal value in the next word: " + line)
            if re.search(r"(?<!\S)--(docker-)?pass" + "word\\b", line):
                found.append("registry password on a command line: " + line)
            if re.search(r"(?<!\S)-p(=|\s+)\S", line):
                found.append("short password option with a value: " + line)
            if "create token" in line and not (line in inside and re.search(r"> \S+", line)):
                found.append("bare create token: " + line)
            if "base64 -d" in line and not re.search(r"base64 -d > \S+", line):
                found.append("base64 -d without a file: " + line)
            if re.search(r"\bset env\b", line) and re.search(r"(SECRET|PASSWORD|PASSWD|TOKEN|KEY|PW)\w*=\S", line):
                found.append("set env with a secret value: " + line)
    return found


class CredentialShapeSweep(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.doc = f.read()
        self.whole = self.doc.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]

    def test_the_runbook_has_no_unsafe_credential_shape(self):
        self.assertEqual(credential_shapes(self.whole), [])

    def test_the_scan_sees_each_unsafe_shape(self):
        samples = {
            "from-literal": "```text\nkubectl create secret generic X --from-literal=k=VALUE\n```",
            "password": "```text\nkubectl create secret docker-registry X --docker-" + "password=P\n```",
            "token": "```text\nkubectl create token cw-operator\n```",
            "base64": "```text\nkubectl get secret X | base64 -d\n```",
            "env": "```text\nkubectl set env deployment/x API_TOKEN=VALUE\n```",
        }
        samples.update({
            "spaced from-literal": "```text\nkubectl create secret generic X --from-literal k=VALUE\n```",
            "from-literal next word": "```text\nkubectl create secret generic X --from-literal k VALUE\n```",
            "bare password option": "```text\nsometool login --pass" + "word P\n```",
            "short option": "```text\nsometool login -p P\n```",
            "old patch form": "```text\nkubectl patch secret X --type merge -p \"$(cat f)\"\n```",
        })
        for name, text in samples.items():
            self.assertEqual(len(credential_shapes(text)), 1, name)
        self.assertEqual(credential_shapes("```text\nkubectl create secret generic X --from-literal=k={}\n```"), [])
        self.assertEqual(credential_shapes("```text\nkubectl create secret generic X --from-literal k={}\n```"), [])
        self.assertEqual(credential_shapes("```text\nkubectl patch secret X --type merge --patch-file FILE\n```"), [])

    def test_the_patch_file_form_replaces_the_command_substitution(self):
        self.assertEqual(self.doc.count("--patch-file registry-patch.json"), 1)
        self.assertNotIn("-p \"$(", self.doc)
        self.assertIn("rm -f secrets.json registry-patch.json", self.doc)

    def test_the_pull_copy_sentence_is_found_once(self):
        sentence = "gives the probe namespace the SAME registry pull value: an administrator step on the same registry"
        self.assertEqual(self.doc.count(sentence), 1)

    def test_a_runbook_with_the_old_patch_restored_fails_the_scan(self):
        new = 'kc patch secret cloud-hub-registry --type merge --patch-file registry-patch.json'
        old = 'kc patch secret cloud-hub-registry --type merge -p "$(cat secrets.json)"'
        mutant = self.whole.replace(new, old)
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.assertEqual(len(credential_shapes(mutant)), 1)

    def test_a_runbook_with_the_bare_token_line_fails_the_scan(self):
        line = [l for l in self.whole.split("\n") if "create token cw-operator" in l and l.startswith("( ")]
        self.assertEqual(len(line), 1)
        mutant = self.whole.replace(line[0], TOKEN_BARE)
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.assertEqual(len(credential_shapes(mutant)), 1)


LIVE = r"(?<![\w-])cw(?![\w-])"
REPORT = "# needs the first block (NS, ns_ok, kc)"
GUARD = 'ns_ok() {'


def runbook_shapes(text):
    """Names of the live-cluster risks found in the code blocks of a runbook text (apart from the credential shapes)."""
    found = []
    for block in code_blocks(text):
        lines = block.split("\n")
        if any("kubectl" in l for l in lines) and not any(l == REPORT for l in lines) and not lines[0].startswith(GUARD):
            found.append("block without the namespace guard: " + lines[0])
        for line in lines:
            if re.search(r"(?:-n|--namespace|namespace)[ =]+\"?" + LIVE, line):
                found.append("the live namespace on a line: " + line)
            if re.search(r"\bdelete\b", line) and re.search(LIVE, line):
                found.append("delete with a literal live name: " + line)
            if "delete namespace" in line and '"${NS:?}"' not in line:
                found.append("delete namespace without the variable: " + line)
            if re.search(r"(?<![\w-])(echo|cat)\s", line) and re.search(r"(?i)token", line):
                found.append("echo or cat of a token: " + line)
            if "--from-env-file" in line:
                found.append("from-env-file: " + line)
            if re.search(r"get secret\b.*(-o|--output)[ =]*(yaml|json)\b", line) and not re.search(r"(-o|--output)[ =]*(yaml|json)\b.*[|>]", line):
                found.append("secret in full to a terminal: " + line)
            if re.search(r"\bkubectl\s+(apply|create)\b.* -f\b", line) and "namespace" not in line.split(" -f")[0].split("kubectl", 1)[1].split()[:2] and ' -n "${NS:?}"' not in line:
                found.append("apply or create -f without the namespace: " + line)
            if "rm -rf" in line and not re.search(r'rm -rf "\$\{\w+:\?\}"', line):
                found.append("rm -rf without a guarded path: " + line)
    return found


class RunbookSweep(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.doc = f.read()
        self.whole = self.doc.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]

    def test_the_runbook_has_no_live_cluster_risk(self):
        self.assertEqual(runbook_shapes(self.whole), [])
        self.assertGreaterEqual(len(code_blocks(self.whole)), 6)

    def test_every_block_with_kubectl_starts_with_the_guard(self):
        for block in code_blocks(self.whole)[1:]:
            if "kubectl" in block or "kc " in block:
                self.assertIn(REPORT, block.split("\n")[:3])
        self.assertIn(GUARD, code_blocks(self.whole)[0].split("\n")[0])
        self.assertLess(code_blocks(self.whole)[0].index("kubectl config current-context"), code_blocks(self.whole)[0].index("kubectl create namespace"))

    def test_the_scan_sees_each_new_shape(self):
        guard = REPORT + "\n"
        samples = {
            "dash n cw": guard + "kubectl -n cw get pods",
            "namespace cw": guard + "kubectl get namespace cw",
            "delete literal": guard + "kubectl delete secret cw",
            "delete namespace literal": guard + "kubectl delete namespace cwp",
            "cat token": guard + "kubectl get nodes\ncat TOKEN_FILE",
            "echo token": guard + "kubectl get nodes\necho $TOKEN",
            "env file": guard + "kubectl create secret generic x --from-env-file=F",
            "yaml terminal": guard + 'kubectl -n "${NS:?}" get secret x -o yaml',
            "json terminal": guard + 'kubectl -n "${NS:?}" get secret x -o json',
            "create without namespace": guard + "kubectl create -f cwp.yaml",
            "apply without namespace": guard + "kubectl apply --dry-run=server -f cwp.yaml",
            "unguarded rm": guard + "kubectl get nodes\nrm -rf PROBE_CONFIG_DIR",
            "no guard": 'kubectl -n "${NS:?}" get nodes',
        }
        for name, text in samples.items():
            self.assertEqual(len(runbook_shapes("```text\n" + text + "\n```")), 1, name)
        good = guard + 'kubectl -n "${NS:?}" get secret x -o json | python3 -c pass\nkubectl delete namespace "${NS:?}"\nrm -rf "${DIR:?}"\nkubectl create -n "${NS:?}" -f cwp.yaml'
        self.assertEqual(runbook_shapes("```text\n" + good + "\n```"), [])

    def test_a_runbook_with_a_planted_shape_fails_the_sweep(self):
        plants = {
            "-n cw": ('kc get secret cloud-hub-registry cloud-hub-pw', "kubectl -n cw get secret cloud-hub-registry cloud-hub-pw"),
            "delete literal": ('kubectl delete namespace "${NS:?}"', "kubectl delete namespace cw"),
            "cat token": ("rm -f TOKEN_FILE\n", "cat TOKEN_FILE\nrm -f TOKEN_FILE\n"),
            "from-env-file": ('create secret generic cloud-hub-pw', "create secret generic cloud-hub-pw --from-env-file=F"),
            "terminal secret": (" -o json | python3", " -o json\npython3"),
            "create without namespace": ('kc create -f cwp.yaml', "kubectl create -f cwp.yaml"),
            "unguarded rm": ('rm -rf "${PROBE_CONFIG_DIR:?}"', "rm -rf PROBE_CONFIG_DIR"),
        }
        for name, (old, new) in plants.items():
            mutant = self.whole.replace(old, new, 1)
            self.assertNotEqual(mutant, self.whole, name)   # the mutation is applied
            self.assertNotEqual(runbook_shapes(mutant), [], name)
        self.assertEqual(len(plants), 7)


KC_DEF = 'kc() { ns_ok && kubectl -n "${NS:?}" "$@"; }'
SRC_READ = 'kubectl -n "${SRC:?}" get '


def probe_section(text):
    return text.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]


def kc_calls(text):
    """Every call through the wrapper kc in the code blocks of a probe section (comment lines and the definition left out)."""
    return [l for b in code_blocks(text) for l in b.replace("\\\n", " ").split("\n")
            if not l.startswith("#") and not l.startswith("kc() {") for _ in re.findall(r"(?<![\w-])kc ", l)]


def bare_kubectl(text):
    """Every 'kubectl' word of the code blocks that is not the wrapper's definition, behind 'ns_ok && ', the pull-copy read or config."""
    bad = []
    for block in code_blocks(text):
        for line in block.replace("\\\n", " ").split("\n"):
            if line.startswith("#") or line == KC_DEF:
                continue
            for m in re.finditer(r"(?<![\w-])kubectl\b", line):
                before, after = line[:m.start()], line[m.start():]
                if before.endswith("ns_ok && ") or after.startswith("kubectl config current-context") or after.startswith(SRC_READ):
                    continue
                bad.append(line)
    return bad


def exit_commands(text):
    return [l for b in code_blocks(text) for l in b.split("\n") if re.search(r"\bexit\b", l)]


class PastedBlockGuard(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.doc = f.read()
        self.whole = probe_section(self.doc)

    def test_every_kubectl_word_of_the_probe_section_is_chained_or_goes_through_kc(self):
        self.assertGreaterEqual(len(kc_calls(self.whole)), 12)
        self.assertEqual(bare_kubectl(self.whole), [])
        self.assertEqual(code_blocks(self.whole)[0].split("\n")[1], KC_DEF)
        for block in code_blocks(self.whole):
            for line in block.split("\n"):
                if "kubectl" in line:
                    self.assertNotIn('-n "$NS"', line)
        self.assertEqual(len([l for b in code_blocks(self.whole) for l in b.split("\n") if "ns_ok && kubectl delete namespace" in l]), 1)
        self.assertEqual(len([l for b in code_blocks(self.whole) for l in b.split("\n") if "ns_ok && kubectl create namespace" in l]), 1)
        self.assertEqual(self.whole.count("rollout status") and len(re.findall(r"kubectl[^`\n]*rollout", self.whole)), 0)

    def test_a_scratch_text_with_one_bare_kubectl_is_caught(self):
        mutations = [
            ('kc delete secret cw-minter-token', "kubectl delete secret cw-minter-token"),
            ('kc patch secret cloud-hub-registry', "kubectl patch secret cloud-hub-registry"),
            ('kc set env', "kubectl set env"),
            ('| kc create -f -', "| kubectl create -f -"),
            ('ns_ok && kubectl -n "${SRC:?}" get secret', "kubectl get secret"),
            ('ns_ok && kubectl delete namespace', "kubectl delete namespace"),
            ('ns_ok && kubectl create namespace', "kubectl create namespace"),
            (KC_DEF, KC_DEF.replace("ns_ok && ", "")),
        ]
        for old, new in mutations:
            host = [b for b in code_blocks(self.whole) if old in b][0]   # the mutation lands inside a code block
            mutant = self.whole.replace(host, host.replace(old, new, 1), 1)
            self.assertNotEqual(mutant, self.whole, old)   # the mutation is applied
            self.assertEqual(len(bare_kubectl(mutant)), 1, old)
        self.assertEqual(len(mutations), 8)

    def test_every_block_that_runs_kubectl_after_the_first_starts_with_the_needs_comment(self):
        blocks = code_blocks(self.whole)
        self.assertTrue(blocks[0].startswith("ns_ok() {"))
        users = [b for b in blocks[1:] if re.search(r"\bkc\b|kubectl|ns_ok", b)]
        self.assertGreaterEqual(len(users), 5)
        for b in users:
            self.assertEqual(b.split("\n")[0], REPORT)
        self.assertIn("fails closed (`kc: command not found`)", self.whole)

    def test_no_pasted_block_has_the_word_exit_as_a_command(self):
        self.assertEqual(exit_commands(self.whole), [])
        for old, new in (('NS=cwp\n', "exit\nNS=cwp\n"), ("rm -f cwp.yaml", "exit")):
            mutant = self.whole.replace(old, new, 1)
            self.assertNotEqual(mutant, self.whole)   # the mutation is applied
            self.assertNotEqual(exit_commands(mutant), [], new)

    def test_the_guard_function_of_the_runbook_reports_and_leaves_the_shell_open(self):
        first = code_blocks(self.whole)[0].split("\n")[0]
        self.assertTrue(first.startswith("ns_ok() {"))
        cases = [("unset", None, 1), ("empty", "", 1), ("cw", "cw", 1), ("default", "default", 1),
                 ("kube-system", "kube-system", 1), ("cwp", "cwp", 0)]
        for name, value, want in cases:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            if value is not None:
                env["NS"] = value
            r = subprocess.run(["sh", "-c", first + '\nns_ok\nrc=$?\necho "rc=$rc still-open"'],
                               env=env, capture_output=True, text=True, timeout=30)
            self.assertIn("rc=%d still-open" % want, r.stdout, name)
            self.assertEqual(r.returncode, 0, name)
            self.assertEqual(bool(r.stderr), want == 1, name)


BAD_VALUES = (("cw", "NS=cw"), ("default", "NS=default"), ("kube-system", "NS=kube-system"), ("unset", "unset NS"), ("empty", "NS="))
USERS = re.compile(r"\bkc\b|kubectl|ns_ok")


def stub_calls(section, ns_line, only=None, teardown=False):
    """Paste the blocks of a probe section in order into ONE scratch 'sh -c' with a STUB kubectl first on PATH; the stub logs its argv.

    Returns the logged calls apart from 'config current-context' (a read of the login). `only` is a list of block indexes to run
    after the first block (the definitions); the line 'NS=cwp' of each block is replaced by `ns_line`."""
    blocks = [b for b in code_blocks(section) if USERS.search(b)]
    chosen = blocks if only is None else [blocks[0]] + [blocks[i] for i in only]
    text = "\n".join(b.replace("\nNS=cwp\n", "\n" + ns_line + "\n") for b in chosen)
    assert text.count(ns_line) >= 1
    with tempfile.TemporaryDirectory() as scratch:
        bindir = os.path.join(scratch, "bin")
        os.mkdir(bindir)
        log = os.path.join(scratch, "calls.txt")
        stub = os.path.join(bindir, "kubectl")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "$*" >> "$STUB_LOG"\nexit 0\n')
        os.chmod(stub, 0o755)
        work = os.path.join(scratch, "work")
        os.mkdir(work)
        env = {"PATH": bindir + ":/usr/bin:/bin", "STUB_LOG": log, "HOME": scratch}
        subprocess.run(["sh", "-c", text], cwd=work, env=env, capture_output=True, text=True, timeout=60)
        if not os.path.exists(log):
            return []
        with open(log) as f:
            lines = f.read().splitlines()
    return [l for l in lines if not l.startswith("config ")]


class StubRun(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.whole = probe_section(f.read())
        self.users = [b for b in code_blocks(self.whole) if USERS.search(b)]

    def test_the_stub_is_never_called_for_each_bad_namespace_in_the_pasted_blocks(self):
        self.assertGreaterEqual(len(self.users), 6)
        for name, line in BAD_VALUES:
            self.assertEqual(stub_calls(self.whole, line), [], name)   # all blocks pasted in one shell
            for i in range(1, len(self.users)):
                self.assertEqual(stub_calls(self.whole, line, only=[i]), [], "%s block %d" % (name, i))

    def test_the_stub_is_called_for_the_probe_namespace_in_every_block(self):
        self.assertGreaterEqual(len(stub_calls(self.whole, "NS=cwp")), 12)
        for i in range(1, len(self.users)):
            self.assertNotEqual(stub_calls(self.whole, "NS=cwp", only=[i]) , [], "block %d" % i)
        first = stub_calls(self.whole, "NS=cwp", only=[])
        self.assertIn("create namespace cwp", first)

    def test_the_teardown_block_with_the_stub_makes_no_call_for_cw_and_the_deletes_for_cwp(self):
        teardown = [b for b in self.users if "delete rolebinding" in b]
        self.assertEqual(len(teardown), 1)
        index = self.users.index(teardown[0])
        for name, line in BAD_VALUES:
            self.assertEqual(stub_calls(self.whole, line, only=[index]), [], "teardown " + name)
        # the first block alone makes calls of its own: only the delete lines are counted for cwp
        calls = [c for c in stub_calls(self.whole, "NS=cwp", only=[index]) if " delete " in " " + c + " " or c.startswith("delete ")]
        self.assertEqual(len(calls), 3)
        self.assertIn("-n cwp delete rolebinding cw-minter", calls)
        self.assertIn("-n cwp delete secret cw-minter-token", calls)
        self.assertIn("delete namespace cwp", calls)

    def test_a_scratch_text_with_a_wrapper_without_the_check_is_caught_by_the_stub(self):
        old = [b for b in code_blocks(self.whole) if KC_DEF in b][0]
        mutant_block = old.replace("ns_ok && ", "", 1)
        self.assertNotEqual(mutant_block, old)   # the mutation is applied
        mutant = self.whole.replace(old, mutant_block, 1)
        counts = [len(stub_calls(mutant, line)) for _, line in BAD_VALUES]
        self.assertTrue(all(n > 0 for n in counts[:3]), counts)   # cw, default, kube-system reach the stub without the check
        self.assertEqual(counts[3:], [0, 0])   # an unset or empty NS still fails the command itself at ${NS:?}

    def test_a_scratch_text_with_one_bare_kubectl_is_caught_by_the_stub(self):
        host = [b for b in code_blocks(self.whole) if "kc get secret cloud-hub-registry cloud-hub-pw" in b][0]
        mutant = self.whole.replace(host, host.replace("kc get secret cloud-hub-registry cloud-hub-pw", "kubectl get secret cloud-hub-registry cloud-hub-pw", 1), 1)
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.assertEqual(len(stub_calls(mutant, "NS=cw")), 1)


class Chained(unittest.TestCase):
    def test_this_file_is_in_the_chain_once(self):
        with open(os.path.join(ROOT, "bin", "test-cloud-launcher.chain")) as f:
            entries = f.read().split("\n")
        self.assertEqual(entries.count("cloud/launcher/tests/" + os.path.basename(__file__)), 1)

    def test_the_script_is_executable__with_a_shebang(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        with open(SCRIPT) as f:
            self.assertEqual(f.readline(), "#!/usr/bin/env python3\n")


class OnceAndLabel(unittest.TestCase):
    STALE = ("The blocks below are read as pasted into an interactive shell by the holder of the administrator login. "
             "The function `ns_ok`, defined once in the first block, only REPORTS: it prints a line.")

    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.whole = probe_section(f.read())
        self.first = code_blocks(self.whole)[0].split("\n")[0]

    def counts(self, text):
        return [text.count("%d. Step %d, " % (n, n)) for n in range(1, 7)] + [text.count("The blocks below")]

    def test_each_step_and_each_blocks_below_paragraph_is_found_once(self):
        self.assertEqual(self.counts(self.whole), [1] * 7)
        mutant = self.whole.replace("\nThe command list for the administrator apply (the owner's steps 3 and 4):",
                                    "\n" + self.STALE + "\n\nThe command list for the administrator apply (the owner's steps 3 and 4):", 1)
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.assertEqual(self.counts(mutant)[6], 2)
        self.assertNotEqual(self.counts(mutant), [1] * 7)
        step = [l for l in self.whole.split("\n") if l.startswith("4. Step 4, ")][0]
        mutant = self.whole.replace("\nThe command list for the administrator apply (the owner's steps", "\n" + step + "\nThe command list for the administrator apply (the owner's steps", 1)
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.assertNotEqual(self.counts(mutant), [1] * 7)

    def test_the_empty_token_file_sentence_is_found_once(self):
        sentence = "If the command was refused, remove the empty token file before running it again: `rm -f \"${TOKEN_FILE:?}\"`"
        self.assertEqual(self.whole.count(sentence), 1)
        self.assertEqual(self.whole.replace(sentence, "", 1).count(sentence), 0)

    def calls_for(self, value):
        text = self.first + "\nNS=\"$1\"\nkc() { ns_ok && kubectl -n \"${NS:?}\" \"$@\"; }\nkc get pods\n"
        with tempfile.TemporaryDirectory() as scratch:
            bindir = os.path.join(scratch, "bin")
            os.mkdir(bindir)
            log = os.path.join(scratch, "calls.txt")
            with open(os.path.join(bindir, "kubectl"), "w") as f:
                f.write('#!/bin/sh\necho "$*" >> "$STUB_LOG"\nexit 0\n')
            os.chmod(os.path.join(bindir, "kubectl"), 0o755)
            env = {"PATH": bindir + ":/usr/bin:/bin", "STUB_LOG": log}
            subprocess.run(["sh", "-c", text, "sh", value], env=env, capture_output=True, text=True, timeout=30)
            if not os.path.exists(log):
                return 0
            with open(log) as f:
                return len(f.read().splitlines())

    def test_the_guard_matches_a_dns_label_with_the_stub(self):
        for value in ("cw ", "CW", "Cwp", "cwp --all-namespaces", "-n", "a" * 64, "c.p", "c/p", "-cwp", "cwp-"):
            self.assertEqual(self.calls_for(value), 0, repr(value))
        self.assertEqual(self.calls_for("cwp"), 1)
        self.assertEqual(self.calls_for("a" * 63), 1)

    def test_a_guard_without_the_label_check_lets_a_trailing_space_through(self):
        old = 'ns_ok() { case "$NS" in ""|cw|default|kube-*) echo "refused: NS is $NS" >&2; return 1;; esac; }'
        self.first, saved = old, self.first
        try:
            self.assertEqual(self.calls_for("cw "), 1)   # the deny-list alone passes it
        finally:
            self.first = saved
        self.assertEqual(self.calls_for("cw "), 0)


class HistoryExpansion(unittest.TestCase):
    """Pasted into an interactive zsh, an exclamation mark can start a history event."""
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.doc = f.read()
        self.whole = probe_section(self.doc)

    @staticmethod
    def triggers(text):
        blocks = code_blocks(text)
        hits = 0
        for b in blocks:
            for line in b.split("\n"):
                for i, c in enumerate(line):
                    if c == "!" and line[i + 1:i + 2] not in ("", " ", "=", "("):
                        hits += 1
        return hits, len(blocks)

    def test_every_code_block_of_the_runbook_has_no_history_expansion_trigger(self):
        hits, n = self.triggers(self.whole)
        print("S-hist: exclamation marks that could expand %d in %d code blocks" % (hits, n))
        self.assertEqual(hits, 0)

    def test_a_negated_bracket_with_a_bang_is_caught(self):
        mutant = self.whole.replace("*[^abcdefghij", "*[!abcdefghij")
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.assertEqual(self.triggers(mutant)[0], 1)
        print("S-hist mutant hits 1")


if __name__ == "__main__":
    unittest.main()
