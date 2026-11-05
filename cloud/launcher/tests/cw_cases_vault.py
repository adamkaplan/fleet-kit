"""Test cases executed by bin/test-cloud-worker in its own namespace; do not run alone."""
class VaultBase(Base):
    """#876 slice 2: the vault access check, run by `launch --cloud` only after a vault read was refused for the firewall (offline: a fake `az` and a
    fake check module in a temp checkout)."""

    def setUp(self):
        super().setUp()
        self.lib = sb.load_module("cloud_worker").bind(sb.__dict__)._lib()
        self.cw = sb.load_module("cloud_worker")
        (self.stub_dir / "az").write_text(AZ_VAULT)
        (self.stub_dir / "az").chmod(0o755)
        self.checkout = self.home / "checkout"
        self.make_checkout()
        os.environ["FAKE_CHECKOUT"] = str(self.checkout)
        os.environ["CW_VAULT_CHECK_DIR"] = str(self.checkout)
        (self.stub_dir / "az_mode").write_text("firewall")
        recorder = self.home / "python-recorder"
        recorder.write_text('#!/bin/sh\nprintf "%%s\\n" "$@" > "%s/python-args"\nexec "%s" "$@"\n' % (self.stub_dir, sys.executable))
        recorder.chmod(0o755)
        hook = mock.patch.object(self.lib, "VAULT_CHECK_PYTHON", str(recorder))
        hook.start()
        self.addCleanup(hook.stop)

    def make_checkout(self, code=0, fixes=True, **extra):
        module = self.checkout / "scripts" / "v2" / "cw_vault_firewall.py"
        module.parent.mkdir(parents=True, exist_ok=True)
        module.write_text(FAKE_CHECK)
        (self.checkout / "fake.json").write_text(json.dumps(dict(extra, code=code, fixes=fixes)))
        for path in (self.checkout, self.checkout / "scripts", module.parent):
            path.chmod(0o755)
        module.chmod(0o644)

    def runs(self):
        path = self.checkout / "runs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def check_runs(self):
        return [r for r in self.runs() if r["kind"] == "start"]

    def az_reads(self):
        path = self.stub_dir / "az.log"
        return len(path.read_text().splitlines()) if path.exists() else 0

    def all_audit_lines(self):
        return [line for p in self.state.rglob("*") if p.is_file() for line in p.read_text(errors="replace").splitlines() if "cloud.vault_check" in line]

    def audit_lines(self):
        return self.all_audit_lines()[getattr(self, "seen", 0):]


