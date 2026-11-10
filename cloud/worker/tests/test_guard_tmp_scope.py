"""Ask #2354: the guard wrapper (cloud/worker/guard.sh, function in_tmp_scope) relaxes three refusals ONLY for a command that runs in a throwaway
repository under /tmp: a push to the default branch of a local remote under /tmp, `git config KEY VALUE` for a key outside the #CFGX table,
and GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM naming a plain file under /tmp. Outside the scope everything is judged as before. Offline; real git
in scratch repositories; nothing is pushed anywhere."""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
GUARD = os.path.join(WORKER, "guard.sh")
README = os.path.join(WORKER, "..", "README.md")
TAIL = "the repository's rules are the guard; this wrapper is only a convenience"
PUSH = "refused: pushing to the default branch: " + TAIL + "\n"
FORCE = "refused: force-pushing: " + TAIL + "\n"
GLOB = "refused: pushing by a glob refspec: " + TAIL + "\n"
CONFIG = "refused: changing git configuration: " + TAIL + "\n"
ENV = "refused: git configuration from the environment (GIT_CONFIG_*): " + TAIL + "\n"
ANCHOR = "a relaxation applied outside the temporary directory"
EXCLUDED = ["alias.*", "push.default", "core.hooksPath", "core.sshCommand", "core.fsmonitor", "core.pager", "core.editor", "core.askpass",
            "core.gitProxy", "credential.*", "include.*", "includeIf.*", "url.*", "http.proxy*", "http.sslcainfo", "http.cookiefile",
            "remote.*.proxy", "remote.*.receivepack", "remote.*.uploadpack", "uploadpack.packObjectsHook", "diff.external", "filter.*",
            "merge.*.driver", "gpg.*.program", "sendemail.*", "protocol.ext.allow", "gpg.program", "core.worktree", "core.alternateRefsCommand",
            "sequence.editor", "pager.*", "remote.*.vcs"]


def read(p):
    with open(p) as f:
        return f.read()


def git(cwd, *a):
    r = subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True, text=True, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout


def outside_dir():
    """a writable directory that is NOT under /tmp, or None"""
    for d in ("/var/tmp", "/dev/shm", os.path.expanduser("~")):
        d = os.path.realpath(d)
        if os.path.isdir(d) and os.access(d, os.W_OK) and not (d == "/tmp" or d.startswith("/tmp/")):
            return d
    return None


