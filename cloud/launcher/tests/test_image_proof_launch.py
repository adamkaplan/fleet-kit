#!/usr/bin/env python3
"""Ask #2440: the proof command launches through `fleet-switchboard launch --cloud` with a scratch configuration directory, the brief file, and the proof set's
command lines. Offline: a fake switchboard that records its arguments and environment; mutants are in-memory copies of the source."""
import fnmatch
import json
import os
import re
import shutil
import tempfile
import types
import unittest
from pathlib import Path

import test_image_gate as G

ROOT = G.ROOT
SB = G.SB
PROOF = G.PROOF
ANCHOR = "the real settings file was changed by a proof"
IMAGE_LINE = re.compile(r"(?m)^CW_CLOUD_IMAGE=.*$")


class Launch(G.Base):
    def setUp(self):
        super().setUp()
        Path(self.conf, "config.json").write_text("{}")
        os.mkdir(os.path.join(self.conf, "agents"))
        os.symlink(os.path.join(self.tmp, "elsewhere"), os.path.join(self.conf, "secrets"))
        self.base = os.path.join(self.state, SB, "cloud")

    def tree(self):
        return sorted(os.listdir(self.base)) if os.path.isdir(self.base) else []

    def scratches(self):
        return [n for n in self.tree() if n.startswith("image-proof-config-")]

    def run_proof(self, module=None, wait=0):
        module = module or G.load(PROOF)
        args = types.SimpleNamespace(digest=G.SRC, repo="acme/widget", branch="integration", wait=wait)
        return module.prove(args, dict(self.env), now=lambda: G.NOW, sleep=lambda s: None)

    def assert_real_unchanged(self, before):
        if self.snap() != before:
            raise AssertionError(ANCHOR)

    def s2_case(self, module):
        before = self.snap()
        self.run_proof(module)
        self.assert_real_unchanged(before)

    def test_s2_the_scratch_directory_holds_the_candidate_and_links_and_the_real_file_is_unchanged(self):
        before = self.snap()
        self.run_proof()
        seen = Path(self.fake, "seen.env").read_text()
        self.assertEqual(IMAGE_LINE.findall(seen), ["CW_CLOUD_IMAGE=" + G.SRC])
        self.assertEqual(seen.replace("CW_CLOUD_IMAGE=" + G.SRC, "X"), Path(self.settings).read_text().replace("CW_CLOUD_IMAGE=reg.example/cw@sha256:" + "c" * 64, "X"))
        self.assertEqual(self.snap(), before)

    def test_s2_entries_are_linked_and_the_copy_is_0600_in_a_0700_directory(self):
        module = G.load(PROOF)
        scratch = module.make_scratch(dict(self.env), G.SRC)
        try:
            inner = os.path.join(scratch, SB)
            self.assertEqual(oct(os.stat(scratch).st_mode & 0o777), "0o700")
            self.assertEqual(oct(os.stat(os.path.join(inner, "cloud.env")).st_mode & 0o777), "0o600")
            self.assertFalse(os.path.islink(os.path.join(inner, "cloud.env")))
            for name in ("config.json", "agents", "secrets"):
                self.assertEqual(os.readlink(os.path.join(inner, name)), os.path.join(self.conf, name))
            self.assertEqual(os.readlink(os.path.join(scratch, "opencode")), os.path.join(self.xdg, "opencode"))
            self.assertEqual(os.path.basename(scratch)[:19], "image-proof-config-")
        finally:
            shutil.rmtree(scratch, True)

    def test_s3_the_launch_names_cloud_the_brief_the_scratch_directory_and_the_retire_the_same_pod(self):
        self.run_proof()
        launches = [l for l in self.log("launcher") if l.startswith("launch ")]
        retires = [l for l in self.log("launcher") if l.startswith("retire ")]
        self.assertEqual((len(launches), len(retires)), (1, 1))
        words = launches[0].split()
        for want in ("--cloud", "--repo", "acme/widget", "--branch", "integration", "--role", "tester", "--agent", "coder", "--shape", "M", "--issue", "2418",
                     "--idle", "40m", "--hard", "70m", "--brief-file"):
            self.assertIn(want, words)
        self.assertEqual(words[words.index("--brief-file") + 1], str(ROOT / "cloud" / "worker" / "proof-brief.md"))
        self.assertEqual(retires[0].split()[1], words[1])
        self.assertEqual(retires[0].split()[2:], ["--close"])
        seen_env = Path(self.fake, "launcher.env").read_text()
        scratch = Path(self.fake, "seen.scratch").read_text().strip()
        self.assertIn("XDG_CONFIG_HOME=" + scratch, seen_env)
        self.assertNotEqual(scratch, self.xdg)
        self.assertTrue(scratch.startswith(self.base + os.sep + "image-proof-config-"))
        self.assertIn("CW_CLOUD_IMAGE=unset", seen_env)

    def test_s3_the_pod_is_taken_from_the_launch_output_for_the_exec(self):
        self.run_proof()
        self.assertTrue(any(" proof-pod-1 " in " " + l + " " for l in self.log("kubectl")), self.log("kubectl"))

    def test_s3_no_cloud_worker_program_is_run_directly(self):
        self.assertNotIn('"create"', PROOF)
        self.assertNotIn("cloud-worker", PROOF.split('"""', 2)[2])

    def test_s4_the_scratch_directory_is_gone_and_the_pod_retired_after_a_pass_a_fail_and_a_launch_failure(self):
        for label in ("pass", "fail", "launch"):
            self.setUp()
            if label == "fail":
                Path(self.fake, "results").write_text("pg-env pass\n")
            if label == "launch":
                Path(self.fake, "launchexit").write_text("1")
                module = G.load(PROOF)
                with self.assertRaises(module.Refused):
                    self.run_proof(module)
            else:
                self.run_proof()
            self.assertEqual(self.scratches(), [], label)
            self.assertEqual(len([l for l in self.log("launcher") if l.startswith("retire ")]), 1, label)

    def test_stale_scratch_directories_of_earlier_runs_are_removed_and_other_names_are_kept(self):
        os.makedirs(os.path.join(self.base, "image-proof-config-old", SB))
        os.makedirs(os.path.join(self.base, "keep-me"))
        self.run_proof()
        self.assertEqual(self.tree(), ["image-proof", "keep-me"])

    def test_a_settings_file_without_one_image_line_is_refused_before_any_launch(self):
        Path(self.settings).write_text("A=1\n")
        module = G.load(PROOF)
        with self.assertRaises(module.Refused):
            self.run_proof(module)
        self.assertEqual(self.log("launcher"), [])
        self.assertEqual(self.scratches(), [])

    def test_s8_control_passes_and_a_mutant_writing_the_image_into_the_real_file_fails_with_the_anchored_text(self):
        self.s2_case(G.load(PROOF))   # the control first
        self.setUp()
        old = "        scratch = make_scratch(env, args.digest)\n"
        self.assertEqual(PROOF.count(old), 1)
        new = old + ('        real = os.path.join(config_home(env), SWITCHBOARD, "cloud.env")\n'
                     '        open(real, "w").write(re.sub(r"(?m)^CW_CLOUD_IMAGE=.*$", "CW_CLOUD_IMAGE=" + args.digest, open(real).read()))\n')
        mutated = PROOF.replace(old, new)
        self.assertNotEqual(mutated, PROOF)
        with self.assertRaises(AssertionError) as caught:
            self.s2_case(G.load(mutated))
        self.assertIn(ANCHOR, str(caught.exception))