class TestVaultCheckRelay(VaultBase):
    """S6, S8, S8c, U3: the five exit codes, one table."""

    def test_the_table_is_the_five_codes_of_the_contract(self):
        self.assertEqual(sorted(self.lib.VAULT_CHECK_CODES), [0, 10, 20, 30, 40])
        self.assertEqual([c for c, row in sorted(self.lib.VAULT_CHECK_CODES.items()) if row[1]], [0, 10])   # only these continue

    def test_s6_each_exit_code_is_relayed_audited_once_and_the_count_line_is_printed_for_10_only(self):
        codes = sorted(self.lib.VAULT_CHECK_CODES) + [7, 255]   # 7 and 255: a code the contract does not have is treated as 40 (a stop)
        for code in codes:
            with self.subTest(code=code):
                for stale in (self.checkout / "runs.jsonl", self.checkout / "fixed", self.stub_dir / "az.log", self.stub_dir / "calls.log"):
                    stale.unlink(missing_ok=True)
                self.make_checkout(code=code, fixes=code in (0, 10), address="addr-marker-%d" % code)
                for old in self.state.glob("cloud/*.json"):
                    old.unlink()
                self.audit_lines_clear()
                got, out, err = self.launch()
                continues = code in (0, 10)
                self.assertEqual(got, 0 if continues else 2, err)
                self.assertEqual(len(self.check_runs()), 1)
                lines = self.audit_lines()
                self.assertEqual(len(lines), 1, lines)
                self.assertIn('"exit": %d' % code, lines[0])
                self.assertEqual(err.count("rules 3 -> 4, 2026-10-19"), 1 if code == 10 else 0, err)
                self.assertEqual(out.count("rules 3 -> 4"), 0)
                if not continues:
                    self.assertIn("the owner of the vault access check", err)
                    self.assertIn("exit %d" % code, err)
                    self.assertEqual(self.stub_calls(), [], "something was created")
                for text in [out, err] + lines:
                    self.assertNotIn("addr-marker", text)
                self.audit_lines_clear()

    def audit_lines_clear(self):
        self.seen = len(self.all_audit_lines())

    def test_s8c_the_count_line_is_printed_once_and_audited_with_the_counts_and_no_address(self):
        self.make_checkout(code=10, fixes=True, address="addr-marker-xyz")
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertEqual(err.count("rules 3 -> 4, 2026-10-19"), 1, err)
        (line,) = self.audit_lines()
        self.assertIn('"rules_before": 3', line)
        self.assertIn('"rules_after": 4', line)
        self.assertIn('"exit": 10', line)
        for text in (out, err, line):
            self.assertNotIn("addr-marker", text)

    def test_s8_a_firewall_refusal_runs_the_check_once_and_reads_twice(self):
        self.make_checkout(code=10, fixes=True)
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertEqual((len(self.check_runs()), self.az_reads()), (1, 2))
        self.assertEqual(len(self.audit_lines()), 1)

    def test_s8_a_read_the_vault_already_answers_never_runs_the_check(self):
        (self.stub_dir / "az_mode").write_text("ok")
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertEqual((len(self.check_runs()), self.az_reads(), self.audit_lines()), (0, 1, []))

    def test_s8_the_retry_is_once_even_when_the_vault_still_refuses(self):
        self.make_checkout(code=0, fixes=False)   # the check says "answered", the vault disagrees
        code, out, err = self.launch()
        self.assertEqual(code, 2, err)
        self.assertEqual((len(self.check_runs()), self.az_reads()), (1, 2))
        self.assertIn("the owner of the vault access check", err)
        self.assertEqual(self.stub_calls(), [])

    def test_s8_any_other_vault_error_is_not_retried_and_runs_no_check(self):
        for mode in ("auth", "down"):
            with self.subTest(mode=mode):
                (self.stub_dir / "az.log").unlink(missing_ok=True)
                (self.stub_dir / "az_mode").write_text(mode)
                code, out, err = self.launch()
                self.assertEqual(code, 2, err)
                self.assertEqual((len(self.check_runs()), self.az_reads(), self.audit_lines()), (0, 1, []))

    def test_an_operator_admin_command_that_prints_the_firewall_text_is_not_retried_and_runs_no_check(self):
        self.make_checkout(code=10, fixes=True)
        os.environ["CW_CLUSTER_ADMIN_CMD"] = "echo ForbiddenByFirewall >&2; exit 1"
        code, out, err = self.launch()
        self.assertEqual(code, 2, err)
        self.assertEqual((len(self.check_runs()), self.az_reads(), self.audit_lines()), (0, 0, []))

    def test_a_mint_with_no_recovery_never_runs_the_check(self):
        self.make_checkout(code=10, fixes=True)
        self.assertIsNone(self.cw.Cluster().recovery)   # every command but launch builds its login this way
        with self.assertRaises(self.cw.Refusal):
            self.cw.cluster_token(env=dict(os.environ))
        self.assertEqual((len(self.check_runs()), self.az_reads()), (0, 1))

    def test_an_off_list_repository_never_runs_the_check(self):
        os.environ["CW_ALLOWED_REPOS"] = "acme-org/other"
        code, out, err = self.launch()
        self.assertEqual(code, 2, err)
        self.assertEqual((len(self.check_runs()), self.az_reads()), (0, 0))

    def test_u3_the_argument_list_is_exactly_the_module_and_check_and_the_cwd_is_the_checkout(self):
        self.make_checkout(code=10, fixes=True)
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        self.assertEqual((self.stub_dir / "python-args").read_text().split("\n")[:-1], ["-m", "scripts.v2.cw_vault_firewall", "check"])
        (start,) = self.check_runs()
        self.assertEqual(start["argv"], ["check"])
        self.assertEqual(os.path.realpath(start["cwd"]), os.path.realpath(str(self.checkout)))

    def test_u3_the_child_environment_is_scrubbed_and_each_poisoned_name_is_absent(self):
        poison = {"CW_VAULT_NAME": "x", "CW_VAULT_SUBSCRIPTION": "x", "CW_APP_KEY_CMD": "x", "PYTHONPATH": "/nowhere", "HTTPS_PROXY": "http://proxy.example.internal",
                  "GH_TOKEN": "x", "CW_OPENSSL": "/usr/bin/openssl", "LANG": "C", "CW_VAULT_CHECK_DIR": str(self.checkout), "AZURE_CLI_X": "x", "CW_ALLOWED_REPOS_X": "x"}
        self.make_checkout(code=10, fixes=True)
        with mock.patch.dict(os.environ, poison):
            code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        (start,) = self.check_runs()
        keys = set(start["env"])
        for name in poison:
            self.assertNotIn(name, keys, name)
        ours = {"PATH", "HOME", "TMPDIR", "AZURE_CONFIG_DIR"}
        # what a BARE python3 on the fixed PATH adds to such an environment by itself (the macOS /usr/bin/python3 shim adds SDKROOT and friends), plus the recording sh wrapper's own
        bare = subprocess.run([shutil.which("python3", path=self.lib.PREFLIGHT_PATH), "-c", "import os,json;print(json.dumps(sorted(os.environ)))"], capture_output=True, text=True,
                              env={"PATH": self.lib.PREFLIGHT_PATH, "HOME": str(self.home), "TMPDIR": str(self.home), "AZURE_CONFIG_DIR": str(self.home)}).stdout
        shim = set(json.loads(bare)) - ours | {"SHLVL", "PWD", "_", "__CF_USER_TEXT_ENCODING"}
        self.assertEqual({k for k in keys if not k.startswith("LC_")} - shim, ours)   # nothing of ours beyond the four, and nothing of the caller's
        import pwd
        self.assertEqual(start["home"], pwd.getpwuid(os.getuid()).pw_dir)   # the password database's home: the poisoned HOME variable is ignored
        self.assertNotEqual(start["home"], os.environ["HOME"])

    def test_the_lock_file_is_private_and_a_linked_lock_path_is_refused(self):
        self.make_checkout(code=10, fixes=True)
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)
        lock = self.state / "cloud" / self.cw.VAULT_LOCK_NAME
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        lock.unlink()
        os.symlink(str(self.home / "elsewhere"), str(lock))   # a linked lock path is never followed
        for old in self.state.glob("cloud/*.json"):
            old.unlink()
        (self.checkout / "fixed").unlink()
        code, out, err = self.launch()
        self.assertEqual(code, 2, err)
        self.assertIn("lock", err)
        self.assertFalse((self.home / "elsewhere").exists())


