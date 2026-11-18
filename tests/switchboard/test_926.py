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
