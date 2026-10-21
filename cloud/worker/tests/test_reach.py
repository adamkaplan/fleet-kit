"""#1031: what the worker's user can still reach (S4) and where the live-probe plan may point (U3). Offline: scripts and the Dockerfile text only.
The image itself is not built here: the probe script is the thing the image runs (`cw-probe-reach`), tested with stand-in paths."""
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
PROBE = os.path.join(WORKER, "probe-reach.sh")
PROBE_TOKEN = os.path.join(WORKER, "probe-token.py")
DOCKERFILE = os.path.join(WORKER, "Dockerfile")
TABLE = os.path.join(os.path.dirname(WORKER), "token-table.txt")


def slurp(p):
    with open(p) as f:
        return f.read()


class Reach(unittest.TestCase):
    def run_probe(self, gh):
        return subprocess.run(["sh", PROBE], env={"PATH": os.environ.get("PATH", ""), "CW_PROBE_GH": gh}, capture_output=True, text=True, timeout=30)

    def test_S4_an_absent_real_gh_exits_127_and_exactly_three_routes_remain(self):
        p = self.run_probe("/nonexistent/usr/bin/gh")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("exit 127", p.stdout)
        routes = [l for l in p.stdout.splitlines() if l.startswith("route:")]
        self.assertEqual(len(routes), 3, p.stdout)
        for name in ("curl", "python3", "git credential helper"):
            self.assertEqual(sum(name in r for r in routes), 1, name)

    def test_S4_a_reachable_real_gh_fails_the_probe(self):
        with tempfile.TemporaryDirectory() as d:
            gh = os.path.join(d, "gh")
            with open(gh, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(gh, 0o755)
            p = self.run_probe(gh)
            self.assertEqual(p.returncode, 1)
            self.assertIn("FAIL", p.stdout)
            self.assertEqual([l for l in p.stdout.splitlines() if l.startswith("route:")], [])

    def test_S4_the_default_path_is_where_the_package_put_the_real_gh(self):
        self.assertIn('${CW_PROBE_GH:-/usr/bin/gh}', slurp(PROBE))

    def test_S4_the_dockerfile_installs_no_gh_and_no_github_repository_and_copies_the_client_and_the_probe(self):
        text = slurp(DOCKERFILE)
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"(^|\s)gh(\s|\\|$)")
        self.assertNotIn("cli.github.com", code)
        self.assertNotIn("githubcli", code)
        self.assertIn("COPY worker/cw-gh /usr/local/bin/cw-gh", code)
        self.assertIn("COPY worker/probe-reach.sh /usr/local/bin/cw-probe-reach", code)
        self.assertRegex(code, r"chmod 0755[^&]*cw-gh[^&]*cw-probe-reach")
        for f in ("cw-gh", "probe-reach.sh", "probe-token.py"):
            self.assertTrue(os.access(os.path.join(WORKER, f), os.X_OK), f)


class ProbeTarget(unittest.TestCase):
    def run_pt(self, *args, throwaway=None):
        env = {"PATH": os.environ.get("PATH", "")}
        if throwaway is not None:
            env["CW_PROBE_THROWAWAY"] = throwaway
        return subprocess.run([sys.executable, PROBE_TOKEN, *args], env=env, capture_output=True, text=True, timeout=30)

    def test_U3_any_other_target_is_refused_with_exit_2(self):
        for target, throwaway in (("acme-org/widget-service", "acme-org/throwaway"), ("acme-org/throwaway-2", "acme-org/throwaway"),
                                  ("acme-org/throwaway", None), ("acme-org/throwaway", ""), ("acme-org", "acme-org"), ("a/b/c", "a/b/c")):
            with self.subTest(target=target, throwaway=throwaway):
                p = self.run_pt(target, throwaway=throwaway)
                self.assertEqual(p.returncode, 2, p.stdout)
                self.assertIn("refused", p.stdout)

    def test_U3_the_named_throwaway_lists_exactly_the_rows_to_probe_and_makes_no_request(self):
        rows = [l for l in slurp(TABLE).splitlines() if l.strip() and not l.startswith("#") and "to be probed live" in l.split(" | ")[3]]
        p = self.run_pt("Acme-Org/Throwaway", throwaway="acme-org/throwaway")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(len([l for l in p.stdout.splitlines() if l.startswith("probe:")]), len(rows))
        src = slurp(PROBE_TOKEN)
        for needle in ("urllib", "http.client", "socket", "subprocess", "GH_TOKEN", "git-token"):
            self.assertNotIn(needle, src.split('"""', 2)[2], needle)   # no request and no credential read in this version


if __name__ == "__main__":
    unittest.main()
