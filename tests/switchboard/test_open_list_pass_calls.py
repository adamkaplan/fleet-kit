"""#1886 Eighth amendment (3): the calls of one pass count the open-list pages. Real: the pass, the sweep read, read_pages. Fake: the gh runner (1 s a call, a slow workflow read) and the GitHub world."""
from unittest import mock
from support.loader import sb


def build(Slow):
    class TestOpenListPassCalls(Slow):
        """One pass of the rig of the slow catch-up, 1,200 unowned items, counted by the calls the fake gh sees."""

        def counted_pass(self):
            rig = self.rig()
            seen = {"open": 0, "all": 0, "runs": 0}
            inner = rig.hub.runner

            def count(argv, **kwargs):
                path = argv[-1]
                seen["all"] += 1
                seen["open"] += 1 if "issues?state=open" in path else 0
                seen["runs"] += 1 if "actions/runs" in path else 0
                return inner(argv, **kwargs)
            rig.hub.runner = count
            rig.hub.catch_up(rig.watch(), rig.world.clock.now())
            seen["all"] = seen["open"] = seen["runs"] = 0
            took = self.pass_seconds(rig)
            seen["took"] = took
            return seen

        def test_the_open_list_pages_of_the_pass_are_the_ceiling_and_nothing_else_grew(self):
            now = self.counted_pass()
            with mock.patch.object(sb, "OPEN_LIST_PAGES", sb.SWEEP_LIST_PAGES):
                old = self.counted_pass()
            pages = min(-(-self.ITEMS // sb.GITHUB_PAGE_SIZE), sb.OPEN_LIST_PAGES)
            self.assertEqual(now["open"], pages, "one pass reads the open-list pages up to the ceiling and no more")
            self.assertEqual(now["all"] - now["open"], old["all"] - old["open"], "every other call of the pass is the same as at the old cap")
            # 1 s a call, plus 19 s more for each timed-out workflow read of this rig
            self.assertEqual(now["took"], now["all"] + 19 * now["runs"])
            self.assertLessEqual(now["took"], sb.GITHUB_ROUTE_SECONDS + sb.GITHUB_ROUTE_READ_TIMEOUT + (sb.OPEN_LIST_PAGES - sb.SWEEP_LIST_PAGES) + (sb.COMMENT_WINDOW_PAGES - sb.SWEEP_LIST_PAGES), now["took"])
    return TestOpenListPassCalls
