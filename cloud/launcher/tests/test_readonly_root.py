"""Ask #2205 (design R step ii): a lane in CW_READONLY_ROOT_LANES gets a read-only root and writable mounts only at the measured paths."""
import argparse, datetime as dt, json, os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(LAUNCHER))
sys.path.insert(0, LAUNCHER)
import cloud_worker as cw  # noqa: E402

SOURCE = os.path.join(LAUNCHER, "cloud_worker.py")
NOW = dt.datetime(2026, 11, 5, tzinfo=dt.timezone.utc)
MOUNTS = ["/tmp", "/home/worker/.cache/opencode", "/home/worker/.config/opencode", "/home/worker/.local/state/opencode",
          "/home/worker/.local/share", "/home/worker/.npm", "/home/worker/.cache/pip", "/var/lib/docker", "/var/log"]
BASE_ENV = {"CW_TOKEN_ENDPOINT": "http://t"}


def render(mod, boss, setting=None):
    a = argparse.Namespace(image="img", repo="acme-org/widget-service", branch="main", shape="M", idle="30m",
                           max_hard=None, run_id=None, pull_secret=None, stack=False, boss=boss)
    env = dict(BASE_ENV, **({"CW_READONLY_ROOT_LANES": setting} if setting is not None else {}))
    return mod.render_pod(a, "w1", "w1-abcd", NOW, NOW + dt.timedelta(hours=1), env)


def check_rendered(test, mod):
    pod = render(mod, "laneA", "laneA,laneB")
    c = pod["spec"]["containers"][0]
    test.assertTrue(c["securityContext"].get("readOnlyRootFilesystem") is True, "readOnlyRootFilesystem is not true for a listed lane")
    extra = [m["mountPath"] for m in c["volumeMounts"] if m["mountPath"] not in ("/run/cw", "/run/cw-tmp", "/work")]
    for m in MOUNTS:   # a required path is present (the set may grow)
        test.assertIn(m, extra, "no writable mount at " + m)
    # nested docker: the daemon dirs are writable emptyDirs, so a read-only pod keeps docker working
    vols = {v["name"]: v for v in pod["spec"]["volumes"]}
    for m in ("/var/lib/docker", "/var/log"):
        mount = [x for x in c["volumeMounts"] if x["mountPath"] == m][0]
        test.assertIn("emptyDir", vols[mount["name"]])
        test.assertFalse(mount.get("readOnly"))
    test.assertIn({"name": "GIT_CONFIG_GLOBAL", "value": "/run/cw-tmp/gitconfig"}, c["env"])
    names = {v["name"] for v in pod["spec"]["volumes"]}
    test.assertTrue(all(m["name"] in names for m in c["volumeMounts"]))
    test.assertFalse(any(m["mountPath"] == "/home/worker" or m["mountPath"].endswith(".gitconfig") for m in c["volumeMounts"]))


def load_mutant(old, new):
    src = open(SOURCE).read()
    assert src.count(old) == 1, "mutation text not found exactly once"
    mod = type(sys)("cw_mutant")
    mod.__file__ = SOURCE
    exec(compile(src.replace(old, new), SOURCE, "exec"), mod.__dict__)
    return mod


class ReadonlyRoot(unittest.TestCase):
    def test_listed_lane(self):
        check_rendered(self, cw)

    def test_unlisted_or_unset_is_unchanged(self):
        base = json.dumps(render(cw, None), sort_keys=True)
        self.assertEqual(json.dumps(render(cw, "laneA"), sort_keys=True), base)
        self.assertEqual(json.dumps(render(cw, "laneA", ""), sort_keys=True), base)
        self.assertEqual(json.dumps(render(cw, "laneC", "laneA laneB"), sort_keys=True), base)
        self.assertNotIn("readOnlyRootFilesystem", base)

    def test_unset_equals_tip(self):
        tip = subprocess.run(["git", "show", "origin/integration:cloud/launcher/cloud_worker.py"], cwd=ROOT, capture_output=True, text=True)
        if tip.returncode:
            self.skipTest("no origin/integration")
        mod = type(sys)("cw_tip")
        mod.__file__ = SOURCE
        exec(compile(tip.stdout, SOURCE, "exec"), mod.__dict__)
        self.assertEqual(json.dumps(render(mod, "laneA"), sort_keys=True), json.dumps(render(cw, "laneA"), sort_keys=True))

    def test_mutant_forgetting_readonly_fails(self):
        check_rendered(self, cw)   # control first
        mut = load_mutant('["readOnlyRootFilesystem"] = True', '["readOnlyRootFilesystem"] = False')
        with self.assertRaises(AssertionError) as r:
            check_rendered(self, mut)
        self.assertIn("readOnlyRootFilesystem is not true for a listed lane", str(r.exception))


if __name__ == "__main__":
    unittest.main()