class Brief(unittest.TestCase):
    def test_s5_the_brief_names_both_paths_and_holds_no_secret_or_identity(self):
        text = (ROOT / "cloud" / "worker" / "proof-brief.md").read_text()
        self.assertEqual(text.count("/work/proof-set.md") >= 1 and text.count("/work/proof-results.txt") >= 1, True)
        self.assertIsNone(re.search(r"(ghp_|github_pat_|Bearer |sk-[A-Za-z0-9]{10}|[A-Za-z0-9+/]{40,}|@[a-z0-9-]+\.[a-z]{2,}|\$\{?CW_|#\d+)", text))
        self.assertNotIn(str(ROOT), text)


def proof_items():
    text = (ROOT / "cloud" / "worker" / "proof-set.md").read_text()
    out = {}
    files = re.findall(r"^- (tests/\S+\.test\.ts)$", text, re.M)
    for name, section in re.findall(r"^### (\S+)\n(.*?)(?=^###? |\Z)", text, re.M | re.S):
        lines = []
        for body in re.findall(r"```\n(.*?)\n```", section, re.S):
            for line in body.split("\n"):
                if "<the Nth file of the list>" in line or "proof-test-N" in line:
                    lines += [line.replace("proof-test-N", "proof-test-%d" % (i + 1)).replace("<the Nth file of the list>", f) for i, f in enumerate(files)]
                elif line.strip():
                    lines.append(line)
        out[name] = lines
    return out


def deny_table():
    rows = json.loads((ROOT / "cloud" / "worker" / "opencode-worker.json").read_text())["permission"]["bash"]
    return list(rows.items())


def wildcard(pattern):
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern), re.S)


def refused(command, rows, compiled):
    """The pod's rule: the LAST matching row decides (wildcards are * and ?); the fnmatch reading is judged too, and a deny by either is a refusal."""
    last = None
    last_fn = None
    for (pattern, action), rx in zip(rows, compiled):
        if rx.fullmatch(command):
            last = action
        if fnmatch.fnmatchcase(command, pattern):
            last_fn = action
    return last == "deny" or last_fn == "deny"


class Items(unittest.TestCase):
    def test_s6_no_command_line_of_any_item_matches_a_deny_row(self):
        items = proof_items()
        self.assertEqual(sorted(items), sorted(G.ITEMS))
        rows = deny_table()
        compiled = [wildcard(p) for p, _ in rows]
        self.assertGreater(len(rows), 1000)
        count = 0
        for item, lines in items.items():
            self.assertTrue(lines, item)
            for line in lines:
                count += 1
                self.assertFalse(refused(line, [r[0:2] for r in rows], compiled), (item, line))
        self.assertGreaterEqual(count, 7)

    def test_s6_discriminates_the_old_hook_command_and_a_push_are_refused(self):
        rows = deny_table()
        compiled = [wildcard(p) for p, _ in rows]
        for bad in ("git config --global core.hooksPath", "git config --global core.hooksPath=/x", "git push origin HEAD:main"):
            self.assertTrue(refused(bad, rows, compiled), bad)

    def test_the_hook_item_reads_a_file_and_never_asks_git_for_the_hooks_path(self):
        hook = "\n".join(proof_items()["hook"])
        self.assertIn("~/.gitconfig", hook)
        self.assertNotIn("git config", hook)
        self.assertNotIn("hooksPath=", hook)

    def test_the_set_says_each_item_and_still_names_the_eight_files(self):
        text = (ROOT / "cloud" / "worker" / "proof-set.md").read_text()
        self.assertEqual(len(re.findall(r"^- tests/\S+\.test\.ts$", text, re.M)), 8)
        self.assertIn("END YOUR TURN", text)


if __name__ == "__main__":
    unittest.main()
