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
        self.assertEqual(ep.count('"$cfgdir/opencode/plugins/cw-tool-cap.js"'), 2)
        self.assertIn("plugins/cw-tool-cap/index.js", ep)

    def test_real_opencode_loads_plugin(self):
        oc = shutil.which("opencode")
        if not oc:
            self.skipTest("opencode not on PATH")
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            pdir = os.path.join(d, "opencode", "plugins"); os.makedirs(pdir)
            shutil.copy(os.path.join(W, "plugins/cw-tool-cap/index.js"), os.path.join(pdir, "cw-tool-cap.js"))
            with open(os.path.join(d, "opencode", "opencode.json"), "w") as f:
                f.write("{}")
            env = dict(os.environ, XDG_CONFIG_HOME=d, XDG_DATA_HOME=os.path.join(d, "data"), XDG_STATE_HOME=os.path.join(d, "st"), XDG_CACHE_HOME=os.path.join(d, "ca"), OPENCODE_SERVER_PASSWORD="tp")
            log = open(os.path.join(d, "log"), "w")
            port = "14098"
            p = subprocess.Popen([oc, "serve", "--hostname", "127.0.0.1", "--port", port], env=env, stdout=log, stderr=log)
            try:
                import urllib.request, time, json as _j
                ok = False
                for _ in range(20):
                    time.sleep(1)
                    try:
                        import base64
                        rq = urllib.request.Request("http://127.0.0.1:%s/api/plugin" % port, headers={"Authorization": "Basic " + base64.b64encode(b"opencode:tp").decode()})
                        body = urllib.request.urlopen(rq, timeout=5).read().decode()
                    except Exception:
                        continue
                    if any(x.get("id") == "cw.tool-cap" and x["state"]["status"] == "active" for x in _j.loads(body)["data"]):
                        ok = True
                        break
                self.assertTrue(ok, "cw.tool-cap not active")
            finally:
                p.terminate(); p.wait(timeout=20); log.close()
            with open(os.path.join(d, "log")) as f:
                self.assertNotIn("must be a directory", f.read())

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
