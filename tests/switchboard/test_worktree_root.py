"""Ask #2363: where the fleet makes a worktree (config `worktree_root`, default ~/Code/.fleet). S1 the key, S2 the path check and the local launch, S3 the text.
Real paths in a temporary HOME and real git; no GitHub call, no state directory. The names are neutral."""
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support.loader import captured, sb  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


class Reached(Exception):
    """Raised by a stub placed AFTER the worktree check: the launch got past it."""


class Home(unittest.TestCase):
    """A temporary HOME with ~/Code, a main checkout in it and a place outside it that is not the temp directory's own children of ~/Code."""

    def setUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="wtroot-")))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.base)])
        self.home = self.base / "home"
        self.code = self.home / "Code"
        self.code.mkdir(parents=True)
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        self.temp = self.base / "tmp"   # the temp directory, apart from HOME (the system's own may hold the whole fixture)
        self.temp.mkdir()
        patch = mock.patch.object(tempfile, "tempdir", str(self.temp))
        patch.start()
        self.addCleanup(patch.stop)
        self.main = self.code / "widgets"
        self.git("init", "-q", str(self.main), cwd=self.base)
        self.git("-c", "user.name=n", "-c", "user.email=n@example.com", "commit", "-q", "--allow-empty", "-m", "first", cwd=self.main)

    def git(self, *args, cwd):
        subprocess.run(["git"] + list(args), cwd=str(cwd), check=True, capture_output=True)

    def worktree(self, path, branch):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "-q", "-b", branch, str(path), cwd=self.main)
        return str(path)


class TestKey(Home):
    def test_s1_absent_and_null_are_the_default_and_a_good_value_is_kept(self):
        self.assertNotIn("worktree_root", sb.validate_config({}))
        self.assertNotIn("worktree_root", sb.validate_config({"worktree_root": None}))
        self.assertEqual(sb.worktree_root({}), "~/Code/.fleet")
        for good in ("~/Code/.trees", str(self.code / "x" / "y"), os.path.join(tempfile.gettempdir(), "fleet-roots")):
            with self.subTest(good=good):
                self.assertEqual(sb.validate_config({"worktree_root": good})["worktree_root"], good)

    def test_s1_a_bad_value_is_a_config_error_naming_the_key(self):
        outside = str(self.base / "elsewhere")
        for bad in ("", " ", 7, ["~/Code/x"], "relative/dir", "~/Code", str(self.code), "~", outside, "/", "~/Code/../Other", "~/Code/x\ny"):
            with self.subTest(bad=bad):
                with self.assertRaises(sb.ConfigError) as caught:
                    sb.validate_config({"worktree_root": bad})
                self.assertIn("worktree_root", str(caught.exception))


class TestCheck(Home):
    def test_s2_a_new_top_level_entry_of_code_is_refused_and_the_text_names_the_root(self):
        config = {"worktree_root": "~/Code/.trees"}
        for path in (self.code / "fresh", self.code / "fresh" / ".." / "other", str(self.code) + "/fresh/"):
            with self.subTest(path=str(path)):
                text = sb.check_worktree_path(config, str(path))
                self.assertIsInstance(text, str)
                self.assertIn("~/Code/.trees", text)

    def test_s2_a_symlink_into_code_does_not_hide_a_top_level_entry(self):
        elsewhere = self.base / "link-home"
        elsewhere.mkdir()
        (elsewhere / "to-code").symlink_to(self.code)
        self.assertIsNotNone(sb.check_worktree_path({}, str(elsewhere / "to-code" / "fresh")))

    def test_s2_the_root_the_temp_directory_and_deeper_paths_are_accepted(self):
        for config in ({}, {"worktree_root": "~/Code/.trees"}):
            with self.subTest(config=config):
                root = sb.worktree_root(config)
                self.assertIsNone(sb.check_worktree_path(config, os.path.join(os.path.expanduser(root), "widgets", "job-a")))
                self.assertIsNone(sb.check_worktree_path(config, os.path.join(tempfile.gettempdir(), "job-b")))
                self.assertIsNone(sb.check_worktree_path(config, str(self.main / "sub" / "job-c")))

    def test_s2_the_default_root_is_named_when_the_key_is_absent(self):
        self.assertIn("~/Code/.fleet", sb.check_worktree_path({}, str(self.code / "fresh")))

    def test_s2_a_symlink_as_the_last_component_is_still_a_top_level_entry(self):
        target = self.base / "far" / "wt"
        target.mkdir(parents=True)
        (self.code / "fresh").symlink_to(target)
        self.assertIsNotNone(sb.check_worktree_path({}, str(self.code / "fresh")))

    def test_s2_a_path_that_is_not_absolute_is_refused(self):
        for path in ("fresh", "", None, 5):
            with self.subTest(path=path):
                self.assertIsInstance(sb.check_worktree_path({}, path), str)


