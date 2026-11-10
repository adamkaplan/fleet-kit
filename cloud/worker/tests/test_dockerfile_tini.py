"""Ask #2353: the worker image installs tini in its own RUN (package lists removed) before the scan stage, and its ENTRYPOINT runs
the entrypoint script under tini, so an init is the pod's first process; CMD and the run user are unchanged.
Offline: reads the Dockerfile only. Scenarios S2, S3."""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
DF = os.path.join(os.path.dirname(os.path.dirname(HERE)), "worker", "Dockerfile")
WANT = 'ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/worker-entrypoint"]'
OLD = 'ENTRYPOINT ["/usr/local/bin/worker-entrypoint"]'


def read():
    with open(DF) as f:
        return f.read()


def problems(t):
    out = []
    w = t[t.find("AS worker"):t.find("FROM worker AS scan")]
    flat = w.replace("\\\n", " ")
    installs = [l for l in flat.split("\n") if l.startswith("RUN ") and re.search(r"apt-get install\b[^\n]*\btini\b", l)]
    if len(installs) != 1 or "--no-install-recommends" not in installs[0] or "rm -rf /var/lib/apt/lists/*" not in installs[0]:
        out.append("tini must be installed once, without recommends, lists removed")
    ents = re.findall(r"(?m)^ENTRYPOINT .*$", w)
    if ents != [WANT]:
        out.append("the pod has no init as its first process")
    if not re.search(r'(?m)^CMD \["opencode", "serve", "--hostname", "0\.0\.0\.0", "--port", "4096"\]$', w):
        out.append("CMD changed")
    users = re.findall(r"(?m)^USER (\S+)$", w)
    if not users or users[-1] != "worker":
        out.append("the last USER is not worker")
    if installs and w.find(installs[0].split(" && ")[0][:40]) > w.find(WANT) >= 0:
        out.append("tini installed after the ENTRYPOINT")
    return out


class T(unittest.TestCase):
    def test_real_file_clean(self):
        self.assertEqual(problems(read()), [])

    def test_install_is_in_the_worker_stage_before_the_scan(self):
        t = read()
        self.assertLess(t.find("apt-get install -y --no-install-recommends tini"), t.find("FROM worker AS scan"))
        self.assertGreater(t.find("apt-get install -y --no-install-recommends tini"), t.find("AS worker"))

    def test_mutant_old_entrypoint_is_refused(self):
        t = read()
        self.assertEqual(problems(t), [])  # control first
        self.assertEqual(t.count(WANT), 1)
        m = t.replace(WANT, OLD, 1)
        self.assertNotEqual(m, t)
        self.assertIn("the pod has no init as its first process", problems(m))

    def test_mutant_no_install_is_refused(self):
        t = read()
        self.assertEqual(problems(t), [])
        m = t.replace("--no-install-recommends tini", "--no-install-recommends make", 1)
        self.assertNotEqual(m, t)
        self.assertTrue(problems(m))


if __name__ == "__main__":
    unittest.main()
