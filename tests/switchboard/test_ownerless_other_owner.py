"""#34: the ownerless finder leaves another owner's issues out of its list and counts them apart."""
import unittest

from support.loader import sb

NOW = 10 * 86400.0


class TestOwnerlessOtherOwner(unittest.TestCase):
    def test_other_owner_issues_are_counted_apart_and_never_listed(self):
        module = sb.load_module("triage_ownerless")
        ctx = {"chain": set(), "owned": set(), "unresolved": set()}
        make = lambda n, author: {"number": n, "pr": False, "state": "open", "labels": set(), "author": author, "assignees": [], "updated": 0.0, "owner_body": False}
        items = {n: make(n, "DoThompson_Example") for n in range(1, 98)}
        items.update({n: make(n, "alice") for n in range(98, 102)})
        others = ["dothompson_example"]
        rows, parked = module.ownerless_split(items, ctx, (), (), NOW, other_owners=others)
        self.assertEqual([n for n, _ in rows], [98, 99, 100, 101])
        self.assertEqual(parked, [])
        count = module.other_owner_count(items, others, NOW)
        self.assertEqual(count, 97)
        self.assertEqual(module.other_owner_count(items, [], NOW), 0)
        line = module.summary_line("acme-org/widget-service", rows, 101, 101, other_owner=count)
        self.assertIn("; other-owner 97;", line)
        self.assertNotIn("other-owner", module.summary_line("acme-org/widget-service", rows, 101, 101))

    def test_the_status_line_shows_the_count_only_when_there_is_one(self):
        def status(info):
            triage = {"counts": {}, "ownerless": {"acme-org/widget-service": info}}
            return "\n".join(sb.triage_status_lines(triage))
        base = {"count": 4, "parked": 0, "read": 101, "total": 101}
        self.assertIn("other-owner 97", status(dict(base, other_owner=97)))
        self.assertNotIn("other-owner", status(base))