class TestLaunch(Home):
    """The real `launch` command of a LOCAL worker, stopped by a stub right after the check (Reached): a refusal never gets there."""

    def launch(self, directory, check=True):
        patches = [mock.patch.object(sb, "build_engine", lambda *a, **k: (types.SimpleNamespace(config={}, audit=None, actor="t"), "state", {})),
                   mock.patch.object(sb, "refuse_cloud_name", lambda name: None),
                   mock.patch.object(sb, "launch_model_choice", lambda *a: {}),
                   mock.patch.object(sb, "launch_memory_check", lambda *a: None),
                   mock.patch.object(sb, "launch_repo", mock.Mock(side_effect=Reached))]
        if not check:
            patches.append(mock.patch.object(sb, "check_worktree_path", lambda config, path: None))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        argv = ["launch", "worker-a", "--agent", "coder", "--dir", directory, "--headless", "--brief-file", os.devnull]
        with captured() as (out, err):
            try:
                code = sb.main(argv, runner=subprocess.run, now=lambda: 0, socket_call=None)
            except Reached:
                return "reached", err.getvalue()
        return code, err.getvalue()

    def test_s2_a_worktree_that_is_a_new_top_level_entry_of_code_is_refused_by_launch(self):
        top = self.worktree(self.code / "job-top", "job-top")
        code, err = self.launch(top)
        self.assertNotEqual(code, "reached")
        self.assertIn("~/Code/.fleet", err)

    def test_s2_a_worktree_under_the_root_and_one_in_the_temp_directory_start(self):
        under = self.worktree(self.code / ".fleet" / "widgets" / "job-root", "job-root")
        self.assertEqual(self.launch(under)[0], "reached")

    def test_s2_a_worktree_in_the_temp_directory_starts(self):
        temp = self.worktree(self.temp / "job-temp", "job-temp")
        self.assertEqual(self.launch(temp)[0], "reached")

    def test_s2_a_subdirectory_of_a_top_level_worktree_is_refused_too(self):
        top = self.worktree(self.code / "job-top", "job-top")
        Path(top, "sub").mkdir()
        self.assertNotEqual(self.launch(os.path.join(top, "sub"))[0], "reached")

    def test_s2_a_relative_dir_is_judged_by_where_it_is(self):
        under = self.worktree(self.code / ".fleet" / "widgets" / "job-rel", "job-rel")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(under)
        self.assertEqual(self.launch(".")[0], "reached")

    def test_s2_the_owners_main_checkout_in_code_is_not_refused(self):
        self.assertEqual(self.launch(str(self.main))[0], "reached")

    def test_s2_control_without_the_check_the_same_top_level_worktree_starts(self):
        """A bypass control (the check switched off, which is what the launch did before this change): the same launch goes on, so the refusal above is the check's own."""
        top = self.worktree(self.code / "job-top", "job-top")
        self.assertEqual(self.launch(top, check=False)[0], "reached")


class TestText(unittest.TestCase):
    def test_s3_the_skill_names_the_root_once_and_no_line_makes_a_top_level_worktree(self):
        flat = " ".join((REPO / "skills" / "fleet-coordination" / "SKILL.md").read_text().split())
        self.assertIn("worktree_root", flat)
        self.assertIn("~/Code/.fleet", flat)
        for path in (REPO / "skills").rglob("*"):
            if path.is_file() and path.suffix in (".md", ".txt"):
                for line in path.read_text().splitlines():
                    self.assertNotRegex(line, r"worktree add\s+~/Code/[^/\s]+(\s|$)", str(path))
        for path in list((REPO / "agents").rglob("*.md")):
            for line in path.read_text().splitlines():
                self.assertNotRegex(line, r"(make|create|add)\b[^.]*worktree[^.]*(top-level|directly in) ~/Code", str(path))


if __name__ == "__main__":
    unittest.main()
