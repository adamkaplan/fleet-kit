"""Ask #2447: the pod's installed opencode.json lists the clamp plugin once and sets no timeout above 300000 ms; the Dockerfile copies it; the plugin's own tests pass."""
import json, os, re, shutil, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.join(HERE, "..")
sys.path.insert(0, HERE)
from test_tool_cap_env import cap  # one definition of CAP_MS reader

CAP = cap()


def timeouts(o):
    if isinstance(o, dict):
        for k, v in o.items():
            if "timeout" in k.lower():
                yield v
            yield from timeouts(v)
    elif isinstance(o, list):
        for v in o:
            yield from timeouts(v)


class ToolCap(unittest.TestCase):
    def cfg(self):
        with open(os.path.join(W, "opencode-worker.json")) as f:
            return json.load(f)

    def test_s3_plugin_listed_once(self):
        # the permission file stays the catch-all allow alone (content pin); the entrypoint registers the plugin in the running config
        self.assertNotIn("plugin", self.cfg())
        with open(os.path.join(W, "entrypoint.sh")) as f:
            ep = f.read()
        self.assertNotIn('"plugin"', ep)
        self.assertIn("plugins/cw-tool-cap.js", ep)

    def test_s3_no_timeout_above_cap(self):
        vals = list(timeouts(self.cfg()))
        self.assertEqual([v for v in vals if not isinstance(v, (int, float)) or v > CAP], [])

    def test_dockerfile_copies_plugin(self):
        with open(os.path.join(W, "Dockerfile")) as f:
            df = f.read()
        self.assertEqual(len(re.findall(r"^COPY worker/plugins/cw-tool-cap/index\.js /usr/local/share/cw/plugins/cw-tool-cap/index\.js$", df, re.M)), 1)

    def test_s2_s4_plugin_tests(self):
        self.assertTrue(shutil.which("node"), "node is required for the plugin tests")
        r = subprocess.run(["node", "--test", os.path.join(W, "plugins/cw-tool-cap/index.test.mjs")], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout[-800:])


if __name__ == "__main__":
    unittest.main()
