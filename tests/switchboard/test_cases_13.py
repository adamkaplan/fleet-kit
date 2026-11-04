"""Test classes moved out of bin/test-switchboard (text unchanged): cases_13."""
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
from support.paths import REPO


class TestClosedUnmergedDocs(unittest.TestCase):
    """#1347 S9: the documentation says plainly that a closed-unmerged last pull request is a defect and what clears it."""

    def test_the_union_check_section_names_the_defect_the_refusals_and_the_waiver(self):
        text = (REPO / "docs/switchboard.md").read_text()
        item = [l for l in text.split("\n") if l.startswith("- **A closed-unmerged last pull request is a defect")]
        self.assertEqual(len(item), 1)
        for needle in ("`gates ask-close` refuses the ask and names the unclaimed scenarios and the closed pull requests", "the next pull request's merge is refused too",
                       "a pull request claims the scenario", "head-bound `CU` waiver"):
            self.assertIn(needle, item[0])


class TestUnionCheckLimitsDocs(unittest.TestCase):
    """#1347 S12: the union-check section states its three limits plainly."""

    def test_the_section_states_the_three_limits(self):
        text = (REPO / "docs/switchboard.md").read_text()
        for needle in ("An ask whose only pull request closed unmerged with no claim line and no narrowing claim anywhere passes.",
                       "A last merge made before the union gate existed is not caught at `gates ask-close`.",
                       "A failed read refuses the close, and only the head-bound `CU` waiver clears it."):
            self.assertEqual(text.count(needle), 1, needle)
