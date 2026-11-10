"""Ask #2341 (S7): the setting CW_COUNCIL_URL puts COUNCIL_MODEL_URL in the worker pod's environment, never a key; unset, the pod is
byte for byte what it was. Offline, no cloud."""
import datetime as dt
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_stack import ENV, _args, build  # noqa: E402
from test_cloud_worker import T0  # noqa: E402

URL = "http://copilot-bridge.cw.svc:8080"


def env_of(pod):
    return {e["name"]: e["value"] for c in pod["spec"]["containers"] for e in c.get("env", [])}


class Render(unittest.TestCase):
    def pod(self, **extra):
        env = dict(ENV, **extra)
        return cw.render_pod(_args(), "coder7", cw.pod_name("coder7"), T0, T0 + dt.timedelta(hours=2), env, ())

    def test_on_carries_the_address_and_no_key(self):
        e = env_of(self.pod(CW_COUNCIL_URL=URL))
        self.assertEqual(e["COUNCIL_MODEL_URL"], URL)
        bad = [k for k in e if re.search(r"KEY|TOKEN|SECRET|PASSWORD|AUTH", k) and k not in ("CW_TOKEN_ENDPOINT",)]
        self.assertEqual(bad, [], "a key variable is in the pod's environment")

    def test_off_is_todays_pod(self):
        self.assertEqual(json.dumps(self.pod(), sort_keys=True), json.dumps(build(_args()), sort_keys=True))
        self.assertNotIn("COUNCIL_MODEL_URL", env_of(self.pod()))
        self.assertNotIn("COUNCIL_MODEL_URL", env_of(self.pod(CW_COUNCIL_URL="  ")))

    def test_on_differs_from_off_by_the_one_variable(self):
        on, off = self.pod(CW_COUNCIL_URL=URL), self.pod()
        e_on = env_of(on)
        e_on.pop("COUNCIL_MODEL_URL")
        self.assertEqual(e_on, env_of(off))
        for c in on["spec"]["containers"]:
            for e in c.get("env", []):
                self.assertEqual(set(e), {"name", "value"}, "no valueFrom: a key is never read from a Secret")

    def test_the_setting_has_one_row_and_a_url_rule(self):
        self.assertIn(("CW_COUNCIL_URL", False, "name"), cw.SETTINGS)
        ok = cw.VALIDATORS["CW_COUNCIL_URL"][1]
        self.assertTrue(ok(URL) and ok("https://council.example.internal/v1"))
        self.assertFalse(ok("ftp://x.example") or ok("http://u@x.example") or ok("http://x.example?a=1"))


if __name__ == "__main__":
    unittest.main()
