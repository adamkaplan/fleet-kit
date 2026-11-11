"""(#2215) `gates-check` sends its READ-ONLY gh calls to the real gh binary, not the shim; a write never takes that route."""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def load():
    loader = importlib.machinery.SourceFileLoader("fsb_direct", str(REPO / "bin" / "fleet-switchboard"))
    spec = importlib.util.spec_from_loader("fsb_direct", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestDirectReads(unittest.TestCase):
    def setUp(self):
        self.sb = load()
        self.tmp = Path(tempfile.mkdtemp(prefix="direct-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(str(self.tmp), ignore_errors=True))
        (self.tmp / "shim").mkdir()
        (self.tmp / "real").mkdir()
        shim = self.tmp / "shim" / "gh"
        shim.write_bytes(b"#!/bin/sh\n" + self.sb.SHIM_HEAD + b"\n")
        real = self.tmp / "real" / "gh"
        real.write_text("#!/bin/sh\n")
        for f in (shim, real):
            f.chmod(f.stat().st_mode | stat.S_IXUSR)
        self.real = str(real)
        self.env = {"PATH": "%s:%s" % (self.tmp / "shim", self.tmp / "real")}
        self.seen = []

    def inner(self, argv, *a, **kw):
        self.seen.append((list(argv), kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    def runner(self, config=None, repo="acme/app"):
        return self.sb.direct_read_runner(self.inner, repo, dict({"gh": "gh"}, **(config or {})), self.env)

    def test_a_read_goes_to_the_real_gh(self):
        run = self.runner()
        for argv in (["gh", "pr", "view", "7", "--json", "x"], ["gh", "pr", "diff", "7"], ["gh", "api", "repos/a/b/pulls/7/files"], ["gh", "run", "list"]):
            run(argv)
        self.assertEqual([a[0] for a, _ in self.seen], [self.real] * 4)

    def test_a_write_never_takes_the_direct_route(self):
        run = self.runner()
        writes = (["gh", "pr", "merge", "7"], ["gh", "pr", "ready", "7"], ["gh", "api", "-X", "POST", "repos/a/b/issues"], ["gh", "api", "-f", "a=b", "x"],
                  ["gh", "api", "--method", "DELETE", "x"], ["gh", "pr", "comment", "7"])
        for argv in writes:
            run(argv)
        self.assertEqual([a[0] for a, _ in self.seen], ["gh"] * len(writes))

    def test_the_account_is_kept(self):
        config = {"gh_users": {"acme/app": "bot"}}
        run = self.sb.direct_read_runner(self.inner, "acme/app", dict({"gh": "gh"}, **config), self.env)
        self.sb.GhAccounts.token_for = lambda self_, account: "tok-" + account
        self.addCleanup(lambda: load())
        run(["gh", "pr", "view", "7"])
        argv, kw = self.seen[-1]
        self.assertEqual(argv[0], self.real)
        self.assertEqual(kw["env"]["GH_TOKEN"], "tok-bot")

    def test_no_real_gh_falls_back_to_the_old_path(self):
        self.env = {"PATH": str(self.tmp / "shim")}
        self.runner()(["gh", "pr", "view", "7"])
        self.assertEqual(self.seen[-1][0][0], "gh")


if __name__ == "__main__":
    unittest.main()
