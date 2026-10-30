"""#1098: the whole-read check of `review record` on a pull request of more than 100 files (moved out of bin/test-switchboard, size ceiling).

The fixtures are those of bin/test-switchboard (HOST, set by its loader block)."""
import json
import re
import subprocess
from unittest import mock


HOST = None   # set by bin/test-switchboard


def load():
    host = HOST
    LedgerBase, TestWholeRead, FLOOR_CONFIG = host.LedgerBase, host.TestWholeRead, host.FLOOR_CONFIG
    FAKE_GH, LEDGER_HEAD, sb = host.FAKE_GH, host.LEDGER_HEAD, host.sb

    class TestWholeReadOfALargePullRequest(LedgerBase):
        """#1098: the whole-read check of `review record` judges EVERY changed file of a pull request of more than 100 files, read through the shared paged reader (`delegated_clear.read_pr_files`)."""

        extra_config = dict(FLOOR_CONFIG, plan_gates={"repos": {"acme/app": {"mode": "shadow"}}})
        records = TestWholeRead.records
        block = TestWholeRead.block
        record = TestWholeRead.record

        def world_of(self, count, platform_total=None):
            self.names = ["src/f%03d.py" % i for i in range(count)]
            self.requests = []
            self.platform_total = count if platform_total is None else platform_total
            self.pr = dict(self.pr, baseRefOid="c" * 40, changedFiles=self.platform_total,
                           files=[{"path": n, "additions": 1, "deletions": 0} for n in self.names[:100]])

        def route(self, argv, **kwargs):
            if argv[0] == FAKE_GH and argv[1] == "api" and re.search(r"/pulls/9(\?|$|/files)", argv[2]):
                self.requests.append(argv[2])
                if argv[2].endswith("/pulls/9"):
                    return subprocess.CompletedProcess(argv, 0, json.dumps({"changed_files": self.platform_total, "head": {"sha": LEDGER_HEAD}}), "")
                page = int(re.search(r"&page=(\d+)", argv[2]).group(1))
                chunk = self.names[(page - 1) * 100:page * 100]
                return subprocess.CompletedProcess(argv, 0, json.dumps([{"filename": n, "additions": 1, "deletions": 0, "status": "modified"} for n in chunk]), "")
            return super().route(argv, **kwargs)

        def rows(self, skip=None):
            return tuple((n, 1, 1) for n in self.names if n != skip)

        def pages_asked(self):
            return [r for r in self.requests if "/files" in r]

        def check_large(self):
            """Every claim, as assertions over the real command (each mutant of the reader fails at least one)."""
            self.world_of(139)
            code, out, err = self.record(self.rows())
            self.assertEqual(code, 0, err)
            self.assertEqual((self.records()[-1]["files_read"], self.records()[-1]["files_total"]), (139, 139))
            code, out, err = self.record(self.rows(skip="src/f138.py"))   # the unread path is among the last 39
            self.assertEqual(code, 2)
            self.assertIn("src/f138.py", err)
            self.world_of(100)
            self.assertEqual(self.record(self.rows())[0], 0)
            self.assertEqual(len(self.pages_asked()), 0)   # `gh pr view` listed all 100: no paged read at all
            self.world_of(101)
            self.assertEqual(self.record(self.rows())[0], 0)
            self.assertEqual(len(self.pages_asked()), 2)   # 101 files: two pages
            self.world_of(250)
            self.assertEqual(self.record(self.rows())[0], 0)
            self.assertEqual(len(self.pages_asked()), 3)   # past the second page: the last 50 are read too
            self.world_of(3001)
            code, out, err = self.record(self.rows()[:3])   # the block itself is not the point
            self.assertEqual(code, 2)
            self.assertIn("3000", err)
            self.assertEqual(self.pages_asked(), [])   # refused before any page is read

        def test_s3_s7_a_pull_request_of_139_files_is_judged_on_all_of_them(self):
            self.check_large()

        def test_each_mutant_of_the_reader_fails_the_check(self):
            real = sb.load_module("delegated_clear")
            real_pages = real.FILE_PAGES
            from_pages = lambda n: mock.patch.object(real, "FILE_PAGES", n)
            for name, mutant in (("reads only the first page", lambda: from_pages(1)), ("stops after two pages", lambda: from_pages(2))):
                with self.subTest(name), mutant():
                    with self.assertRaises(AssertionError):
                        self.check_large()
            with self.subTest("ignores the cap"), mock.patch.object(real, "FILE_LIMIT", 10 ** 6):
                with self.assertRaises(AssertionError):
                    self.check_large()
            self.assertEqual(real.FILE_PAGES, real_pages)

    return TestWholeReadOfALargePullRequest
