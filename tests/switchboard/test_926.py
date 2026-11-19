"""#926: a label ENDING `:parked` parks an issue in the ownerless check, whatever its prefix. Kept out of bin/test-switchboard (its line ceiling)."""
import unittest

from support.loader import sb

NOW = 10 * 86400.0
BODY = "Parked until: 1970-02-20"   # a day well after NOW


class TestForeignPrefixParkedLabel(unittest.TestCase):
    def split(self, reader, label):
        module = sb.load_module("triage_ownerless")
        item = {"number": 1, "pr": False, "state": "open", "labels": {label}, "author": "alice", "assignees": [], "updated": 0.0,
                "owner_body": False, "parked_until": reader(BODY, {label})}
        ctx = {"chain": set(), "owned": set(), "unresolved": set()}
        return module.ownerless_split({1: item}, ctx, (), (), NOW, parked_now=sb.triage_parked_now)

    def test_a_foreign_prefix_label_with_a_future_date_parks_the_issue(self):
        self.assertEqual(self.split(sb.parked_until, "x:parked"), ([], [(1, 10.0)]))

    def test_a_reader_restricted_to_the_fleet_prefix_would_list_it(self):   # the mutant: this is what the test above guards against
        fleet = sb.label_names({})["parked"]
        mutant = lambda body, labels: sb.parked_until(body, [l for l in labels if l == fleet])
        self.assertEqual(self.split(mutant, "x:parked"), ([(1, 10.0)], []))


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
