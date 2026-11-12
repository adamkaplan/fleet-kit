"""Ask #2489: the containerd fix removes the startup taint from its own node, only after the runtime is verified.

Offline: the manifest's own fix.sh runs against a fake host, a fake label command and a fake taint command (a stand-in for kubectl
taint), one shared call log, so the ORDER of the calls is read. No yaml library exists in the repository: tolerations are read as text.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
K8S = os.path.join(ROOT, "cloud", "k8s")
KEY = "startup-taint.cluster-autoscaler.kubernetes.io/sysbox-not-ready"
with open(os.path.join(K8S, "containerd-fix.yaml")) as _f:
    FIX_YAML = _f.read()


def extract_script(text):
    m = re.search(r"^  fix\.sh: \|\n(.*?)^---$", text, re.S | re.M)
    return "\n".join(l[4:] for l in m.group(1).split("\n"))


SCRIPT = extract_script(FIX_YAML)
LEGACY = '''version = 3
[plugins.'io.containerd.cri.v1.runtime'.containerd.runtimes.runc]
  runtime_type = 'io.containerd.runc.v2'
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.sysbox-runc]
  runtime_type = "io.containerd.runc.v2"
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.sysbox-runc.options]
  BinaryName = "/usr/bin/sysbox-runc"
'''
RIGHT = LEGACY.replace("grpc.v1.cri", "cri.v1.runtime")
HOST = '''#!/bin/sh
echo "$@" >> "$TMP_LOG"
case "$1 $2" in
  "containerd config") [ "${DUMP_OK:-1}" = 1 ] || exit 1; cat "$CFG" ;;
  "systemctl restart") [ "${RESTART_FAIL:-0}" = 1 ] && exit 1; exit 0 ;;
  "timeout 10") [ "${CTR_DOWN:-0}" = 1 ] && exit 1; exit 0 ;;
esac
'''


def sections(text):
    return [d for d in re.split(r"^---\s*$", text, flags=re.M) if d.strip()]


def tolerations(doc):
    """The (key, operator, value, effect) tuples of a doc's `tolerations:` block (the subset the manifests use)."""
    m = re.search(r"^( *)tolerations:\n((?:\1 +.*\n?|\s*\n)*)", doc, re.M)
    if not m:
        return []
    items, cur = [], None
    for line in m.group(2).split("\n"):
        mm = re.match(r"\s*(- )?(\w+):\s*(.*)$", line)
        if not mm:
            continue
        if mm.group(1):
            cur = {}
            items.append(cur)
        cur[mm.group(2)] = mm.group(3).strip().strip('"')
    return [(i.get("key"), i.get("operator"), i.get("value"), i.get("effect")) for i in items]


class Run:
    def __init__(self, script=SCRIPT, config=LEGACY):
        self.tmp = tempfile.mkdtemp(prefix="startup-taint-")
        self.cfg = os.path.join(self.tmp, "config.toml")
        self.log = os.path.join(self.tmp, "log")
        open(self.cfg, "w").write(config)
        open(os.path.join(self.tmp, "fix.sh"), "w").write(script)
        for name, body in (("host", HOST), ("label", '#!/bin/sh\necho label >> "$TMP_LOG"\n'),
                           ("taint", '#!/bin/sh\necho "taint-removed $NODE_NAME" >> "$TMP_LOG"\n'),
                           ("ready", "#!/bin/sh\nexit 0\n")):
            p = os.path.join(self.tmp, name)
            open(p, "w").write(body)
            os.chmod(p, 0o755)

    def run(self, **env):
        open(self.log, "w").close()
        e = dict(os.environ, TMP_LOG=self.log, CFG=self.cfg, HOST=os.path.join(self.tmp, "host"),
                 LABEL_CMD=os.path.join(self.tmp, "label"), TAINT_CMD=os.path.join(self.tmp, "taint"),
                 READY_CMD=os.path.join(self.tmp, "ready"), NODE_NAME="node-1", ONCE="1", WAIT="2", POLL="1")
        e.update(env)
        self.rc = subprocess.run(["sh", os.path.join(self.tmp, "fix.sh")], env=e, capture_output=True, text=True, timeout=60).returncode
        self.calls = [l.strip() for l in open(self.log) if l.strip()]
        return self

    def removals(self):
        return [i for i, c in enumerate(self.calls) if c.startswith("taint-removed")]

    def restarts(self):
        return [i for i, c in enumerate(self.calls) if c == "systemctl restart containerd"]

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def check_s2(script):
    r = Run(script).run()
    try:
        assert r.rc == 0, "the fix exited %s" % r.rc
        rem, res = r.removals(), r.restarts()
        assert len(rem) == 1, "the startup taint was never removed" if not rem else "removed %d times" % len(rem)
        assert len(res) == 1, "expected one restart, saw %d" % len(res)
        checks = [i for i, c in enumerate(r.calls) if c.startswith("containerd config dump")]
        assert rem[0] > res[0] and rem[0] > max(checks), "the removal came before the restart or the check"
        assert [c for c in r.calls if c.startswith("timeout 10 ctr version")] and rem[0] > min(
            i for i, c in enumerate(r.calls) if c.startswith("timeout 10 ctr version")), "removal before the health check"
    finally:
        r.close()


