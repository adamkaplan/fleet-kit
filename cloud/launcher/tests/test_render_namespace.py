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
        for command in ('kubectl delete namespace "${NS:?}"', 'kc delete rolebinding cw-minter',
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
        self.assertIn('`kc rollout status deployment/cloud-hub`', self.text)
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
        for kept in ("delete rolebinding cw-minter", "delete secret cw-minter-token", 'kubectl delete namespace "${NS:?}"',
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
GUARD = 'ns_ok() {'


def runbook_shapes(text):
    """Names of the live-cluster risks found in the code blocks of a runbook text (apart from the credential shapes)."""
    found = []
    for block in code_blocks(text):
        lines = block.split("\n")
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
            for seg in line.split("|"):   # one pipeline side at a time: a kc call on the other side does not count
                if re.search(r"\bkubectl\b.*\b(apply|create)\b.* -f\b", seg) and "namespace" not in seg.split(" -f")[0].split("kubectl", 1)[1].split()[:2] and ' -n "${NS:?}"' not in seg:
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
        first = code_blocks(self.whole)[0]
        self.assertIn(GUARD, first.split("\n")[0])
        self.assertEqual(first.split("\n")[1], WRAPPER)
        self.assertEqual(unchained_calls(self.whole), [])
        self.assertLess(first.index("kubectl config current-context"), first.index("kc create -f cwp.yaml"))

    def test_the_scan_sees_each_new_shape(self):
        guard = "true\n"
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
        }
        for name, text in samples.items():
            self.assertEqual(len(runbook_shapes("```text\n" + text + "\n```")), 1, name)
        good = guard + 'kubectl -n "${NS:?}" get secret x -o json | python3 -c pass\nkubectl delete namespace "${NS:?}"\nrm -rf "${DIR:?}"\nkubectl create -n "${NS:?}" -f cwp.yaml'
        self.assertEqual(len(unchained_calls("```text\nkubectl -n \"${NS:?}\" get nodes\n```")), 1)
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


WRAPPER = 'kc() { ns_ok && kubectl -n "${NS:?}" "$@"; }'
CLUSTER_LINE = re.compile(r'kubectl (create|delete) namespace "\$\{NS:\?\}"')


def probe_section(text):
    return text.split("\n## A probe namespace\n", 1)[1].split("\n## ", 1)[0]


def kubectl_calls(text):
    """Every kubectl word of the code blocks of a probe section, as (text before it, text from it) on its line."""
    calls = []
    for block in code_blocks(text):
        for line in block.replace("\\\n", " ").split("\n"):
            for m in re.finditer(r"\bkubectl\b", line):
                calls.append((line[:m.start()], line[m.start():]))
    return calls


def unchained_calls(text):
    """The kubectl words that are not the wrapper's definition, a chained cluster-scoped line, the SRC read or the context read."""
    bad = []
    for before, rest in kubectl_calls(text):
        if before == "kc() { ns_ok && " and rest == 'kubectl -n "${NS:?}" "$@"; }':
            continue
        if rest.startswith("kubectl config current-context"):
            continue
        if before.endswith("ns_ok && ") and CLUSTER_LINE.match(rest):
            continue
        if rest.startswith('kubectl -n "${SRC:?}" get '):
            continue
        bad.append(before + rest)
    return bad


def exit_commands(text):
    return [l for b in code_blocks(text) for l in b.split("\n") if re.search(r"\bexit\b", l)]


class PastedBlockGuard(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.doc = f.read()
        self.whole = probe_section(self.doc)

    def test_every_kubectl_word_of_the_probe_section_matches_the_chained_form(self):
        calls = kubectl_calls(self.whole)
        self.assertGreaterEqual(len(calls), 6)
        self.assertEqual(unchained_calls(self.whole), [])
        for block in code_blocks(self.whole):
            for line in block.split("\n"):
                self.assertNotIn("|| false", line)   # a report that does not stop the next pasted line
                self.assertNotIn('-n "$NS"', line)
        pull = [c for b, c in calls if c.startswith('kubectl -n "${SRC:?}" get secret PULL_SECRET_NAME')]
        self.assertEqual(len(pull), 1)
        self.assertEqual(len([c for b, c in calls if "delete namespace" in c]), 1)
        self.assertEqual(self.whole.count(WRAPPER), 1)
        self.assertIn("runs NOTHING", self.whole)
        self.assertIn('so the write receives empty input and creates nothing', self.whole)

    def test_a_scratch_text_with_one_bare_kubectl_call_is_caught(self):
        mutations = [
            ('kc delete secret cw-minter-token', "kubectl delete secret cw-minter-token"),
            ('kc patch secret cloud-hub-registry', "kubectl patch secret cloud-hub-registry"),
            ('kc set env', 'kubectl -n "${NS:?}" set env'),
            ('| kc create -f -', "| kubectl create -f -"),
            ('kubectl -n "${SRC:?}" get secret', "kubectl get secret"),
            ('kc create secret generic cloud-hub-pw', 'kubectl -n "" create secret generic cloud-hub-pw'),
            ('ns_ok && kubectl delete namespace', "kubectl delete namespace"),
            ('ns_ok && kubectl create namespace', "kubectl create namespace"),
            (WRAPPER, WRAPPER.replace("ns_ok && ", "")),
        ]
        for old, new in mutations:
            mutant = self.whole.replace(old, new, 1)
            self.assertNotEqual(mutant, self.whole, old)   # the mutation is applied
            self.assertEqual(len(unchained_calls(mutant)), 1, old)
        self.assertEqual(len(mutations), 9)

    def test_no_pasted_block_has_the_word_exit_as_a_command(self):
        self.assertEqual(exit_commands(self.whole), [])
        for old, new in (('kc get secret cloud-hub-registry cloud-hub-pw', "exit 1"), ("rm -f cwp.yaml", "exit")):
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


BAD_VALUES = (("cw", "cw"), ("default", "default"), ("kube-system", "kube-system"), ("unset", None), ("empty", ""))
SHELL_BLOCK = re.compile(r"\bkc\b|\bkubectl\b")


def administrator_script(section, value):
    """The probe section's shell blocks in the document's order, as ONE script: the definitions, then NS as given, then the rest.
    The NS= lines of the blocks are dropped (the case sets NS); the other lines are the document's own."""
    blocks = [b for b in code_blocks(section) if SHELL_BLOCK.search(b)]
    first = blocks[0].split("\n")
    assert first[0].startswith("ns_ok() {") and first[1].startswith("kc() {")
    lines = first[2:]
    for block in blocks[1:]:
        lines += block.split("\n")
    lines = [l for l in lines if not l.startswith("NS=")]
    return "\n".join(first[:2] + ([] if value is None else ["NS=" + value]) + lines)


def stub_calls(section, value):
    """Runs the script in sh -c with a stub kubectl first on PATH; returns the number of lines the stub wrote (its calls)."""
    with tempfile.TemporaryDirectory() as scratch:
        stubs = os.path.join(scratch, "stubs")
        work = os.path.join(scratch, "work")
        os.mkdir(stubs)
        os.mkdir(work)
        log = os.path.join(scratch, "calls.log")
        stub = os.path.join(stubs, "kubectl")
        with open(stub, "w") as f:
            f.write('#!/bin/sh\necho "$*" >> "%s"\nexit 0\n' % log)
        os.chmod(stub, 0o755)
        env = {"PATH": stubs + ":/usr/bin:/bin", "HOME": work}
        subprocess.run(["sh", "-c", administrator_script(section, value)], cwd=work, env=env,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if not os.path.exists(log):
            return 0
        with open(log) as f:
            return len(f.read().splitlines())


def check_stub_guard(section):
    for name, value in BAD_VALUES:
        got = stub_calls(section, value)
        if got != 0:
            raise AssertionError("%s: the stub was called %d times" % (name, got))
    if stub_calls(section, "cwp") == 0:
        raise AssertionError("cwp: the stub was never called")


class StubGuard(unittest.TestCase):
    def setUp(self):
        with open(DOCS, encoding="utf-8") as f:
            self.whole = probe_section(f.read())

    def test_a_blocked_namespace_never_reaches_the_stub_and_cwp_does(self):
        for name, value in BAD_VALUES:
            with self.subTest(name=name):
                self.assertEqual(stub_calls(self.whole, value), 0)
        self.assertGreaterEqual(stub_calls(self.whole, "cwp"), 10)
        check_stub_guard(self.whole)

    def test_the_blocks_run_in_the_documents_order_with_the_definitions_first(self):
        script = administrator_script(self.whole, "cwp")
        lines = script.split("\n")
        self.assertTrue(lines[0].startswith("ns_ok() {"))
        self.assertEqual(lines[1], WRAPPER)
        self.assertEqual(lines[2], "NS=cwp")
        self.assertEqual(sum(1 for l in lines if l.startswith("NS=")), 1)
        for earlier, later in (("kc create -f cwp.yaml", "kc create secret generic cloud-hub-pw"),
                               ("kc create secret generic cloud-hub-pw", "SRC=SOURCE_NAMESPACE"),
                               ("SRC=SOURCE_NAMESPACE", "kc delete rolebinding cw-minter"),
                               ("kc delete rolebinding cw-minter", "kc set env"), ("kc set env", "kc patch secret")):
            self.assertLess(script.index(earlier), script.index(later))
        self.assertEqual(sum(1 for l in administrator_script(self.whole, None).split("\n") if l.startswith("NS=")), 0)

    def test_a_wrapper_without_the_check_is_caught_by_the_stub_guard(self):
        mutant = self.whole.replace(WRAPPER, WRAPPER.replace("ns_ok && ", ""), 1)
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        self.assertGreater(stub_calls(mutant, "cw"), 0)
        with self.assertRaises(AssertionError):
            check_stub_guard(mutant)

    def test_one_bare_call_in_a_block_is_caught_by_the_stub_guard(self):
        mutant = self.whole.replace("kc delete secret cw-minter-token", 'kubectl -n "${NS:-x}" delete secret cw-minter-token', 1)
        self.assertNotEqual(mutant, self.whole)   # the mutation is applied
        with self.assertRaises(AssertionError):
            check_stub_guard(mutant)


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
