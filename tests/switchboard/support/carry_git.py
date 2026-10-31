"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): carry_git."""
import argparse
import ast
import traceback
import base64
import calendar
import collections
import contextlib
import errno
import copy
import datetime as _dt
import fcntl
import gzip
import fnmatch
import hashlib
import hmac
import importlib.machinery
import importlib.util
import inspect
import http.client
import io
import itertools
import select
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote
import shlex
import shutil
import textwrap
import zipfile
import signal
import socket
import stat
import tarfile
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
import unittest
import uuid
import urllib.parse
from unittest import mock
from support.paths import BIN, REPO


class GitHubOverGit:
    """The slice of GitHub's REST answers the carry reads, produced from a REAL git repository (so a merge, a conflict, a rebase, a binary file and a rename are the real thing):
    pulls/N, pulls/N/files, compare/A...B and commits/SHA, with GitHub's shapes (a patch starts at the first hunk and is absent for a binary file, files are paged by 100)."""

    def __init__(self, repo_dir, branch="feature", base="main", number=7):
        self.dir, self.branch, self.base, self.number = str(repo_dir), branch, base, number
        self.calls, self.fail, self.changed_files_override = [], None, None

    def git(self, *args, check=True):
        done = subprocess.run(["git", "-C", self.dir] + list(args), capture_output=True, text=True)
        if check and done.returncode != 0:
            raise RuntimeError("git %s: %s" % (" ".join(args), done.stderr.strip()))
        return done.stdout

    def rev(self, ref):
        return self.git("rev-parse", ref).strip()

    def files_between(self, a, b):
        key = (self.rev(a), self.rev(b))
        self.cache = getattr(self, "cache", {})
        if key not in self.cache:
            self.cache[key] = self._files_between(a, b)
        return self.cache[key]

    def _files_between(self, a, b):
        merge_base = self.git("merge-base", a, b).strip()
        names = [line.split("\t") for line in self.git("diff", "-M", "--name-status", merge_base, b).splitlines()]
        patches = [section for section in ("\n" + self.git("diff", "-M", merge_base, b)).split("\ndiff --git ")[1:]]
        tree_b = {line.split("\t")[1]: line.split()[2] for line in self.git("ls-tree", "-r", b).splitlines()}
        tree_base = {line.split("\t")[1]: line.split()[2] for line in self.git("ls-tree", "-r", merge_base).splitlines()}
        out = []
        for parts, section in zip(names, patches):
            code, paths = parts[0], parts[1:]
            path, previous = paths[-1], (paths[0] if code.startswith("R") else None)
            status = {"A": "added", "M": "modified", "D": "removed", "R": "renamed"}[code[0]]
            patch = section[section.index("\n@@") + 1:].rstrip("\n") if "\n@@" in section else None
            changes = sum(1 for l in (patch or "").splitlines() if l[:1] in "+-" and not l.startswith(("+++", "---")))
            blob = tree_base[path] if status == "removed" else tree_b[path]
            item = {"filename": path, "status": status, "sha": blob, "changes": changes}
            if previous:
                item["previous_filename"] = previous
            if patch is not None:
                item["patch"] = patch
            out.append(item)
        return out

    def page(self, items, query):
        number = int(query.get("page", ["1"])[0])
        return items[(number - 1) * 100:number * 100]

    def get(self, path):
        self.calls.append(path)
        if self.fail and self.fail in path:
            raise RuntimeError("HTTP 502 for %s" % path)
        head = self.rev(self.branch)
        key = (path, head, self.base, self.changed_files_override)   # a git repository is immutable by sha: the same question at the same tip has the same answer
        self.answers = getattr(self, "answers", {})
        if key not in self.answers:
            self.answers[key] = self._answer(path, head)
        return self.answers[key]

    def _answer(self, path, head):
        route, _, query = path.partition("?")
        query = dict(q.split("=", 1) for q in query.split("&") if q)
        query = {k: [v] for k, v in query.items()}
        parts = route.split("/")                      # repos / owner / repo / ...
        tail = "/".join(parts[3:])
        if tail == "pulls/%d" % self.number:
            files = self.files_between(self.base, head)
            return {"head": {"sha": head}, "base": {"ref": self.base}, "changed_files": self.changed_files_override if self.changed_files_override is not None else len(files)}
        if tail == "pulls/%d/files" % self.number:
            return self.page(self.files_between(self.base, head), query)
        if tail.startswith("compare/"):
            a, b = tail[len("compare/"):].split("...")
            if self.rev(a) == self.rev(b):
                status = "identical"
            elif subprocess.run(["git", "-C", self.dir, "merge-base", "--is-ancestor", a, b]).returncode == 0:
                status = "ahead"
            elif subprocess.run(["git", "-C", self.dir, "merge-base", "--is-ancestor", b, a]).returncode == 0:
                status = "behind"
            else:
                status = "diverged"
            commits = []
            for line in self.git("rev-list", "--parents", "--reverse", "%s..%s" % (a, b)).splitlines():
                sha, *parents = line.split()
                commits.append({"sha": sha, "parents": [{"sha": p} for p in parents]})
            return {"status": status, "total_commits": len(commits), "commits": self.page(commits, query), "files": self.page(self.files_between(a, b), query)}
        if tail.startswith("commits/"):
            sha = tail[len("commits/"):]
            parents = self.git("rev-list", "--parents", "-n", "1", sha).split()[1:]
            files = []
            if parents:
                for line in self.git("diff", "--name-status", parents[0], sha).splitlines():
                    code, path = line.split("\t")[0], line.split("\t")[-1]
                    files.append({"filename": path, "status": {"A": "added", "M": "modified", "D": "removed"}.get(code[0], "modified"), "sha": self.rev("%s:%s" % (sha if code[0] != "D" else parents[0], path))})
            return {"sha": sha, "parents": [{"sha": p} for p in parents], "files": self.page(files, query)}
        raise RuntimeError("the fake GitHub has no answer for %s" % path)