def never_removes(script):
    out = re.sub(r"^(\s*)remove_startup_taint(?:\s+#.*)?$", r"\1true", script, flags=re.M)
    assert out != script
    return out


class StartupTaint(unittest.TestCase):
    def test_s2_removal_after_restart_and_check(self):
        check_s2(SCRIPT)

    def test_s3_already_right_removes_once_no_restart(self):
        r = Run(config=RIGHT).run()
        self.addCleanup(r.close)
        self.assertEqual(r.rc, 0)
        self.assertEqual(len(r.removals()), 1)
        self.assertEqual(len(r.restarts()), 0)

    def test_s4_failed_repair_and_cap_off_remove_nothing(self):
        r = Run().run(DUMP_OK="0")           # the config dump check fails
        self.addCleanup(r.close)
        self.assertNotEqual(r.rc, 0)
        self.assertEqual(r.removals(), [])
        r2 = Run().run(RESTART_FAIL="1")     # the restart fails: rolled back
        self.addCleanup(r2.close)
        self.assertNotEqual(r2.rc, 0)
        self.assertEqual(r2.removals(), [])
        r3 = Run()                           # at the cap
        self.addCleanup(r3.close)
        open(r3.cfg + ".sysbox-fix.failed", "w").write("count=2\n")
        r3.run()
        self.assertEqual(r3.rc, 4)
        self.assertEqual(r3.removals(), [])
        r4 = Run(config=RIGHT)               # already right but containerd not answering: not verified
        self.addCleanup(r4.close)
        r4.run(CTR_DOWN="1")
        self.assertEqual(r4.removals(), [])

    def test_absent_taint_is_not_an_error(self):
        r = Run(config=RIGHT)
        self.addCleanup(r.close)
        p = os.path.join(r.tmp, "taint")
        open(p, "w").write('#!/bin/sh\necho "taint \\"x\\" not found" >&2\nexit 1\n')
        r.run()
        self.assertEqual(r.rc, 0)

    def test_the_default_command_names_the_key_and_the_node(self):
        self.assertIn('kubectl taint node $NODE_NAME $TAINT_KEY=true:NoSchedule-', SCRIPT)
        self.assertIn('TAINT_KEY="%s"' % KEY, SCRIPT)

    def test_s5_manifests_tolerate_exactly_the_taint(self):
        ds = [d for d in sections(FIX_YAML) if re.search(r"^kind: DaemonSet", d, re.M)]
        self.assertEqual(len(ds), 1)
        want = [(KEY, "Equal", "true", "NoSchedule")]
        self.assertEqual(tolerations(ds[0]), want)
        with open(os.path.join(K8S, "patches", "sysbox-install-toleration.yaml")) as f:
            patch = f.read()
        self.assertEqual(tolerations(patch), want)

    def test_s5_control_a_manifest_without_it_is_caught(self):
        self.assertEqual(tolerations(FIX_YAML.replace("tolerations:", "other:")), [])

    def test_s6_mutant_that_never_removes_is_caught(self):
        check_s2(SCRIPT)                       # the control first
        with self.assertRaises(AssertionError) as cm:
            check_s2(never_removes(SCRIPT))
        self.assertIn("the startup taint was never removed", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