class TestVaultCheckPath(VaultBase):
    """S6b: a missing key or a bad checkout refuses naming the key, runs nothing, and never prints the path."""

    def cases(self):
        """ONE table of the bad checkouts: label -> (a function that spoils a fresh checkout, optionally the checkout to point the key at)."""
        mod = self.checkout / "scripts" / "v2" / "cw_vault_firewall.py"
        v2, scripts = mod.parent, self.checkout / "scripts"

        def add_file(where, name, mode):
            def spoil():
                (where / name).write_text("x = 1\n")
                (where / name).chmod(mode)
            return spoil

        def link(where, name):
            return lambda: os.symlink(str(self.checkout / "fake.json"), str(where / name))

        def parent(mode_or_link):
            def spoil():
                nest = self.home / "par"
                nest.mkdir()
                shutil.move(str(self.checkout), str(nest / "checkout"))
                if mode_or_link == "link":
                    os.rename(str(nest), str(self.home / "par-real"))
                    os.symlink(str(self.home / "par-real"), str(nest))
                else:
                    nest.chmod(mode_or_link)
                os.environ["CW_VAULT_CHECK_DIR"] = str(nest / "checkout")
            return spoil
        cases = {
            "no key": lambda: os.environ.pop("CW_VAULT_CHECK_DIR"),
            "missing directory": lambda: os.environ.update(CW_VAULT_CHECK_DIR=str(self.home / "nope")),
            "relative path": lambda: os.environ.update(CW_VAULT_CHECK_DIR="checkout"),
            "dot-dot path": lambda: os.environ.update(CW_VAULT_CHECK_DIR=str(self.checkout) + "/../checkout"),
            "a file, not a directory": lambda: os.environ.update(CW_VAULT_CHECK_DIR=str(self.checkout / "fake.json")),
            "checkout group-writable": lambda: self.checkout.chmod(0o775),
            "checkout world-writable": lambda: self.checkout.chmod(0o757),
            "checkout is a symlink": lambda: (os.symlink(str(self.checkout), str(self.home / "link")), os.environ.update(CW_VAULT_CHECK_DIR=str(self.home / "link"))),
            "module missing": lambda: os.unlink(str(mod)),
            "module a directory": lambda: (os.unlink(str(mod)), os.mkdir(str(mod))),
            "module symlink": lambda: (os.unlink(str(mod)), os.symlink(str(self.checkout / "fake.json"), str(mod))),
            "module group-writable": lambda: mod.chmod(0o664),
            "module world-writable": lambda: mod.chmod(0o646),
            "another file group-writable (a module it imports)": add_file(v2, "helper.py", 0o664),
            "another file world-writable": add_file(v2, "helper.py", 0o666),
            "an __init__.py world-writable": add_file(scripts, "__init__.py", 0o666),
            "an __init__.py at the root group-writable": add_file(self.checkout, "__init__.py", 0o664),
            "a directory group-writable": lambda: (v2 / "pkg").mkdir() or (v2 / "pkg").chmod(0o775),
            "a directory deep inside world-writable": lambda: ((v2 / "a" / "b").mkdir(parents=True), (v2 / "a" / "b").chmod(0o777)),
            "a symlink to a file": link(v2, "linked.py"),
            "a symlink in a deep directory": lambda: ((v2 / "a").mkdir(), link(v2 / "a", "x.py")()),
            "scripts is a symlink": lambda: (shutil.move(str(scripts), str(scripts) + ".real"), os.symlink(str(scripts) + ".real", str(scripts))),
            "scripts/v2 is a symlink": lambda: (shutil.move(str(v2), str(v2) + ".real"), os.symlink(str(v2) + ".real", str(v2))),
            "a parent group-writable": parent(0o775),
            "a parent world-writable": parent(0o777),
            "a symlink in the parents": parent("link"),
        }
        return cases

    def test_s6b_each_bad_path_refuses_naming_the_key_and_runs_nothing(self):
        for label, spoil in self.cases().items():
            with self.subTest(label):
                for leftover in ("link", "par", "par-real"):
                    path = self.home / leftover
                    if path.is_symlink():
                        path.unlink()
                    elif path.exists():
                        os.chmod(str(path), 0o755)
                        shutil.rmtree(str(path), ignore_errors=True)
                shutil.rmtree(str(self.checkout), ignore_errors=True)
                self.make_checkout(code=10, fixes=True)
                os.environ["CW_VAULT_CHECK_DIR"] = str(self.checkout)
                spoil()
                (self.stub_dir / "az.log").unlink(missing_ok=True)
                for old in self.state.glob("cloud/*.json"):
                    old.unlink()
                code, out, err = self.launch()
                self.assertEqual(code, 2, (label, err))
                self.assertIn("CW_VAULT_CHECK_DIR", err)
                self.assertEqual(len(self.check_runs()) + len(list(self.home.glob("par*/checkout/runs.jsonl"))), 0, label)
                self.assertEqual(self.audit_lines(), [])
                self.assertNotIn(str(self.home), err)   # never the path
                self.assertEqual(self.stub_calls(), [])

    def test_a_good_checkout_with_a_git_directory_and_pycache_is_accepted(self):
        (self.checkout / ".git").mkdir()
        os.symlink("/nonexistent", str(self.checkout / ".git" / "link"))   # the version-control directory is not loaded by python and is not walked
        (self.checkout / "scripts" / "v2" / "__pycache__").mkdir()
        self.assertIsNone(self.lib.vault_check_dir_problem(str(self.checkout)))

    def test_each_entry_branch_has_its_reason(self):
        import types
        user = os.getuid()
        mk = lambda mode, uid=user: types.SimpleNamespace(st_mode=mode, st_uid=uid)
        for found, reason in ((mk(stat.S_IFLNK | 0o777), "symlink"), (mk(stat.S_IFREG | 0o644, user + 1), "not owned"), (mk(stat.S_IFREG | 0o664), "writable"),
                              (mk(stat.S_IFREG | 0o646), "writable")):
            self.assertIn(reason, self.lib._entry_problem(found, "an entry"))
        self.assertIsNone(self.lib._entry_problem(mk(stat.S_IFREG | 0o644), "an entry"))

    def test_each_parent_branch_has_its_own_reason(self):
        for label, mode, reason in (("link", "link", "symlink"), ("group", 0o775, "writable"), ("world", 0o777, "writable")):
            nest = self.home / ("p-" + label)
            real = self.home / ("r-" + label)
            real.mkdir()
            (real / "c").mkdir()
            if mode == "link":
                os.symlink(str(real), str(nest))
            else:
                real.chmod(mode)
                nest = real
            self.assertIn(reason, self.lib.parents_problem(str(nest / "c" / "x")), label)

    def test_a_sticky_parent_is_accepted_and_one_owned_by_another_user_is_not(self):
        sticky = self.home / "sticky"
        sticky.mkdir()
        sticky.chmod(0o1777)
        (sticky / "x").mkdir()
        self.assertIsNone(self.lib.parents_problem(str(sticky / "x" / "y")))
        other = os.getuid() + 7
        with mock.patch.object(os, "getuid", lambda: other):
            self.assertIn("another user", self.lib.parents_problem(str(sticky / "x" / "y")))

    def test_the_lock_directorys_parents_are_judged_by_the_same_walk(self):
        self.make_checkout(code=10, fixes=True)
        (self.home / "state").mkdir(exist_ok=True)
        os.chmod(str(self.home / "state"), 0o777)
        code, out, err = self.launch()
        os.chmod(str(self.home / "state"), 0o755)
        self.assertEqual(code, 2, err)
        self.assertIn("vault check lock", err)
        self.assertEqual(len(self.check_runs()), 0)

    def test_an_unset_key_does_not_refuse_a_launch_the_vault_already_answers(self):
        (self.stub_dir / "az_mode").write_text("ok")
        os.environ.pop("CW_VAULT_CHECK_DIR")
        code, out, err = self.launch()
        self.assertEqual(code, 0, err)


