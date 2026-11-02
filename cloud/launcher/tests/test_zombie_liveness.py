"""S7 of #1709: a REAL zombie is not alive for stub_support.group_alive and test_stub_cleanup.pid_alive; a live process is; the old bodies say alive.

Linux reads /proc/<pid>/stat; where it cannot be read (macOS) the test SKIPS loudly. The zombie is a child of this test that exited and is not
reaped until the end. Nothing is written under the repository; every child is killed and reaped at the end."""
import inspect
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import test_stub_cleanup  # noqa: E402
import stub_support  # noqa: E402

NEW_GROUP = "    return _group_has_live_member(pgid)"
NEW_PID = "    return not _is_zombie(pid)"


def state_of(pid):
    try:
        with open("/proc/%d/stat" % pid) as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return None


def old_function(func, new_line, old_line):
    src = inspect.getsource(func)
    assert src.count(new_line) == 1, "mutation not applicable: new line occurs %d times" % src.count(new_line)
    mutated = src.replace(new_line, old_line)
    assert mutated != src
    ns = dict(vars(inspect.getmodule(func)))
    exec(mutated, ns)
    return ns[func.__name__]


class S7Zombie(unittest.TestCase):
    def test_zombie_and_live(self):
        if state_of(os.getpid()) is None:
            line = "SKIPPED: /proc not readable: a zombie cannot be observed here"
            print(line)
            self.skipTest(line)
        if True:
            z = stub_support.start(self, [sys.executable, "-c", "pass"])
            end = time.time() + 20
            while state_of(z.pid) != "Z":
                if time.time() > end:
                    raise AssertionError("the child %d never became a zombie (state %r)" % (z.pid, state_of(z.pid)))
                time.sleep(0.02)
            live = stub_support.start(self, [sys.executable, "-c", "import time; time.sleep(60)"])
            while state_of(live.pid) is None:
                time.sleep(0.02)
            zg, zp = stub_support.group_alive(z.pid), test_stub_cleanup.pid_alive(z.pid)
            lg, lp = stub_support.group_alive(live.pid), test_stub_cleanup.pid_alive(live.pid)
            print("S7: zombie group_alive %s, zombie pid_alive %s (False required), live group_alive %s, live pid_alive %s (True required)"
                  % (zg, zp, lg, lp))
            self.assertIs(zg, False, "group_alive counts a zombie group as alive")
            self.assertIs(zp, False, "pid_alive counts a zombie as alive")
            self.assertIs(lg, True, "control: group_alive of a live group")
            self.assertIs(lp, True, "control: pid_alive of a live process")
            og = old_function(stub_support.group_alive, NEW_GROUP, "    return True")
            op = old_function(test_stub_cleanup.pid_alive, NEW_PID, "    return True")
            applied = [og is not stub_support.group_alive, op is not test_stub_cleanup.pid_alive]
            mg = 1 if og(z.pgid if hasattr(z, "pgid") else z.pid) is True else 0
            mp = 1 if op(z.pid) is True else 0
            print("S7 mutants: old group_alive True %d of 1, old pid_alive True %d of 1, mutations applied %s" % (mg, mp, all(applied)))
            self.assertTrue(all(applied))
            self.assertEqual((mg, mp), (1, 1), "the old bodies must count the zombie as alive")
            n_z = sum(1 for v in (zg, zp) if v is False)
            n_l = sum(1 for v in (lg, lp) if v is True)
            n_o = sum(1 for v in (og(z.pid), op(z.pid)) if v is True)
            print("S7: zombie False %d of 2 (group_alive, pid_alive), live True %d of 2, old bodies True for the zombie %d of 2" % (n_z, n_l, n_o))
            self.assertEqual((n_z, n_l, n_o), (2, 2, 2))


class S16SkipText(unittest.TestCase):
    """S16 of #1709: the skip text is exercised on a fake of a machine without /proc (/proc cannot be read: state_of answers None)."""
    def run_zombie_test(self):
        started = []
        real_start = stub_support.start

        def counting(case, *a, **kw):
            started.append(a)
            return real_start(case, *a, **kw)
        stub_support.start = counting
        self.addCleanup(setattr, stub_support, "start", real_start)
        result = unittest.TestResult()
        S7Zombie("test_zombie_and_live").run(result)
        stub_support.start = real_start
        return result, len(started)

    def test_skip_text_on_a_fake_without_proc(self):
        if state_of(os.getpid()) is None:
            self.skipTest("the control needs a readable /proc")
        c_result, c_started = self.run_zombie_test()
        control_ran = 1 if (c_started >= 1 and not c_result.skipped and c_result.wasSuccessful()) else 0
        self.assertEqual(control_ran, 1, "control: the zombie test must run, not skip, and start its child")
        real_state = globals()["state_of"]

        def cannot_read(pid):
            return None   # as state_of answers when /proc cannot be read
        globals()["state_of"] = cannot_read
        self.addCleanup(globals().__setitem__, "state_of", real_state)
        f_result, f_started = self.run_zombie_test()
        globals()["state_of"] = real_state
        texts = [reason for _, reason in f_result.skipped]
        with_text = 1 if len(texts) == 1 and texts[0].startswith("SKIPPED: /proc not readable") else 0
        print("S16: skip text exercised: control ran %d of 1, fake skipped %d of 1 with the text, children started %d"
              % (control_ran, with_text, f_started))
        self.assertEqual(with_text, 1, "the fake must skip with the text: %r" % (texts,))
        self.assertEqual(f_started, 0, "no child may start on the fake")


if __name__ == "__main__":
    unittest.main()
