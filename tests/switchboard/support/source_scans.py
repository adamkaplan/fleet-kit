"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): source_scans."""
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
import importlib
from support.paths import BIN, REPO
from support.fake_jev import SHIM
from support.paths import BIN


def _matches(keys, pattern):
    return any(fnmatch.fnmatchcase(key, pattern) for key in keys)


def _url_tail(url):
    match = re.search(r"/issues/(\d+)$", url or "")
    return int(match.group(1)) if match else None


def urllib_query(path):
    query = {}
    for pair in path.partition("?")[2].split("&"):
        if pair:
            name, _, value = pair.partition("=")
            query[name] = value
    return query


def load_check_public_names():
    loader = importlib.machinery.SourceFileLoader("check_public_names_under_test", str(BIN / "check-public-names"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


OLD_AGENT_SOURCES = re.compile(r"read_pane_health|PANE_HEALTH_FILE|RETIRED_FILE|pane-health\.json|retired\.json")   # the old copies of the agent list (docs/agent-registry.md)


def _fold(node):
    """The string an expression builds, with what is not a constant left out: a constant, a `+` chain, a `%` format, an f-string, `sep.join([...])`. None for
    anything else. `s + "/retired" + ".json"` folds to "/retired.json", the way a reader of the file would name it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        left, right = _fold(node.left), _fold(node.right)
        if isinstance(node.op, ast.Mod):
            return left
        return (left or "") + (right or "") if left is not None or right is not None else None
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join" and isinstance(node.func.value, ast.Constant) \
            and node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
        parts = [e.value for e in node.args[0].elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        return node.func.value.value.join(parts)
    return None


def old_source_reads(path, text):
    """{(function, token)} for every use of an old agent-list source in the Python source `text`: a name, an attribute, an import (and its alias, wherever it is
    called), a string or a concatenation of strings, outside a docstring. A source that is not Python is read as text: [("<text>", token)]."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return sorted({("<text>", m.group(0)) for m in OLD_AGENT_SOURCES.finditer(text)})
    found, aliases = set(), {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if OLD_AGENT_SOURCES.search(alias.name):
                    aliases[alias.asname or alias.name.split(".")[-1]] = OLD_AGENT_SOURCES.search(alias.name).group(0)

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            inner = child.name if fn is None and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            hit = None
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                hit = next((OLD_AGENT_SOURCES.search(a.name).group(0) for a in child.names if OLD_AGENT_SOURCES.search(a.name)), None)
            elif isinstance(child, ast.Name) and (OLD_AGENT_SOURCES.search(child.id) or child.id in aliases):
                hit = OLD_AGENT_SOURCES.search(child.id).group(0) if OLD_AGENT_SOURCES.search(child.id) else aliases[child.id]
            elif isinstance(child, ast.Attribute) and OLD_AGENT_SOURCES.search(child.attr):
                hit = OLD_AGENT_SOURCES.search(child.attr).group(0)
            elif isinstance(child, (ast.Constant, ast.BinOp, ast.JoinedStr, ast.Call)) and _fold(child) is not None and OLD_AGENT_SOURCES.search(_fold(child)) and not isinstance(node, ast.Expr):
                hit = OLD_AGENT_SOURCES.search(_fold(child)).group(0)
            if hit:
                found.add((inner or "<module>", hit))
            if not (isinstance(child, (ast.BinOp, ast.JoinedStr)) and _fold(child) is not None and hit):   # a folded expression is one finding
                walk(child, inner)
    walk(tree, None)
    return sorted(found)


def duplicate_defs(text):
    """[(scope, name)] for every class or function defined twice in one scope of the Python source `text`: a second definition silently shadows the first (a merge that doubles a block).
    A decorated function (a property's setter, an overload) is a deliberate second definition and is left out."""
    tree = ast.parse(text)
    found = []
    for scope in [tree] + [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        seen = {}
        for node in scope.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not getattr(node, "decorator_list", None):
                seen[node.name] = seen.get(node.name, 0) + 1
        found += [(getattr(scope, "name", "<module>"), name) for name, n in seen.items() if n > 1]
    return sorted(found)


def scanned(path):
    """True for a file the scan judges: not a test, a fixture, a document, or the registry module itself."""
    name = os.path.basename(path)
    skip = (path == "bin/registry.py" or path.endswith((".md", ".json", ".txt", ".lock", ".png", ".svg"))
            or name.startswith("test") or ".test." in name or "/tests/" in path or "/fixtures/" in path or path.startswith(("docs/", ".github/")))
    return not skip


BLOCKED_GUARD_KILLERS = {
    '_named:len(items) > limit': 'test_the_line_names_at_most_so_many_senders_and_reminders_and_is_cut',
    'validate_blocked_alert_config:"blocked_alert" not in config or config["blocked_alert"] is None': 'test_a_block_that_ends_before_it_was_reported_says_nothing',
    'validate_blocked_alert_config:raw is False': 'test_the_alert_can_be_set_or_turned_off',
    'validate_blocked_alert_config:not isinstance(raw, dict)': 'test_the_alert_can_be_set_or_turned_off',
    'validate_blocked_alert_config:unknown': 'test_blocked_alert_is_a_known_config_key_and_an_unknown_key_inside_it_is_warned_about',
    'validate_blocked_alert_config:not _is_number(section[key]) or section[key] <= 0': 'test_the_alert_can_be_set_or_turned_off',
    'blocked_alert_section:raw is False': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_kind:source is None': 'test_an_ended_turn_with_nothing_open_names_the_resume_command_with_the_agents_name',
    'blocked_kind:kinds': 'test_an_ended_turn_with_nothing_open_names_the_resume_command_with_the_agents_name',
    'blocked_kind:k == "permission"': 'test_the_kind_of_block_for_each_open_request_and_when_it_was_not_read',
    'blocked_update:new is None': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'note_held_send:state_dir is None or observation is None or observation.status != "blocked" or hold_reason(observation) is None': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'note_held_send:len(state) > BLOCKED_HELD_AGENTS': 'test_the_lists_are_bounded_and_the_next_over_is_cut',
    'note_held_send:isinstance(entry, dict)': 'test_a_block_that_ends_before_it_was_reported_says_nothing',
    'blocked_sender:isinstance(who, str) and NAME_RE.fullmatch(who)': 'test_every_sink_of_a_caller_claimed_sender_name_shows_only_a_valid_agent_name',
    'blocked_line:held or not refused': 'test_only_the_blocked_agents_own_reminders_due_in_the_block_are_in_the_line',
    'blocked_line:refused': 'test_over_the_threshold_with_held_messages_each_recipient_gets_one_line_naming_the_count_and_senders',
    'blocked_line:reminders': 'test_a_reminder_the_blocked_agent_set_that_falls_due_in_the_block_is_in_the_line',
    'blocked_line:len(held) == 1': 'test_the_end_line_of_a_mix_says_the_held_were_delivered_and_the_refused_were_not',
    'blocked_line:len(refused) == 1': 'test_over_the_threshold_with_held_messages_each_recipient_gets_one_line_naming_the_count_and_senders',
    '_refused_senders:isinstance(entry, dict)': 'test_a_block_that_ends_with_messages_still_held_says_so',
    '_refused_senders:isinstance(rows, list)': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:section is None or discovery is None or engine.state_dir is None': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:changed or done': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:set(found) - waiting': 'test_held_sends_of_an_agent_no_longer_blocked_are_forgotten_and_a_blocked_agents_are_kept',
    'blocked_tick:boss and boss_seen is not None and not hold_reason(boss_seen)': 'test_a_chief_of_staff_that_is_blocked_is_not_told_about_itself_and_none_is_added_to_nobody',
    'blocked_tick:cos and cos not in recipients': 'test_a_boss_that_cannot_be_reached_leaves_the_chief_of_staff_alone_told',
    'blocked_tick:engine.audit is not None': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:seen.status != "blocked"': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:not isinstance(entry, dict) or entry.get("session") != seen.session_id or not _is_number(entry.get("since"))': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:not messages and not refusals': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:last is None and now - entry["since"] >= section["minutes"] * 60': 'test_a_block_that_ends_before_it_was_reported_says_nothing',
    'blocked_tick:not isinstance(entry, dict) or entry.get("reported") is None': 'test_a_block_that_ends_before_it_was_reported_says_nothing',
    'blocked_tick:seen is None': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:still': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:refusals': 'test_the_end_line_of_a_block_with_only_refused_sends_does_not_say_delivered',
    'blocked_tick:boss': 'test_over_the_threshold_with_held_messages_each_recipient_gets_one_line_naming_the_count_and_senders',
    'blocked_tick:last is not None and now - last >= section["repeat_minutes"] * 60': 'test_a_block_that_ends_before_it_was_reported_says_nothing',
    'blocked_tick:had_held': 'test_the_end_line_of_a_block_with_only_refused_sends_does_not_say_delivered',
    'blocked_tick:plan is not None': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:parts': 'test_a_block_that_ends_with_messages_still_held_says_so',
    'blocked_tick:len(refusals) == 1': 'test_the_end_line_of_a_block_with_only_refused_sends_does_not_say_delivered',
}


BLOCKED_TEXT_MUTANTS = {   # id: (anchor function or None, old text, new text, the test that must fail)
    'bound:BLOCKED_HELD_KEEP': (None, 'BLOCKED_HELD_KEEP = 100', 'BLOCKED_HELD_KEEP = 1000', 'test_the_lists_are_bounded_and_the_next_over_is_cut'),
    'bound:BLOCKED_HELD_AGENTS': (None, 'BLOCKED_HELD_AGENTS = 64', 'BLOCKED_HELD_AGENTS = 640', 'test_the_lists_are_bounded_and_the_next_over_is_cut'),
    'bound:BLOCKED_LINE_CHARS': (None, 'BLOCKED_LINE_CHARS = 1200', 'BLOCKED_LINE_CHARS = 12000', 'test_the_lists_are_bounded_and_the_next_over_is_cut'),
    'bound:BLOCKED_LINE_SENDERS': (None, 'BLOCKED_LINE_SENDERS = 10', 'BLOCKED_LINE_SENDERS = 100', 'test_the_line_names_at_most_so_many_senders_and_reminders_and_is_cut'),
    'bound:BLOCKED_LINE_REMINDERS': (None, 'BLOCKED_LINE_REMINDERS = 5', 'BLOCKED_LINE_REMINDERS = 50', 'test_the_line_names_at_most_so_many_senders_and_reminders_and_is_cut'),
    'lock:flock': ('def blocked_lock', 'fcntl.flock(fd, fcntl.LOCK_EX)\n        yield', 'yield', 'test_two_interleaved_senders_lose_no_row'),
    'config-key:blocked_alert': (None, '"friction_wake", "blocked_alert", ', '"friction_wake", ', 'test_blocked_alert_is_a_known_config_key_and_an_unknown_key_inside_it_is_warned_about'),
    'validator:zero': ('def validate_blocked_alert_config', 'not _is_number(section[key]) or section[key] <= 0', 'not _is_number(section[key]) or section[key] < 0', 'test_the_alert_can_be_set_or_turned_off'),
    'guard-removed:boss-not-blocked': (None, 'boss and boss_seen is not None and not hold_reason(boss_seen)', 'boss and boss_seen is not None', 'test_a_boss_that_is_itself_blocked_is_not_told_and_the_chief_of_staff_alone_is'),
    'write:per-report': ('def blocked_tick', 'blocked_update(engine.state_dir, BLOCKED_ALERT_STATE, lambda _: state)', 'pass', 'test_each_report_is_written_at_once_so_a_crash_repeats_at_most_one'),
    'wording:and-more': ('def _named', '" and %d more"', '" and %d others"', 'test_the_line_names_at_most_so_many_senders_and_reminders_and_is_cut'),
    'wording:limit-slice': ('def _named', 'list(items)[:limit]', 'list(items)[:limit + 1]', 'test_the_line_names_at_most_so_many_senders_and_reminders_and_is_cut'),
    'sender:gate': (None, 'return who if isinstance(who, str) and NAME_RE.fullmatch(who) else "unknown"', 'return who if isinstance(who, str) else "unknown"', 'test_every_sink_of_a_caller_claimed_sender_name_shows_only_a_valid_agent_name'),
    'sender:fullmatch': (None, 'NAME_RE.fullmatch(who) else', 'NAME_RE.match(who) else', 'test_every_sink_of_a_caller_claimed_sender_name_shows_only_a_valid_agent_name'),
}


def blocked_functions(text):
    """The names of every function of the report: those between `BLOCKED_ALERT_DEFAULTS` and the end of `blocked_tick`, derived from the code."""
    tree = ast.parse(text)
    first = next(n.lineno for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "BLOCKED_ALERT_DEFAULTS")
    last = next(n.end_lineno for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "blocked_tick")
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef) and first <= n.lineno <= last}


def blocked_guards(text):
    """{id: (start, end)} byte offsets of the test of every `if`, conditional expression and `while` in the report's functions: derived from the code."""
    tree, found, lines = ast.parse(text), {}, [0]
    names = blocked_functions(text)
    for line in text.encode().split(b"\n"):
        lines.append(lines[-1] + len(line) + 1)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            for guard in ast.walk(node):
                if isinstance(guard, (ast.If, ast.IfExp, ast.While)):
                    test = guard.test
                    found["%s:%s" % (node.name, ast.get_source_segment(text, test))] = (lines[test.lineno - 1] + test.col_offset, lines[test.end_lineno - 1] + test.end_col_offset)
    return found


def marker_mutant_source(func, ret="None"):
    """The REAL shim's text with the retired marker honoured again at the top of `func` (after its docstring): `return` / `return None` when the variable is "1"."""
    source = SHIM.read_text()
    start = source.index("def %s(" % func)
    head = source[start:]
    at = head.index(":\n", head.index(")")) + 2
    stripped = head[at:].lstrip()
    if stripped.startswith('"""'):
        at += head[at:].index('"""') + 3 + head[at:][head[at:].index('"""') + 3:].index('"""') + 3 + 1
    guard = '    if os.environ.get("FLEET_SWITCHBOARD_OWN_GH") == "1":\n        return%s\n' % ("" if ret == "" else " " + ret)
    mutant = source[:start] + head[:at] + guard + head[at:]
    ast.parse(mutant)
    return mutant
