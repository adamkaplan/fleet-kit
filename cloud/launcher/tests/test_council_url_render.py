#!/usr/bin/env python3
"""Ask #2322, S7: the launcher puts COUNCIL_MODEL_URL (from CW_COUNCIL_URL) in a worker pod's environment, never a key; with the setting off the pod is today's."""
import argparse, copy, datetime as dt, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402

NOW = dt.datetime(2026, 11, 10, tzinfo=dt.timezone.utc)
ENV = {"CW_CLOUD_IMAGE": "registry.example.internal/worker:1", "CW_TOKEN_ENDPOINT": "https://tokens.example.internal/mint"}
URL = "http://council-gateway.cw.svc:8080"
ARGS = dict(repo="acme-org/widget-service", branch="main", idle="10m", hard="2h", max_hard="8h", shape="S", image=None, run_id=None, pull_secret=None)


def pod(env, **kw):
    return cw.render_pod(argparse.Namespace(**dict(ARGS, **kw)), "coder7", "cw-coder7", NOW, NOW + dt.timedelta(hours=2), env, ())


def envs(p):
    return {e["name"]: e["value"] for e in p["spec"]["containers"][0]["env"]}


class Render(unittest.TestCase):
    def test_s7_address_present_and_no_key(self):
        e = envs(pod(dict(ENV, CW_COUNCIL_URL=URL)))
        self.assertEqual(e["COUNCIL_MODEL_URL"], URL)
        for name in e:
            self.assertFalse(name.startswith("COUNCIL_MODEL_KEY") or name == "COUNCIL_MODEL_BEARER", name)
        self.assertEqual([n for n in e if n.startswith("COUNCIL_")], ["COUNCIL_MODEL_URL"])

    def test_s7_the_setting_is_read_with_its_whitespace_stripped(self):
        self.assertEqual(envs(pod(dict(ENV, CW_COUNCIL_URL="  " + URL + " ")))["COUNCIL_MODEL_URL"], URL)

    def test_s7_off_is_todays_pod_byte_for_byte(self):
        today = pod(ENV)
        for off in ({}, {"CW_COUNCIL_URL": ""}, {"CW_COUNCIL_URL": "   "}):
            self.assertEqual(pod(dict(ENV, **off)), today)
        self.assertNotIn("COUNCIL_MODEL_URL", envs(today))
        on = copy.deepcopy(pod(dict(ENV, CW_COUNCIL_URL=URL)))
        on["spec"]["containers"][0]["env"] = [e for e in on["spec"]["containers"][0]["env"] if e["name"] != "COUNCIL_MODEL_URL"]
        self.assertEqual(on, today)   # the one environment entry is the only difference

    def test_s7_the_setting_has_a_url_rule(self):
        ok = cw.VALIDATORS["CW_COUNCIL_URL"][1]
        self.assertTrue(ok(URL))
        self.assertTrue(ok("https://gw.example.internal/v1"))
        for bad in ("ftp://gw", "http://u@gw", "http://gw/?k=v", "gw:8080", "http://gw#f", "http://"):
            self.assertFalse(ok(bad), bad)


if __name__ == "__main__":
    unittest.main()