class TestVaultCheckProcess(VaultBase):
    """The run limit kills the process group; two launches never run the check at once (S8b)."""

    def test_a_check_past_its_limit_is_killed_and_is_a_stop(self):
        self.make_checkout(code=10, fixes=True, sleep=30)
        with mock.patch.object(self.lib, "VAULT_CHECK_SECONDS", 1), mock.patch.object(self.lib, "VAULT_CHECK_GRACE", 0):
            code, out, err = self.launch()
        self.assertEqual(code, 2, err)
        self.assertIn("the owner of the vault access check", err)
        (start,) = self.check_runs()
        end = time.monotonic() + 10
        gone = False
        while not gone and time.monotonic() < end:   # a bounded wait for the killed group to be reaped
            try:
                os.kill(start["pid"], 0)
                time.sleep(0.05)
            except ProcessLookupError:
                gone = True
        self.assertTrue(gone, "the check is still running")
        (line,) = self.audit_lines()
        self.assertIn('"outcome": "timed-out"', line)

    def pid_gone(self, pid, limit=10):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False

    def test_a_grandchild_that_outlives_the_check_is_killed_and_does_not_hold_the_result(self):
        self.make_checkout(code=10, fixes=True, grandchild=True)
        started = time.monotonic()
        result = self.lib.run_vault_check(dict(os.environ))
        self.assertEqual((result.code, result.outcome), (10, "added"))
        self.assertLess(time.monotonic() - started, 30, "the result waited for the grandchild's pipe")
        self.assertTrue(self.pid_gone(int((self.checkout / "grandchild.pid").read_text())), "the grandchild survived")

    def test_a_grandchild_is_killed_with_a_check_that_timed_out(self):
        self.make_checkout(code=10, fixes=True, grandchild=True, sleep=30)
        with mock.patch.object(self.lib, "VAULT_CHECK_SECONDS", 1), mock.patch.object(self.lib, "VAULT_CHECK_GRACE", 0):
            result = self.lib.run_vault_check(dict(os.environ))
        self.assertEqual((result.code, result.outcome), (None, "timed-out"))
        self.assertTrue(self.pid_gone(int((self.checkout / "grandchild.pid").read_text())), "the grandchild survived")

    def test_a_holder_killed_with_no_chance_to_react_frees_the_lock(self):
        lock = str(self.home / "holder.lock")
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl,os,sys,time\nfd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT,0o600)\nfcntl.flock(fd,fcntl.LOCK_EX)\nprint('held',flush=True)\ntime.sleep(60)", lock],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        recovery = self.cw.VaultRecovery(lock, None, sleep=lambda s: time.sleep(0.02))
        with mock.patch.object(self.cw.cloud_cluster, "VAULT_LOCK_GRACE", 0), mock.patch.object(self.lib, "VAULT_CHECK_SECONDS", 0), mock.patch.object(self.lib, "VAULT_CHECK_GRACE", 0):
            with self.assertRaises(self.cw.Refusal):   # held: refused after its bounded wait
                recovery(lambda: None, dict(os.environ))
        holder.kill()   # SIGKILL: the kernel frees the lock with the process
        holder.wait()
        fd, waited = recovery._lock()
        os.close(fd)
        self.assertFalse(waited)

    def recover(self, results, index):
        env = dict(os.environ)
        recovery = self.cw.VaultRecovery(self.state / "cloud" / "vault-check.lock", None)
        recovery.lock_path = str(self.home / "vault.lock")
        read = lambda: self.cw._capture(["az", "keyvault", "secret", "show"], env=env)
        results[index] = recovery(read, env)

    def wait_started(self, limit=30):
        end = time.monotonic() + limit
        while not self.check_runs() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(self.check_runs(), "the first check never started")

    def two_launches(self):
        results = [None, None]
        threads = [threading.Thread(target=self.recover, args=(results, i), daemon=True) for i in (0, 1)]
        threads[0].start()
        self.wait_started()   # the first holds the lock (its check has started and sleeps) when the second arrives
        threads[1].start()
        for thread in threads:
            thread.join(60)
        return results

    def intervals(self):
        starts = [r["t"] for r in self.runs() if r["kind"] == "start"]
        ends = [r["t"] for r in self.runs() if r["kind"] == "end"]
        return sorted(zip(starts, ends))

    def test_s8b_two_launches_run_the_check_once_and_the_second_reads_again_first(self):
        self.make_checkout(code=10, fixes=True, sleep=1.5)
        results = self.two_launches()
        self.assertEqual(len(self.check_runs()), 1)
        self.assertEqual([r[0] for r in results], [0, 0])
        self.assertEqual(self.az_reads(), 2 + 0)   # the second's re-read answered: no read after a check of its own

    def test_s8b_when_the_re_read_is_still_refused_the_second_runs_its_own_check_with_no_overlap(self):
        self.make_checkout(code=0, fixes=False, sleep=1.0)
        self.two_launches()
        spans = self.intervals()
        self.assertEqual(len(spans), 2)
        self.assertLessEqual(spans[0][1], spans[1][0], "the two checks overlapped")

    def test_a_lock_that_is_not_freed_in_time_refuses_naming_the_lock(self):
        self.make_checkout(code=0, fixes=False, sleep=3)
        env = dict(os.environ)
        results = [None]
        holder = threading.Thread(target=self.recover, args=(results, 0), daemon=True)
        holder.start()
        self.wait_started()
        recovery = self.cw.VaultRecovery(self.home / "vault.lock", None, sleep=lambda s: time.sleep(0.05), clock=time.monotonic)
        with mock.patch.object(self.lib, "VAULT_CHECK_SECONDS", 0), mock.patch.object(self.lib, "VAULT_CHECK_GRACE", 0), mock.patch.object(self.cw.cloud_cluster, "VAULT_LOCK_GRACE", 0.3):
            with self.assertRaises(self.cw.Refusal) as caught:
                recovery(lambda: self.cw._capture(["az", "x"], env=env), env)
        self.assertIn("vault-check.lock", str(caught.exception))
        holder.join(30)

