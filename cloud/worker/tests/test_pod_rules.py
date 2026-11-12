"""Ask #2402: the pod's OpenCode permission file (cloud/worker/opencode-worker.json) holds no convenience rule. Its bash map is the single row
'* allow' (the operator's long-run rows are added at start by entrypoint.sh, see test_entrypoint_long_run.py), 'question' stays 'deny', and there is no
'edit' rule. The boundaries are the token's scope, the repository's rulesets, the egress policy and the missing cluster token, not this file.
Offline; everything is derived from the file."""
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.dirname(HERE)
CONF = os.path.join(WORKER, "opencode-worker.json")


def load():
    with open(CONF) as f:
        return json.load(f)


def permissions(conf):
    return {"top": conf["permission"], "worker": conf["agent"]["worker"]["permission"]}


class PodRules(unittest.TestCase):
    def test_the_bash_map_has_no_deny_row_and_no_row_but_the_catch_all_allow(self):
        for name, perm in permissions(load()).items():
            bash = perm["bash"]
            self.assertEqual([k for k, v in bash.items() if v == "deny"], [], name)
            self.assertEqual(bash, {"*": "allow"}, name)

    def test_question_is_denied_in_both_maps_and_nothing_else_is_ruled(self):
        for name, perm in permissions(load()).items():
            self.assertEqual(perm["question"], "deny", name)
            self.assertEqual(sorted(perm), ["bash", "question"], name)   # no 'edit' rule, no other tool rule

    def test_the_agent_block_holds_only_the_worker_permission(self):
        conf = load()
        self.assertEqual(sorted(conf), ["agent", "permission"])
        self.assertEqual(list(conf["agent"]), ["worker"])
        self.assertEqual(list(conf["agent"]["worker"]), ["permission"])


if __name__ == "__main__":
    unittest.main()