class Scope(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = os.path.realpath(tempfile.mkdtemp(dir="/tmp", prefix="scope2354-"))
        cls.addClassCleanup(shutil.rmtree, cls.root, True)
        cls.remote = os.path.join(cls.root, "remote.git")
        git(cls.root, "init", "-q", "--bare", cls.remote)
        cls.repo = cls.mk(cls.root, "repo", cls.remote)
        cls.out = outside_dir()
        cls.outroot = None
        cls.outrepo = None
        if cls.out:
            cls.outroot = tempfile.mkdtemp(dir=cls.out, prefix="scope2354-out-")
            cls.addClassCleanup(shutil.rmtree, cls.outroot, True)
            cls.outrepo = cls.mk(cls.outroot, "repo", cls.remote)
        cls.work = "/work/repo" if os.path.isdir("/work/repo/.git") else None

    @staticmethod
    def mk(parent, name, origin):
        d = os.path.join(parent, name)
        git(parent, "init", "-q", d)
        git(d, "remote", "add", "origin", origin)
        return d

    def guard(self, args, cwd=None, env=None, guard=None):
        e = {"PATH": "/usr/bin:/bin", "HOME": self.root}
        e.update(env or {})
        return subprocess.run(["sh", guard or GUARD, "git"] + args, cwd=cwd or self.repo, env=e, capture_output=True, text=True, timeout=60)

    def allowed(self, args, **kw):
        r = self.guard(args, **kw)
        self.assertEqual((r.returncode, r.stderr), (0, ""), (args, kw))

    def refused(self, args, line, **kw):
        r = self.guard(args, **kw)
        self.assertEqual((r.returncode, r.stderr), (3, line), (args, kw))

    def gfile(self, name="gfile", text="[user]\n\tname = x\n"):
        p = os.path.join(self.root, name)
        with open(p, "w") as f:
            f.write(text)
        return p

    # ---- S2, S3: the scope and the push ----
    def test_s2_s3_in_scope_a_push_to_main_of_a_local_remote_is_allowed(self):
        self.allowed(["push", "origin", "main"])
        self.allowed(["push", "origin", "HEAD:main"])
        self.allowed(["push", "origin", "HEAD:refs/heads/master"])
        self.allowed(["push", self.remote, "main"])
        self.allowed(["push", "file://" + self.remote, "main"])
        self.allowed(["push", "origin", "feature"])
        self.allowed(["-C", self.repo, "push", "origin", "main"], cwd=self.root)

    def test_s3_in_scope_the_other_remote_forms_are_refused(self):
        r = self.repo
        self.addCleanup(git, r, "remote", "set-url", "origin", self.remote)
        for url in ("https://github.com/o/r.git", "ssh://git@github.com/o/r.git", "git@github.com:o/r.git", "git://example.com/r.git", "/usr"):
            git(r, "remote", "set-url", "origin", url)
            self.refused(["push", "origin", "main"], PUSH)
            self.refused(["push", "origin", "HEAD:main"], PUSH)
        link = os.path.join(self.root, "to-origin")
        for target in ("/work/repo", "/usr"):
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(target, link)
            git(r, "remote", "set-url", "origin", link)
            self.refused(["push", "origin", "main"], PUSH)
            self.refused(["push", link, "main"], PUSH)
            self.refused(["push", "file://" + link, "main"], PUSH)
        git(r, "remote", "set-url", "origin", self.remote)
        self.allowed(["push", "origin", "main"])

    def test_s3_a_remote_named_like_a_url_and_a_rewrite_rule_do_not_open_the_push(self):
        os.makedirs(os.path.join(self.root, "x"), exist_ok=True)
        name = "file://" + self.root + "/x"
        git(self.repo, "config", "remote.%s.url" % name, "https://github.com/o/nonexist.git")
        self.addCleanup(git, self.repo, "config", "--remove-section", "remote." + name)
        self.refused(["push", name, "main"], PUSH)
        g = self.gfile("rewrite", '[url "https://github.com/o/nonexist.git"]\n\tpushInsteadOf = %s\n' % self.remote)
        self.refused(["push", "origin", "main"], PUSH, env={"GIT_CONFIG_GLOBAL": g})
        self.refused(["push", self.remote, "main"], PUSH, env={"GIT_CONFIG_GLOBAL": g})

    def test_s3_abbreviated_and_repo_options_do_not_move_the_remote(self):
        git(self.repo, "remote", "add", "web", "https://github.com/o/r.git")
        self.addCleanup(git, self.repo, "remote", "remove", "web")
        for opt in (["--ex", self.remote], ["--receive-p", self.remote], ["--rep", self.remote], ["--repo=" + self.remote], ["--repo", self.remote]):
            self.refused(["push"] + opt + ["web", "main"], PUSH)
        self.allowed(["push", "--dry-run", "origin", "main"])

    def test_s3_short_clusters_and_a_second_push_url_do_not_open_the_push(self):
        git(self.repo, "remote", "add", "r2", "/usr")
        git(self.repo, "remote", "add", "multi", self.remote)
        git(self.repo, "config", "--add", "remote.multi.pushurl", self.remote)
        git(self.repo, "config", "--add", "remote.multi.pushurl", "https://github.com/o/r.git")
        self.addCleanup(git, self.repo, "remote", "remove", "r2")
        self.addCleanup(git, self.repo, "remote", "remove", "multi")
        self.refused(["push", "-oxo", "r2", "ok", "main"], PUSH)
        self.refused(["push", "-uoo", "r2", "ok", "main"], PUSH)
        self.refused(["push", "multi", "main"], PUSH)
        self.allowed(["push", "-u", "origin", "main"])
        self.allowed(["push", "-o", "x", "origin", "main"])

    def test_s3_a_destination_whose_git_file_leads_out_and_a_home_rewrite_do_not_open_the_push(self):
        dest = os.path.join(self.root, "gitfile-dest")
        os.makedirs(dest)
        with open(os.path.join(dest, ".git"), "w") as f:
            f.write("gitdir: %s\n" % (self.outrepo + "/.git" if self.outrepo else "/usr"))
        self.refused(["push", dest, "main"], PUSH)
        home = os.path.join(self.root, "home")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, ".gitconfig"), "w") as f:
            f.write('[url "https://github.com/o/r"]\n\tpushInsteadOf = %s\n' % self.remote)
        self.refused(["push", self.remote, "main"], PUSH, env={"HOME": home})
        self.refused(["push", "-", self.remote, "main"], PUSH)

    def test_s3_in_scope_force_is_allowed_for_a_local_remote_and_a_glob_all_and_mirror_stay_refused(self):
        for f in (["--force"], ["--force-with-lease"], ["--force-with-lease=feat"], ["-f"], ["--force-if-includes"]):
            self.allowed(["push"] + f + ["origin", "feat"])
        self.allowed(["push", "origin", "+feat"])
        self.allowed(["push", "--force", self.remote, "feat"])
        self.allowed(["push", "--force", "file://" + self.remote, "feat"])
        self.refused(["push", "origin", "+refs/heads/*:refs/heads/*"], GLOB)
        self.refused(["push", "origin", "refs/heads/*:refs/heads/*"], GLOB)
        self.refused(["push", "--all", "origin"], PUSH)
        self.refused(["push", "--mirror", "origin"], FORCE)
        self.refused(["push", "--force", "--mirror", "origin"], FORCE)

    def test_s3_a_dash_c_setting_puts_the_push_out_of_scope(self):
        for pre in (["-c", "remote.origin.pushurl=https://github.com/x/y.git"], ["-c", "url.https://github.com/.insteadOf=" + self.root + "/"],
                    ["--config-env=remote.origin.pushurl=FOO"], ["-c", "a.b=c"]):
            self.refused(pre + ["push", "--force", "origin", "feat"], FORCE, env={"FOO": "https://github.com/x/y.git"})
            self.refused(pre + ["push", "origin", "main"], PUSH, env={"FOO": "https://github.com/x/y.git"})

    def test_s3_in_scope_force_to_another_remote_form_is_refused(self):
        r = self.repo
        self.addCleanup(git, r, "remote", "set-url", "origin", self.remote)
        for url in ("https://github.com/o/r.git", "ssh://git@github.com/o/r.git", "/usr"):
            git(r, "remote", "set-url", "origin", url)
            for f in (["--force", "origin", "feat"], ["--force-with-lease", "origin", "feat"], ["origin", "+feat"], ["-f", "origin", "feat"]):
                self.refused(["push"] + f, FORCE)

    # ---- ask #2360: a bare repository, by its git directory ----
    def bare(self, name="bare-scope.git"):
        p = os.path.join(self.root, name)
        if not os.path.isdir(p):
            git(self.root, "init", "-q", "--bare", p)
        return p

    def test_s2_a_bare_repository_under_tmp_is_in_scope_and_other_places_are_not(self):
        b = self.bare()
        self.allowed(["config", "receive.denyNonFastForwards", "true"], cwd=b)
        self.allowed(["config", "http.receivepack", "true"], cwd=b)
        self.allowed(["-C", b, "config", "receive.denyNonFastForwards", "true"], cwd=self.root)
        self.refused(["config", "core.hooksPath", "/x"], CONFIG, cwd=b)
        self.refused(["-C", b, "config", "alias.x", "!id"], CONFIG, cwd=self.root)
        link = os.path.join(self.root, "bare-link-work")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink("/work/repo", link)
        outs = [(["config", "http.receivepack", "true"], d) for d in (self.work and [self.work, link, "/work/repo/.git"] or [])]
        if self.out:
            ob = os.path.join(self.outroot, "bare.git")
            git(self.outroot, "init", "-q", "--bare", ob)
            outs.append((["config", "http.receivepack", "true"], ob))
            lk = os.path.join(self.root, "link-to-outbare")
            if not os.path.lexists(lk):
                os.symlink(ob, lk)
            outs.append((["config", "http.receivepack", "true"], lk))
        if self.work:
            inside = "/work/repo/.git"
            outs.append((["-C", os.path.join(self.root, "..", "..", "work", "repo", ".git"), "config", "http.receivepack", "true"], self.root))
        for args, cwd in outs:
            self.refused(args, CONFIG, cwd=cwd)

    def test_s3_a_bare_repository_remote_takes_a_forced_push(self):
        self.allowed(["push", "--force", "origin", "feat"])
        b = self.bare("bare-origin2.git")
        r = self.mk(self.root, "repo-b", b)
        self.allowed(["push", "origin", "+feat"], cwd=r)
        self.allowed(["push", "--force-with-lease", "origin", "feat"], cwd=r)

    def refused_outside(self, guard=None):
        """every command that must stay refused because it is outside the temporary scope; a wrong allowance names the anchored text"""
        cases = []   # (args, cwd, env, expected stderr or None)
        link = os.path.join(self.root, "link-to-work")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink("/work/repo", link)
        if self.outrepo:   # a repository outside /tmp, with a remote under /tmp
            cases += [(["push", "origin", "main"], self.outrepo, {}, PUSH), (["config", "http.receivepack", "true"], self.outrepo, {}, CONFIG),
                      (["config", "a.b", "c"], self.outrepo, {}, CONFIG),
                      (["status"], self.outrepo, {"GIT_CONFIG_GLOBAL": self.gfile()}, ENV),
                      (["-C", self.outrepo, "push", "origin", "main"], self.repo, {}, PUSH)]   # the -C directory decides, not the cwd
        for t in (self.out or "/usr", "/usr"):   # TMPDIR that resolves outside /tmp
            cases += [(["push", "origin", "main"], self.repo, {"TMPDIR": t}, PUSH), (["config", "http.receivepack", "true"], self.repo, {"TMPDIR": t}, CONFIG)]
        gd = os.path.join(self.repo, ".git")   # the repository chosen another way
        cases += [(["--git-dir", gd, "push", "origin", "main"], self.repo, {}, PUSH), (["push", "origin", "main"], self.repo, {"GIT_DIR": gd}, PUSH),
                  (["status"], self.repo, {"GIT_CONFIG_GLOBAL": self.gfile(), "GIT_DIR": gd}, ENV)]
        if self.work:   # the work checkout, by name, by a link, by dot-dot
            for cwd, args in ((self.work, []), (link, []), (self.root, ["-C", link]), (self.root, ["-C", "/tmp/../work/repo"]),
                              (self.root, ["-C", self.root + "/../../work/repo"])):
                cases += [(args + ["push", "origin", "main"], cwd, {}, PUSH), (args + ["config", "a.b", "c"], cwd, {}, CONFIG),
                          (args + ["config", "http.receivepack", "true"], cwd, {}, CONFIG), (args + ["status"], cwd, {"GIT_CONFIG_GLOBAL": self.gfile()}, ENV)]
        for args, cwd, env, want in cases:   # no subTest: the first wrong answer raises, so a mutant run can be asserted
            r = self.guard(args, cwd=cwd, env=env, guard=guard)
            if r.returncode == 0:
                self.fail("%s: %s in %s let through" % (ANCHOR, args, cwd))
            self.assertEqual((r.returncode, r.stderr), (3, want or r.stderr), (args, cwd, sorted(env)))

    def test_s2_outside_the_scope_everything_is_judged_as_before(self):
        self.refused_outside()

    def fake_guard(self, fake):
        """a copy of the guard whose work checkout is the scratch repository `fake` (a work checkout placed under /tmp)"""
        text = read(GUARD)
        self.assertIn("/work/repo", text)
        p = os.path.join(self.root, "guard-fake-%d.sh" % len(os.listdir(self.root)))
        with open(p, "w") as f:
            f.write(text.replace("/work/repo", fake))
        return p

    def test_s2_a_work_checkout_under_tmp_one_that_contains_it_and_a_shared_common_directory_are_out_of_scope(self):
        fake = self.mk(self.root, "fakework", self.remote)
        g = self.fake_guard(fake)
        self.allowed(["push", "origin", "main"], guard=g)   # a neighbour of the fake work checkout is still in scope
        self.refused(["push", "origin", "main"], PUSH, cwd=fake, guard=g)
        self.refused(["config", "http.receivepack", "true"], CONFIG, cwd=fake, guard=g)
        os.makedirs(os.path.join(fake, "sub"), exist_ok=True)   # a directory inside it
        self.refused(["push", "origin", "main"], PUSH, cwd=os.path.join(fake, "sub"), guard=g)
        outer = os.path.join(self.root, "outer")   # a repository that CONTAINS the fake work checkout
        git(self.root, "init", "-q", outer)
        git(outer, "remote", "add", "origin", self.remote)
        inner = self.mk(outer, "fakework2", self.remote)
        g2 = self.fake_guard(inner)
        self.refused(["push", "origin", "main"], PUSH, cwd=outer, guard=g2)
        self.refused(["config", "http.receivepack", "true"], CONFIG, cwd=outer, guard=g2)
        link = os.path.join(self.root, "to-fake")   # a remote that is a link to the fake work checkout
        os.symlink(fake, link)
        self.addCleanup(git, self.repo, "remote", "set-url", "origin", self.remote)
        git(self.repo, "remote", "set-url", "origin", link)
        self.refused(["push", "origin", "main"], PUSH, guard=g)
        git(self.repo, "remote", "set-url", "origin", self.remote)
        # a worktree whose common directory is the fake work checkout's: the shared configuration is the work checkout's
        git(fake, "-c", "user.name=a", "-c", "user.email=a@b", "commit", "-q", "--allow-empty", "-m", "x")
        wt = os.path.join(self.root, "wt")
        git(fake, "worktree", "add", "-q", "-b", "wtb", wt)
        self.refused(["config", "http.receivepack", "true"], CONFIG, cwd=wt, guard=g)
        self.refused(["push", "origin", "main"], PUSH, cwd=wt, guard=g)

    # ---- S4: git config ----
    def test_s4_in_scope_a_write_of_an_ordinary_key_is_allowed(self):
        self.allowed(["config", "http.receivepack", "true"])
        self.allowed(["config", "--local", "http.receivepack", "true"])
        self.allowed(["config", "receive.denyCurrentBranch", "ignore"])
        self.allowed(["config", "user.name", "x"])

    def test_s4_in_scope_each_excluded_key_and_each_option_form_is_refused(self):
        for pat in EXCLUDED:
            key = pat.replace("*", "x")
            for k in (key, key.upper()):
                with self.subTest(key=k):
                    self.refused(["config", k, "v"], CONFIG)
        for key in ("core.hooksPath", "core.sshCommand", "core.fsmonitor", "credential.helper", "url.x.insteadOf", "alias.x", "include.path",
                    "includeIf.gitdir:/a/.path", "remote.origin.receivepack", "filter.x.clean", "diff.external", "http.https://a.example/.proxy"):
            with self.subTest(key=key):
                self.refused(["config", key, "v"], CONFIG)
        for form in (["--file", "/tmp/f"], ["--file=/tmp/f"], ["-f", "/tmp/f"], ["--global"], ["--system"], ["--worktree"], ["--blob=x"], ["--add"], ["--unset"]):
            with self.subTest(form=form):
                self.refused(["config"] + form + ["http.receivepack", "true"], CONFIG)
        self.refused(["config", "http.receivepack", "--default=x"], CONFIG)
        self.refused(["config", "http.receivepack", "a", "b"], CONFIG)

    def test_s4_the_table_the_readme_and_the_code_name_the_same_excluded_keys(self):
        rows = [l[len("#CFGX excluded | "):].split(" | ")[0].strip() for l in read(GUARD).splitlines() if l.startswith("#CFGX excluded | ")]
        self.assertEqual(rows, EXCLUDED)
        readme = read(README).splitlines()
        para = [l for l in readme if l.startswith("- **`git config` writes are refused**")]
        env = [l for l in readme if l.startswith("- **Git's environment configuration is allowed only")]
        self.assertEqual((len(para), len(env)), (1, 1))
        for k in EXCLUDED:
            self.assertIn("`%s`" % k, para[0], k)
        self.assertIn("#CFGX", para[0])
        self.assertIn("in_tmp_scope", env[0])
        self.assertIn(ANCHOR, env[0])
        code = read(GUARD).lower()
        for pat in EXCLUDED:   # the code names each key (compared in lower case)
            self.assertIn(pat.lower(), code, pat)

    # ---- S5: the environment files ----
    def test_s5_in_scope_a_plain_file_under_tmp_is_let_through(self):
        g = self.gfile()
        for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
            self.allowed(["status"], env={name: g})
        self.allowed(["status"], env={"GIT_CONFIG_GLOBAL": g, "GIT_CONFIG_SYSTEM": g})
        self.allowed(["status"], env={"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
        os.makedirs(os.path.join(self.root, "d1"), exist_ok=True)
        self.allowed(["status"], env={"GIT_CONFIG_GLOBAL": self.gfile("d1/inner")})

    def test_s5_every_other_file_is_refused_with_the_one_line(self):
        good = self.gfile()
        link = os.path.join(self.root, "glink")
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink(good, link)
        exact = self.gfile("exact", "#" * 65536)
        os.makedirs(os.path.join(self.root, "adir"), exist_ok=True)
        bad = {"link": link, "directory": os.path.join(self.root, "adir"), "over 64 KiB": self.gfile("big", "#" * 65537),
               "dot-dot": self.root + "/adir/../gfile", "dot segment": self.root + "/./gfile", "relative": "gfile", "outside /tmp": "/etc/hostname",
               "missing": self.root + "/nothing", "double slash": self.root + "//gfile"}
        if self.out:
            p = os.path.join(self.outroot, "plain")
            with open(p, "w") as f:
                f.write("x")
            bad["plain file outside /tmp"] = p
        for name in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
            self.allowed(["status"], env={name: exact})   # exactly 64 KiB is allowed
            for why, p in bad.items():
                with self.subTest(name=name, why=why):
                    self.refused(["status"], ENV, env={name: p})
            if self.outrepo:
                self.refused(["status"], ENV, cwd=self.outrepo, env={name: good})   # out of scope: the same file
            self.refused(["status"], ENV, cwd="/", env={name: good})

    def real_file(self, path):   # a regular file that exists, owned by the user, removed at the end of the test
        d = os.path.dirname(path)
        made = not os.path.isdir(d)
        os.makedirs(d, exist_ok=True)
        with open(path, "w") as f:
            f.write("[user]\n\tname = x\n")
        self.addCleanup(lambda: os.path.lexists(path) and os.unlink(path))
        if made:
            self.addCleanup(shutil.rmtree, d, True)
        return path

    def test_s5_a_fixture_under_the_checkout_server_tests_is_let_through_in_scope_only(self):
        d = "/work/repo/server/tests"
        if not os.path.isdir(d):
            if not os.path.isdir("/work/repo"):
                self.skipTest("no checkout")
            os.makedirs(d)
            self.addCleanup(shutil.rmtree, "/work/repo/server", True)
        fx = os.path.join(d, "fixture-2360-%d.gitconfig" % os.getpid())
        with open(fx, "w") as f:
            f.write("[user]\n\tname = x\n")
        self.addCleanup(os.unlink, fx)
        for name in ("GIT_CONFIG_SYSTEM", "GIT_CONFIG_GLOBAL"):
            self.allowed(["status"], env={name: fx})
            self.refused(["status"], ENV, env={name: d + "/../" + os.path.basename(fx)})
            self.refused(["status"], ENV, env={name: d + "/../../" + os.path.basename(fx)})
            self.refused(["status"], ENV, env={name: "/work/repo/other"})
            self.refused(["status"], ENV, env={name: "/work/repo/server/testsx/f"})
            # review of 0beb9a46: the two paths above do not exist, so [ -f ] refuses them before the prefix check; these exist
            self.refused(["status"], ENV, env={name: self.real_file("/work/repo/server/other-2360")})
            self.refused(["status"], ENV, env={name: self.real_file("/work/repo/server/testsx/f-2360")})
            lk = os.path.join(self.root, "fxlink")
            if os.path.lexists(lk):
                os.unlink(lk)
            os.symlink(fx, lk)
            self.refused(["status"], ENV, env={name: lk})
            if self.outrepo:
                self.refused(["status"], ENV, cwd=self.outrepo, env={name: fx})   # out of scope
            self.refused(["status"], ENV, cwd=self.bare(), env={name: "/etc/hostname"})
            self.allowed(["status"], cwd=self.bare(), env={name: self.gfile()})
            if self.out:
                p = os.path.join(self.outroot, "fx")
                with open(p, "w") as f:
                    f.write("x")
                self.refused(["status"], ENV, env={name: p})
        self.refused(["status"], ENV, cwd="/", env={"GIT_CONFIG_SYSTEM": fx})

    # ---- S6: the unchanged refusals ----
    def test_s6_the_unscoped_settings_stay_refused_in_scope_and_out(self):
        def pairs(k, v):
            return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": k, "GIT_CONFIG_VALUE_0": v}
        cases = [pairs("http.extraHeader", "X-A: 1"), pairs("url.https://example.com/.insteadOf", "https://github.com/"), pairs("core.hooksPath", "/tmp/h")]
        for cwd in (self.repo, self.outrepo or "/"):
            for env in cases:
                with self.subTest(cwd=cwd, key=env["GIT_CONFIG_KEY_0"]):
                    self.refused(["status"], ENV, cwd=cwd, env=env)

    # ---- S7 (ask #2360 clause 7): the Authorization: Basic header, in scope only ----
    def test_s7_basic_header_and_local_insteadof_in_scope_only(self):
        def pairs(*kv):
            e = {"GIT_CONFIG_COUNT": str(len(kv))}
            for i, (k, v) in enumerate(kv):
                e["GIT_CONFIG_KEY_%d" % i], e["GIT_CONFIG_VALUE_%d" % i] = k, v
            return e
        ok = ("http.extraHeader", "Authorization: Basic dXNlcjpwYXNz")
        self.allowed(["status"], env=pairs(ok))
        self.allowed(["status"], env=pairs(ok, ("url.%s/.insteadOf" % self.root, "https://github.com/")))
        self.allowed(["status"], env=pairs(("url.file://%s/.insteadOf" % self.root, "https://github.com/")))
        self.allowed(["status"], cwd=self.bare(), env=pairs(ok))
        for v in ("Authorization: Basic abc\nX-A: 1", "Authorization: Bearer abc", "X-A: 1", "Authorization: Basic ", "authorization: basic abc", "Authorization: Basic a b"):
            self.refused(["status"], ENV, env=pairs(("http.extraHeader", v)))
        self.refused(["status"], ENV, env=pairs(("url.https://example.com/.insteadOf", "https://github.com/")))
        self.refused(["status"], ENV, env=pairs(("url.%s/.insteadOf" % self.root, "https://example.com/")))
        self.refused(["status"], ENV, cwd="/", env=pairs(ok))   # out of scope
        if self.outrepo:
            self.refused(["status"], ENV, cwd=self.outrepo, env=pairs(ok))
        if self.work:
            self.refused(["status"], ENV, cwd=self.work, env=pairs(ok))
        # the git config form, in scope
        self.allowed(["config", "http.extraHeader", "Authorization: Basic dXNlcjpwYXNz"])
        self.refused(["config", "http.extraHeader", "X-A: 1"], CONFIG)
        for v in ("Authorization: Basic YWJj\nX-Evil: 1", "X-Evil: 1\nAuthorization: Basic YWJj", "Authorization: Basic YWJj\r"):
            self.refused(["config", "http.extraHeader", v], CONFIG)
            self.refused(["config", "http.https://a.example/.extraHeader", v], CONFIG)
        self.refused(["config", "http.extraHeader", "Authorization: Bearer x"], CONFIG)
        self.refused(["config", "url.https://example.com/.insteadOf", "https://github.com/"], CONFIG)
        if self.outrepo:
            self.refused(["config", "http.extraHeader", "Authorization: Basic dXNlcjpwYXNz"], CONFIG, cwd=self.outrepo)

    def test_s7_a_guard_without_the_scope_check_lets_the_header_through_out_of_scope(self):
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": "Authorization: Basic abc"}
        self.refused(["status"], ENV, cwd="/", env=env)   # the control
        p = self.mutant('http.extraheader) in_tmp_scope || env_cfg_refuse; env_cfg_basic', 'http.extraheader) env_cfg_basic')
        r = self.guard(["status"], cwd="/", env=env, guard=p)
        if r.returncode != 0:
            self.fail("mutant did not let it through")
        self.assertEqual(r.returncode, 0, ANCHOR)

    # ---- S7: the mutant ----
    def mutant(self, old, new):
        text = read(GUARD)
        self.assertEqual(text.count(old), 1, old)   # asserted once
        out = text.replace(old, new, 1)
        self.assertNotEqual(out, text)   # applied: a no-op fails
        p = os.path.join(self.root, "mutant-%d.sh" % len(os.listdir(self.root)))
        with open(p, "w") as f:
            f.write(out)
        return p

    def test_s7_control_first_then_a_guard_without_the_scope_check_fails_with_the_anchored_text(self):
        if not self.outrepo:
            self.skipTest("no writable directory outside /tmp")
        self.refused_outside()   # the control: the unmutated guard refuses every case
        p = self.mutant('  [ "$sc_off" = 0 ] || return 1\n', "  return 0\n")
        with self.assertRaisesRegex(AssertionError, ANCHOR):
            self.refused_outside(guard=p)

    def test_s8_a_guard_without_the_git_directory_check_fails_with_the_anchored_text(self):
        if not self.outrepo:
            self.skipTest("no writable directory outside /tmp")
        ob = os.path.join(self.outroot, "mbare.git")
        git(self.outroot, "init", "-q", "--bare", ob)
        def run(g):
            r = self.guard(["config", "http.receivepack", "true"], cwd=ob, guard=g)
            if r.returncode == 0:
                self.fail("%s: a bare repository outside /tmp let through" % ANCHOR)
            self.assertEqual((r.returncode, r.stderr), (3, CONFIG))
        run(None)   # the control
        p = self.mutant('  [ "$sc_off" = 0 ] || return 1\n', "  return 0\n")   # no scope check at all
        with self.assertRaisesRegex(AssertionError, ANCHOR):
            run(p)

    def test_s8_a_guard_that_allows_force_everywhere_is_caught(self):
        git(self.repo, "remote", "set-url", "origin", "https://github.com/o/r.git")
        self.addCleanup(git, self.repo, "remote", "set-url", "origin", self.remote)
        self.refused(["push", "--force", "origin", "feat"], FORCE)   # the control
        p = self.mutant('--force|--force-with-lease|--force-with-lease=*|--force-if-includes) [ "$push_scoped" = 1 ] || refuse "force-pushing" ;;', '--force|--force-with-lease|--force-with-lease=*|--force-if-includes) ;;')
        self.assertEqual(self.guard(["push", "--force", "origin", "feat"], guard=p).returncode, 0)

    def test_s7_a_guard_that_allows_every_remote_form_in_scope_is_caught(self):
        git(self.repo, "remote", "set-url", "origin", "https://github.com/o/r.git")
        self.addCleanup(git, self.repo, "remote", "set-url", "origin", self.remote)
        self.refused(["push", "origin", "main"], PUSH)   # the control
        p = self.mutant('    case "$ps_one" in *%*) return 1 ;; file:///*) ps_one=${ps_one#file://} ;; file:*) return 1 ;; /*) ;; *) return 1 ;; esac\n', "    return 0\n")
        self.assertEqual(self.guard(["push", "origin", "main"], guard=p).returncode, 0)   # the mutant lets the https remote through


if __name__ == "__main__":
    unittest.main()
