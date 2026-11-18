"""Infra Bicep: files, resource types, no private values, params complete, README rules. Offline."""
import os, re, shutil, subprocess, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PLACEHOLDER = re.compile(r"^0{8}-0{4}-0{4}-0{4}-0{12}$|^0{8}-0{4}-0{4}-0{4}-0{11}[0-9]$")
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IPV4 = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
OK_IPS = {"203.0.113.10", "10.244.0.0", "10.0.0.0", "10.0.0.10"}
TYPES = [
    "Microsoft.ContainerService/managedClusters", "Microsoft.App/sandboxGroups",
    "Microsoft.ContainerRegistry/registries", "Microsoft.KeyVault/vaults",
    "Microsoft.Storage/storageAccounts", "Microsoft.DBforPostgreSQL/flexibleServers",
    "Microsoft.ManagedIdentity/userAssignedIdentities",
    "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials",
    "Microsoft.Authorization/roleAssignments", "Microsoft.Consumption/budgets",
    "Microsoft.DBforPostgreSQL/flexibleServers/firewallRules",
]


def slurp(p):
    with open(p) as f:
        return f.read()


def bicep_files(infra):
    out = [os.path.join(infra, "main.bicep")]
    mdir = os.path.join(infra, "modules")
    if os.path.isdir(mdir):
        out += [os.path.join(mdir, n) for n in sorted(os.listdir(mdir)) if n.endswith(".bicep")]
    return out


def check_types(infra):
    text = ""
    for p in bicep_files(infra):
        if not os.path.exists(p):
            return ["missing file: " + os.path.relpath(p, infra)]
        text += slurp(p)
    return ["missing resource type: " + t for t in TYPES if t + "@" not in text]


def check_private(infra):
    bad = []
    for d, _, names in os.walk(infra):
        for n in names:
            p = os.path.join(d, n)
            for i, line in enumerate(slurp(p).splitlines(), 1):
                for u in UUID.findall(line):
                    if not PLACEHOLDER.match(u):
                        bad.append("non-placeholder uuid at %s:%d" % (n, i))
                for ip in IPV4.findall(line):
                    if ip not in OK_IPS:
                        bad.append("non-placeholder ip at %s:%d" % (n, i))
    return bad


def required_params(main_text):
    """Names of `param x type` declarations without a default."""
    out = []
    for m in re.finditer(r"^param\s+(\w+)\s+(\w+)(.*)$", main_text, re.M):
        if "=" not in m.group(3):
            out.append(m.group(1))
    return out


def missing_params(main_text, param_text):
    given = set(re.findall(r"^param\s+(\w+)\s*=", param_text, re.M))
    return ["missing parameter: " + n for n in required_params(main_text) if n not in given]


def scratch(testcase):
    d = tempfile.mkdtemp(prefix="infra-scratch-")
    testcase.addCleanup(shutil.rmtree, d, True)
    dst = os.path.join(d, "infra")
    shutil.copytree(os.path.join(ROOT, "infra"), dst)
    return dst


INFRA = os.path.join(ROOT, "infra")


class TestFiles(unittest.TestCase):
    def test_types_present(self):
        self.assertEqual(check_types(INFRA), [])

    def test_mutant_missing_type(self):
        d = scratch(self)
        for p in bicep_files(d):
            text = slurp(p).replace("Microsoft.ContainerRegistry/registries@", "Microsoft.Other/x@")
            with open(p, "w") as f:
                f.write(text)
        self.assertEqual(check_types(d), ["missing resource type: Microsoft.ContainerRegistry/registries"])


class TestNoPrivate(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(check_private(INFRA), [])

    def test_mutant_uuid(self):
        d = scratch(self)
        with open(os.path.join(d, "main.bicep"), "a") as f:
            f.write("// 12345678-1234-1234-1234-123456789abc\n")
        self.assertEqual(check_private(d), ["non-placeholder uuid at main.bicep:%d" % len(slurp(os.path.join(d, "main.bicep")).splitlines())])


class TestParams(unittest.TestCase):
    def test_example_complete(self):
        self.assertTrue(slurp(os.path.join(INFRA, "example.bicepparam")).startswith("using './main.bicep'"))
        self.assertEqual(missing_params(slurp(os.path.join(INFRA, "main.bicep")), slurp(os.path.join(INFRA, "example.bicepparam"))), [])
        self.assertIn("tenantId", required_params(slurp(os.path.join(INFRA, "main.bicep"))))

    def test_mutant_missing_param(self):
        d = scratch(self)
        p = os.path.join(d, "example.bicepparam")
        text = re.sub(r"^param tenantId = .*\n", "", slurp(p), flags=re.M)
        with open(p, "w") as f:
            f.write(text)
        before = sorted(os.listdir(d))
        self.assertEqual(missing_params(slurp(os.path.join(d, "main.bicep")), text), ["missing parameter: tenantId"])
        self.assertEqual(sorted(os.listdir(d)), before)  # no output file written


class TestGitignore(unittest.TestCase):
    def ignored(self, p):
        return subprocess.run(["git", "check-ignore", "-q", p], cwd=ROOT).returncode

    def test_real_ignored_example_kept(self):
        self.assertEqual(self.ignored("infra/production.bicepparam"), 0)
        self.assertEqual(self.ignored("infra/example.bicepparam"), 1)


@unittest.skipUnless(shutil.which("bicep"), "bicep CLI not installed")
class TestBicepCli(unittest.TestCase):
    def test_build_params(self):
        d = tempfile.mkdtemp(prefix="infra-bicep-")
        self.addCleanup(shutil.rmtree, d, True)
        r = subprocess.run(["bicep", "build-params", os.path.join(INFRA, "example.bicepparam"), "--outfile", os.path.join(d, "o.json")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestReadme(unittest.TestCase):
    def setUp(self):
        self.t = slurp(os.path.join(INFRA, "README.md"))

    def test_rules(self):
        for s in ["--mode Incremental", "--exclude-change-types Ignore NoEffect", "NEVER `--mode Complete`",
                  "infra-live-bicepparam", "No new identity gets Key Vault access", "operator only",
                  "NEVER target the AKS node resource group", "## Resource groups", "## Rollback",
                  "## What-if", "## Deploy", "Smoke checks", "Run-by-hand mutant"]:
            self.assertIn(s, self.t)

    def test_no_complete_command(self):
        for line in self.t.splitlines():
            if line.startswith("az deployment"):
                self.assertNotIn("Complete", line)
        self.assertNotRegex(self.t, r"--mode\s+Complete(?!`)")


if __name__ == "__main__":
    unittest.main()
