"""Ask #2483: `delegated_clear.repo` names the repository of the charter that holds the standing orders.

The delegated clear used to read the charter issue from the configuration's `intent_repo`, which can be another repository than the one that holds the charter, so a listed order
"did not exist". These tests drive `delegated_clear_checks` with a fake reader and record which repository the orders were read from. Examples are neutral (acme-org/widget-service).
"""
import types
import unittest
from unittest import mock

from support.loader import sb

INTENT = "acme-org/intent-hub"
CHARTER_REPO = "acme-org/widget-service"
KIND = ("agent", "cos", "chief-of-staff")
TEXT = "lane-a may clear ordinary held pull requests"


def make_config(**extra):
    module = sb.load_module("delegated_clear")
    block = dict({"orders": [{"number": 2, "sha256": module.order_hash(TEXT)}], "charter": 34}, **extra)
    return {"intent_repo": INTENT, "delegated_clear": block}


class Reads:
    """A stand-in for fetch_orders: records the repository read and answers only for the repository that holds the charter."""

    def __init__(self):
        self.repos = []

    def __call__(self, reader, charter, repo):
        self.repos.append((repo, charter))
        return ["order one", TEXT] if repo == CHARTER_REPO else []


class TestDelegatedClearRepo(unittest.TestCase):
    def check(self, config, number=2):
        reads = Reads()
        engine = types.SimpleNamespace(config=config)
        with mock.patch.object(sb, "fetch_orders", reads), mock.patch.object(sb, "_intent_reader", lambda runner, now: object()):
            result = sb.delegated_clear_checks(engine, None, 0, KIND, number)
        return result, reads

    def test_s2_the_named_repository_is_read_when_repo_is_set(self):
        (order, why), reads = self.check(make_config(repo=CHARTER_REPO))
        self.assertEqual(reads.repos, [(CHARTER_REPO, 34)])
        self.assertIsNone(why)
        self.assertEqual(order["number"], 2)

    def test_s2_intent_repo_is_read_when_repo_is_absent(self):
        (order, why), reads = self.check(make_config())
        self.assertEqual(reads.repos, [(INTENT, 34)], "an installation without the key reads what it read before")
        self.assertIsNone(order)
        self.assertIn("does not exist", why)

    def test_s5_control_without_the_key_the_wrong_repository_is_read(self):
        """The case of 12 November 2026: the key is not honoured (as the old code) => intent_repo is read and the order is missing."""
        config = make_config(repo=CHARTER_REPO)
        del config["delegated_clear"]["repo"]
        (order, why), reads = self.check(config)
        self.assertEqual(sum(1 for repo, _ in reads.repos if repo != CHARTER_REPO), 1)
        self.assertIsNone(order)

    def test_s3_a_malformed_repo_is_refused_naming_the_key(self):
        module = sb.load_module("delegated_clear")
        accepted = 0
        for bad in ("widget-service", "a/b/c", "", " a/b", "a/b\n", 5, None, True, ["a/b"], "a b/c", "a/b#34"):
            with self.subTest(value=bad):
                try:
                    module.validate(make_config(repo=bad)["delegated_clear"], "config")
                    accepted += 1
                except ValueError as err:
                    self.assertIn("delegated_clear.repo", str(err))
                self.assertIsNone(module.settings(make_config(repo=bad)), "a malformed repo turns the delegation off (fails closed)")
        self.assertEqual(accepted, 0)
        module.validate(make_config(repo=CHARTER_REPO)["delegated_clear"], "config")

    def test_s3_the_configuration_loader_refuses_a_malformed_repo(self):
        with self.assertRaises(sb.ConfigError) as caught:
            sb.validate_config({"delegated_clear": make_config(repo="nope")["delegated_clear"]}, "config.json")
        self.assertIn("delegated_clear.repo", str(caught.exception))
        sb.validate_config({"delegated_clear": make_config(repo=CHARTER_REPO)["delegated_clear"]}, "config.json")   # a well formed one loads
        sb.validate_config({"delegated_clear": make_config()["delegated_clear"]}, "config.json")   # and so does none

    def test_s5_control_the_old_reader_reads_intent_repo_and_the_s2_expectation_fails(self):
        """The old behaviour planted (`charter_repo` always answers its default): the repository read is intent_repo, so S2's expectation does not hold."""
        module = sb.load_module("delegated_clear")
        with mock.patch.object(module, "charter_repo", lambda config, default: default):
            (order, why), reads = self.check(make_config(repo=CHARTER_REPO))
        self.assertEqual(reads.repos, [(INTENT, 34)])
        self.assertIsNone(order)
        self.assertEqual(why, "order 2 does not exist on charter %s#34" % INTENT)
        self.assertNotEqual(reads.repos, [(CHARTER_REPO, 34)])

    def test_s4_the_refusal_names_the_repository_read(self):
        config = make_config(repo=CHARTER_REPO)
        config["delegated_clear"]["orders"] = [{"number": 7, "sha256": "0" * 64}]
        (order, why), _ = self.check(config, number=7)
        self.assertEqual(why, "order 7 does not exist on charter %s#34" % CHARTER_REPO)

    def test_s4_the_refusal_names_intent_repo_when_the_key_is_absent(self):
        (order, why), _ = self.check(make_config())
        self.assertEqual(why, "order 2 does not exist on charter %s#34" % INTENT)

    def test_pc1_the_other_refusals_do_not_print_order_text(self):
        config = make_config(repo=CHARTER_REPO)
        config["delegated_clear"]["orders"] = [{"number": 2, "sha256": "0" * 64}]
        (order, why), _ = self.check(config)
        self.assertIsNone(order)
        self.assertNotIn(TEXT, why)

    def test_charter_repo_defaults_when_the_delegation_is_off(self):
        module = sb.load_module("delegated_clear")
        self.assertEqual(module.charter_repo({}, INTENT), INTENT)
        self.assertEqual(module.charter_repo({"delegated_clear": {"orders": [], "repo": CHARTER_REPO}}, INTENT), INTENT)
        self.assertEqual(module.charter_repo(make_config(repo=CHARTER_REPO), INTENT), CHARTER_REPO)
