"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): rigs."""
import io
import json
from pathlib import Path
import zipfile
from support.carry_git import GitHubOverGit


def build_carry_repo(directory):
    """A real git repository (neutral names): `main` with a few files and a `feature` branch whose own change is a.txt edited and c.txt added. Returns the GitHubOverGit over it."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    api = GitHubOverGit(directory)
    g = api.git

    def write(path, text):
        target = directory / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "dev@example.test")
    g("config", "user.name", "Dev")
    g("config", "commit.gpgsign", "false")
    write("README.md", "widget api\n")
    write("a.txt", "one\ntwo\nthree\nfour\nfive\nsix\nseven\n")
    write("src/x.py", "def x():\n    return 1\n")
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    g("checkout", "-q", "-b", "feature")
    write("a.txt", "one\nTWO\nthree\nfour\nfive\nsix\nseven\n")
    write("c.txt", "new file\n")
    g("add", "-A")
    g("commit", "-q", "-m", "the change")
    return api


def _artifact_zip(names, complete=True, run=1, head="a" * 40):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("failing-tests.json", json.dumps({"v": 1, "run": run, "head": head, "complete": complete, "failed": names}))
    return stream.getvalue()
