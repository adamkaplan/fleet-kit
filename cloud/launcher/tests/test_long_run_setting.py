"""Ask #2369 (S5, S6): the setting CW_LONG_RUN_DENY (1 to 8 plain patterns) and its pass-through to the worker pod as the variable of the same name;
unset, the pod is byte for byte what it was. Offline."""
import datetime as dt
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import cloud_worker as cw  # noqa: E402
from test_cloud_stack import ENV, _args, build  # noqa: E402
from test_cloud_worker import T0  # noqa: E402

LIST = "*longcmd*,*other.sh*"


def env_of(pod):
    return {e["name"]: e["value"] for c in pod["spec"]["containers"] for e in c.get("env", [])}


class Setting(unittest.TestCase):
    def test_row_and_validator(self):
        self.assertIn(("CW_LONG_RUN_DENY", False, "name"), cw.SETTINGS)
        ok = cw.VALIDATORS["CW_LONG_RUN_DENY"][1]
        self.assertTrue(ok("*longcmd*"))
        self.assertTrue(ok(",".join("*p%d*" % i for i in range(8))))
        self.assertTrue(ok("a/b-c_d.e?*"))
        for bad in ("", ",".join("*p%d*" % i for i in range(9)), "*a b*", "*a;b*", "*" + "a" * 80, "*a*,", "*a*,,*b*"):
            self.assertFalse(ok(bad), repr(bad))
        self.assertTrue(ok("a" * 80))


class Render(unittest.TestCase):
    def pod(self, **extra):
        return cw.render_pod(_args(), "coder7", cw.pod_name("coder7"), T0, T0 + dt.timedelta(hours=2), dict(ENV, **extra), ())

    def test_on_carries_the_list(self):
        self.assertEqual(env_of(self.pod(CW_LONG_RUN_DENY=LIST))["CW_LONG_RUN_DENY"], LIST)

    def test_off_is_todays_pod(self):
        self.assertEqual(json.dumps(self.pod(), sort_keys=True), json.dumps(build(_args()), sort_keys=True))
        self.assertNotIn("CW_LONG_RUN_DENY", env_of(self.pod()))
        self.assertNotIn("CW_LONG_RUN_DENY", env_of(self.pod(CW_LONG_RUN_DENY="  ")))

    def test_on_differs_by_the_one_variable(self):
        on = env_of(self.pod(CW_LONG_RUN_DENY=LIST)); on.pop("CW_LONG_RUN_DENY")
        self.assertEqual(on, env_of(self.pod()))


if __name__ == "__main__":
    unittest.main()
