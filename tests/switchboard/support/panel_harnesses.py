"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): panel_harnesses."""
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


HARNESS_CASES = r"""
await run("tighten-allow-to-ask", base("allow"), { reply: reply("ask", "Needs your yes") });
await run("tighten-allow-to-deny", base("allow"), { reply: reply("deny", "Denied") });
await run("tighten-ask-to-deny", base("ask"), { reply: reply("deny", "Denied") });
await run("never-loosen-ask-to-allow", base("ask"), { reply: reply("allow", "fine") });
await run("never-loosen-deny-to-ask", base("deny"), { reply: reply("ask", "fine") });
await run("same-ask-changes-nothing", base("ask"), { reply: reply("ask", "same") });
await run("timeout-keeps-configured", base("allow"), { reply: undefined }, 80);
await run("garbage-keeps-configured", base("allow"), { reply: "not json at all" });
await run("bad-outcome-keeps-configured", base("allow"), { reply: JSON.stringify({ outcome: "permit", reason: "x" }) });
await run("no-line-keeps-configured", base("allow"), { reply: null });
await run("spawn-error-keeps-configured", base("allow"), { error: true });
await run("spawn-throws-keeps-configured", base("allow"), { throws: true });
await run("bad-input-spawns-nothing", { action: "shell", effect: "allow" }, { reply: reply("deny") });
const spawnedBefore = spawns.length;
await run("unlisted-action-spawns-nothing", { sessionID: "ses_abc", action: "read", resources: ["a"], effect: "allow" }, { reply: reply("deny") });
results["unlisted-spawned"] = spawns.length - spawnedBefore;
results["request"] = hooks.requestOf({ sessionID: "ses_q", action: "shell", resources: ["a", 5, "b"], effect: "ask" });
results["tools"] = ["shell", "bash", "edit", "write", "webfetch", "task", "subagent", "read", "glob", "grep", "skill", "mcp_x", "toString", "constructor", undefined]
  .map((a) => hooks.toolOf(a));
results["stricter"] = [["ask","allow"],["allow","ask"],["ask","deny"],["ask","ask"],["ask","bogus"]]
  .map(([r, c]) => hooks.isStricter({ outcome: r }, c));
results["settings"] = [
  hooks.settingsOf({ FLEET_SWITCHBOARD_BIN: "fleet-switchboard" }),
  hooks.settingsOf({}),
  hooks.settingsOf({ FLEET_SWITCHBOARD_BIN: "/abs/x", FLEET_HOOKS_TIMEOUT_MS: "250" }),
  hooks.settingsOf({ FLEET_SWITCHBOARD_BIN: "/abs/x", FLEET_HOOKS_TIMEOUT_MS: "nope" }),
];
process.env.OPENROUTER_API_KEY = "canary-in-service-env";
process.env.FLEET_SWITCHBOARD_LAB = "1";
await run("env-probe", base("allow"), { reply: reply("allow") });
const probe = spawns[spawns.length - 1];
results["child-env"] = { keys: Object.keys(probe.opts.env).sort(), hasCanary: JSON.stringify(probe.opts.env).includes("canary") };
const first = spawns[0];
results["spawn-shape"] = { bin: first.bin, args: first.args, shell: first.opts.shell, stdio: first.opts.stdio,
                           stdin: JSON.parse(first.stdin) };
const registered = [];
await plugin.setup({ permission: { hook: (name, fn) => registered.push([name, typeof fn]) } }, { FLEET_SWITCHBOARD_BIN: "/abs/x" });
const unregistered = [];
await plugin.setup({ permission: { hook: (name) => unregistered.push(name) } }, {});
await plugin.setup({ permission: { hook: (name) => unregistered.push(name) } }, { FLEET_SWITCHBOARD_BIN: "relative/x" });
results["registration"] = { registered, unregistered, id: plugin.id, setupIsFunction: typeof plugin.setup };
"""


LAYOUT_HARNESS = r"""
import * as d from "%(url)s";
const now = Date.now();
const iso = (ms) => new Date(ms).toISOString();
const mk = (id, repo, tier, headline, mins, extra = {}) =>
  ({ id, repo, tier, title: "RAW " + headline, headline, since: iso(now - mins * 60000), ...extra });
const list = [
  mk("web#5", "acme/web", "cos", "web cos question", 40),
  mk("api#2", "acme/api", "human", "api for you " + "long ".repeat(30), 90),
  mk("web#7", "acme/web", "human", "web for you", 10, { reported_by: ["coder"] }),
  mk("web#8", "acme/web", "human", "second for you", 5),
  mk("site#1", "acme/site", "orchestrator", "orch one", 3),
  { id: "old#1", repo: "acme/old", title: "only a title", since: iso(now - 60000) },
];
const snap = { status: "ok", decisions: list, errors: [], ageMs: 0 };
const cos = d.buildView(snap, now, 34, d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" }));
const human = d.buildView(snap, now, 34, d.viewOf({}));
console.log(JSON.stringify({
  cosTitles: cos.sections.map((s) => s.title),
  cosKeys: cos.sections.map((s) => s.groups.map((g) => [g.repo, g.entries.map((e) => e.key)])),
  humanTitles: human.sections.map((s) => s.title),
  humanKeys: human.sections.map((s) => s.groups.map((g) => [g.repo, g.entries.map((e) => e.key)])),
  lines: d.viewLines(cos),
  humanLines: d.viewLines(human),
  entry: d.entryOf(list[1], now),
  via: d.entryOf(list[2], now).head,
  noHeadline: d.entryOf(list[5], now),
}));
"""


NOTICE_HARNESS = r"""
import * as d from "%(url)s";
const now = Date.parse("2026-03-04T12:00:00Z");
const iso = (ms) => new Date(ms).toISOString();
const mk = (id, repo, tier, headline, mins, extra = {}) =>
  ({ id, repo, tier, kind: "issue", title: "RAW " + headline, headline, since: iso(now - mins * 60000), ...extra });
const note = (id, repo, headline, mins, extra = {}) =>
  ({ id, repo, tier: "human", kind: "notice", title: headline, headline, since: iso(now - mins * 60000), state: "open",
     additions: 120, deletions: 14, files: 5, ci: "green", review: "approved", stale: false, merge_after: null, ...extra });
const list = [
  mk("web#5", "acme/web", "cos", "web cos question", 40),
  mk("api#2", "acme/api", "human", "api needs your decision", 90),
  note("pr:web#9", "acme/web", "web#9 Add the retry budget to the client", 12, { ci: "failing", review: "none" }),
  note("pr:api#12", "acme/api", "api#12 Parser rewrite " + "long ".repeat(30), 3, { merge_after: "2026-03-04T12:10:00Z" }),
  note("pr:api#11", "acme/api", "api#11 Docs", 30, { files: 1, ci: "pending", review: "changes requested", stale: true }),
];
const snap = { status: "ok", decisions: list, errors: [], ageMs: 0 };
const cos = d.buildView(snap, now, 34, d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" }));
const human = d.buildView(snap, now, 34, d.viewOf({}));
const repo = d.buildView(snap, now, 34, d.viewOf({ FLEET_SWITCHBOARD_REPO: "acme/web" }));
const onlyNotice = d.buildView({ status: "ok", decisions: [list[2]], errors: [], ageMs: 0 }, now, 34, d.viewOf({}));
const keys = (v) => v.sections.map((s) => [s.title, s.groups.map((g) => [g.repo, g.entries.map((e) => e.key)])]);
console.log(JSON.stringify({
  cosTitle: cos.title, humanTitle: human.title, cos: keys(cos), human: keys(human), repo: keys(repo), repoTitle: repo.title,
  onlyNotice: d.viewLines(onlyNotice), humanLines: d.viewLines(human), cosLines: d.viewLines(cos),
  entries: Object.fromEntries(human.sections[1].groups.flatMap((g) => g.entries.map((e) => [e.key, e]))),
  noFacts: d.entryOf(note("pr:x#1", "a/x", "x#1 T", 1, { additions: null, deletions: null, files: null, ci: null, review: null }), now),
}));
"""


CAPTAIN_ROW_HARNESS = r"""
import * as d from "%(url)s";
const now = Date.parse("2026-01-10T12:00:00Z");
const iso = (secs) => new Date(now - secs * 1000).toISOString();
const issue = { id: "#41", kind: "issue", tier: "human", ask: 41, repo: "acme-org/widget-host", title: "Which database?", headline: "Which database?", since: iso(86400) };
const captain = (extra = {}) => ({ id: "pr:acme-org/widget-host#132", kind: "captain", tier: "human", ask: null, repo: "acme-org/widget-host",
  number: 132, title: "Fix the wake text", headline: "widget-host#132 Fix the wake text", since: iso(86400), ci: "green", review: "approved", ...extra });
const snap = (decisions) => ({ status: "ok", decisions, errors: [] });
const view = { kind: "human" };
const out = {};
const show = (list, opts) => { const rows = d.styledRows(snap(list), now, 34, view, opts); return { text: d.styledText(rows, 34), links: rows.filter((r) => r.url).map((r) => r.url), ids: rows.filter((r) => r.id).map((r) => r.id) }; };
out.plain = show([issue, captain()]);
out.v3 = show([issue, captain()], { v3: true });
out.entry = d.buildView(snap([issue, captain()]), now, 34, view).groups.flatMap((g) => g.entries.map((e) => [e.head, e.url || null]));
out.urls = [captain(), captain({ number: "0" }), captain({ number: -3 }), captain({ number: 1.5 }), captain({ repo: "bad repo" }),
  captain({ repo: "../x" }), captain({ repo: "a/b?q=1" }), captain({ number: "12; rm" }), captain({ number: null })].map((x) => d.urlOf(x));
out.safe = [d.safeUrl(d.urlOf(captain())), d.safeUrl("https://github.com/acme-org/widget-host/pull/132?x=1")];
out.idOf = [d.idOf(captain()), d.idOf(captain(), () => true), d.idOf(captain({ repo: "bad repo" })), d.idOf(captain({ number: null })), d.idOf(issue),
  d.idOf({ id: "pr:o/r#7", kind: "notice", repo: "o/r", number: 7 })];
out.title = d.styledText(d.styledRows(snap([captain({ headline: "x\u001b]8;;http://evil\u0007y" })]), now, 34, view), 34).join("|");
console.log(JSON.stringify(out));
"""


STYLED_UNIT_HARNESS = r"""
import * as d from "%(url)s";
const now = Date.parse("2026-10-08T12:00:00Z");
const iso = (secs) => new Date(now - secs * 1000).toISOString();
const mk = (id, repo, tier, headline, secs, extra = {}) => ({ id, repo, tier, title: "RAW " + headline, headline, since: iso(secs), ask: 1, ...extra });
const snap = (decisions, errors = []) => ({ status: "ok", decisions, errors, ageMs: 0 });
const chief = d.viewOf({ [d.ROLE_ENV]: "chief-of-staff" });
const human = d.viewOf({});
const rows = (decisions, view, errors) => d.styledRows(snap(decisions, errors), now, 34, view);
const text = (decisions, view, errors) => d.styledText(rows(decisions, view, errors), 34);
const pr = (number, repo, secs, extra = {}) => ({ id: "pr:" + repo + "#" + number, kind: "notice", tier: "human", repo, number, title: "x",
  headline: repo + "#" + number + " Add a thing", since: iso(secs), ci: "green", review: "approved", additions: 1, deletions: 2, files: 3, ...extra });
const textOf = (l) => l.segs.map((s) => s[0]).join("") + (l.right ? "|" + l.right.map((s) => s[0]).join("") : "");
const r = {};
r.mode = [d.panelModeOf({}), d.panelModeOf({ [d.PANEL_ENV]: "plain" }), d.panelModeOf({ [d.PANEL_ENV]: " PLAIN " }), d.panelModeOf({ [d.PANEL_ENV]: "styled" }), d.panelModeOf({ [d.PANEL_ENV]: "c" }), d.panelModeOf(null)];
r.env = d.PANEL_ENV;
// the layout
const mix = [mk("a#1", "o/a", "human", "Which database?", 600), mk("a#2", "o/a", "human", "Pick a cache", 1200), mk("b#1", "o/b", "cos", "c1", 3601),
  mk("b#2", "o/b", "cos", "c2", 100), mk("b#3", "o/b", "cos", "c3", 90000), pr(5, "o/a", 100), pr(6, "o/a", 4000, { ci: "failing", review: "none" }),
  pr(7, "o/c", 90000, { ci: "pending", review: "none", additions: 900, deletions: 100 })];
r.chief = text(mix, chief);
r.human = text(mix, human);
r.none = text([], human);
r.onlyCos = text([mk("c#1", "o/c", "cos", "q", 100)], human);
r.onlyPr = text([pr(5, "o/a", 100)], human);
r.stale = d.styledText(d.styledRows({ status: "stale", decisions: [], errors: [] }, now));
r.errors = text([], human, [{ source: "x" }]).slice(-1);
// ages: muted, error-tinted... from a day
r.tint = [0, 59, 3599, 3600, 86399, 86400, 999999].map((s) => d.ageStyle(s));
r.tintConst = [d.AGE_WARN_SECONDS, d.AGE_ERROR_SECONDS];
const lineWith = (decisions, view, pick) => rows(decisions, view).find(pick);
r.ageSegs = [30, 3599, 86399, 86400].map((s) => lineWith([mk("web#1", "o/web", "human", "x", s)], human, (l) => l.id === "web#1").right);
r.ageLine = textOf(lineWith([mk("web#1", "o/web", "human", "x", 300)], human, (l) => l.id === "web#1"));
// PR number coloured by state; the real id is kept on the line
const prLine = (extra) => lineWith([pr(5, "o/a", 100, extra)], human, (l) => l.id);
r.prState = [{ ci: "failing", review: "none" }, { ci: "green", review: "approved" }, { ci: "green", review: "none" }, { ci: "pending", review: "none" }].map((e) => prLine(e).segs.map((s) => [s[0].split(" ")[0], s[1]])[1]);
r.prId = [prLine({}).id, textOf(prLine({}))];
// a decision that needs you: two lines, bold headline, id muted
r.need = rows([mk("a#1", "o/a", "human", "Which database?", 600)], human).slice(-3).map((l) => [textOf(l), l.segs.map((s) => s[1])]);
// ambiguity and refs
const clash = [mk("web#1", "acme/web", "human", "see acme/web#9 and other/web#4 and web#3", 60), mk("web#2", "other/web", "human", "x", 60)];
r.clash = text(clash, human);
r.tidy = [d.tidyRefs("fix acme/web#9, api#4.", "acme/api", d.repoAmbiguity([{ repo: "acme/web" }, { repo: "acme/api" }])), d.shortRepo("acme/web"), String(d.repoAmbiguity([{ repo: "a/web" }, { repo: "b/web" }]).ambiguous("a/web"))];
// theme
const theme = { text: { muted: "M", feedback: { warning: { base: "W" }, error: { base: "E" }, success: { base: "S" }, info: { base: "I" } } } };
r.theme = [d.themeColors(theme), d.themeColors(undefined), d.themeColors({}), d.themeColors({ get text() { throw new Error("boom"); } })];
// the plain view is untouched, same order of decisions
r.plainFirst = d.viewLines(d.buildView(snap(mix), now, 34, chief)).slice(0, 3);
// links and control characters
const linked = rows([mk("web#1", "acme/web", "human", "Q?", 60, { ask: 12 }), pr(5, "acme/web", 60), mk("x#1", "bad repo", "human", "H", 60, { ask: 3 })], human);
r.links = linked.filter((l) => l.url).map((l) => [textOf(l), l.url, !!l.id]);
r.noLink = linked.filter((l) => !l.url).map((l) => textOf(l)).filter((t) => /bad repo|^\u2754 H$/.test(t));
const esc = rows([mk("e#1", "acme/e", "human", "bad\u001b]8;;http://x\u0007title", 60, { ask: 1, rows: [{ text: "r\u001b[31mow" }] })], human);
r.ctrl = esc.map((l) => textOf(l)).filter((t) => /title|31m/.test(t));
// iteration 3
const v3 = (list, view) => d.styledRows(snap(list), now, 34, view, { v3: true });
r.v3 = {
  hue: [d.projectHue("acme/web"), d.projectHue("acme/web"), d.projectHue("acme/api"), d.projectHue("")],
  hues: [...new Set(["a/1", "b/2", "c/3", "d/4", "e/5", "f/6", "g/7", "h/8"].map((x) => d.projectHue(x)))].sort(),
  glyphs: Object.entries(d.GLYPH).map(([k, g]) => [k, [...g].length, g.codePointAt(0) < 0x10000 && g.length === 1]),
  chief: d.styledText(v3(mix, chief), 34),
  styles: v3(mix, human).filter((l) => l.id || /^(a|c)$/.test(textOf(l))).map((l) => [textOf(l), l.segs.map((s) => s[1]), (l.right || []).map((s) => s[1])]),
  size: v3([pr(5, "o/a", 100, { additions: 900, deletions: 100 })], human).slice(-2).map((l) => [textOf(l), l.segs.map((s) => s[1])]),
  gap: v3(mix, human).slice(2, 6).map((l) => textOf(l)),
  plainC: d.styledText(d.styledRows(snap(mix), now, 34, chief), 34).join("\n") === r.chief.join("\n"),
  theme: d.themeColors({ syntax: { keyword: "K", function: "F", operator: "O", type: "T" }, diff: { text: { added: "A", removed: "R" } } }),
  themeNone: d.themeColors({ syntax: {} }),
};
// search links and kind marks
const env = { USER: "tester" };
const withEnv = (list, view) => d.styledRows(snap(list), now, 34, view, { v3: true, env });
const cosList = [mk("a#1", "acme/a", "cos", "q1", 100, { kind: "issue" }), mk("a#2", "acme/a", "cos", "q2", 100, { kind: "issue" }), mk("a#3r", "acme/a", "cos", "r", 100, { kind: "report" }),
  mk("b#9r", "acme/b", "cos", "only a report", 100, { kind: "report" }), mk("c#1", "other/c", "cos", "q", 100, { kind: "issue" }), pr(5, "acme/a", 10), pr(6, "other/c", 10)];
const sumRows = withEnv(cosList, chief).slice(1, 3);
r.links2 = {
  prs: d.searchUrl("pr", ["acme/a"]), prsMany: d.searchUrl("pr", ["other/c", "acme/a", "acme/a"]),
  cos: d.searchUrl("label", ["acme/a"], "tester:awaiting-cos"), cosMany: d.searchUrl("label", ["acme/a", "other/c"], "tester:awaiting-user"),
  hostile: [d.searchUrl("label", ["acme/a"], 'x" repo:evil/z'), d.searchUrl("label", ["acme/a"], "a:awaiting-cos&x=1"), d.searchUrl("pr", ["a b/c"]), d.searchUrl("pr", ["../x"]), d.searchUrl("pr", []), d.searchUrl("pr", [5, null, {}])],
  safe: [d.safeUrl("https://github.com/a/b/issues?q=is%%3Aopen"), d.safeUrl("https://github.com/a/b/issues?q=x&y=1"), d.safeUrl("https://github.com/a/b/issues?q=x\u001b"), d.safeUrl("https://github.com/search?q=x%%2By&type=issues"),
    d.safeUrl("https://github.com/search?q=x&type=code"), d.safeUrl("https://evil.com/a/b/issues?q=x"), d.safeUrl("https://github.com/a/b/pulls?q=x y"), d.safeUrl("https://github.com/a/b/pulls?q=x#h")],
  user: [d.labelUserOf({ USER: "tester" }), d.labelUserOf({ LOGNAME: "x.y" }), d.labelUserOf({ USER: "a b" }), d.labelUserOf({ USER: "x:y" }), d.labelUserOf({}), d.labelUserOf(null)],
  summary: sumRows.flatMap((l) => l.segs).map((x) => [x[0], x[2] ?? null]),
  fold: withEnv(cosList, chief).filter((l) => l.right && /oldest/.test(textOf(l))).map((l) => [textOf(l), l.segs[0][2] ?? null]),
  noEnv: d.styledRows(snap(cosList), now, 34, chief, { v3: true }).slice(1, 3).flatMap((l) => l.segs).map((x) => x[2] ?? null),
  marks: [{ kind: "issue" }, { kind: "question" }, { kind: "report" }, { kind: "permission" }, {}].map((e) => d.kindGlyph(e)).concat([d.GLYPH.pr]),
};
console.log(JSON.stringify(r));
"""


LINK_HARNESS = r"""
import * as d from "%(url)s";
const now = Date.parse("2026-03-04T12:00:00Z");
const iso = (ms) => new Date(ms).toISOString();
const dec = (id, extra) => ({ id, tier: "human", kind: "issue", title: "t " + id, headline: "t " + id, since: iso(now - 60000), ...extra });
const list = [
  dec("web#5", { repo: "acme/web", ask: 5 }),
  dec("rep#6", { repo: "acme/web", ask: "6", kind: "report" }),
  dec("batch#7", { repo: "acme/api", ask: 7, rows: [{ text: "a: b" }] }),
  dec("pr:web#9", { repo: "acme/web", kind: "notice", number: 9, state: "open" }),
  dec("untagged", { kind: "report" }),
  dec("norepo", { ask: 3 }),
  dec("nonum", { repo: "acme/web", ask: null }),
  dec("nonumnotice", { repo: "acme/web", kind: "notice" }),
];
const bad = ["acme/web/../x", "../web", "a b/c", "acme/web\x1b", "acme/web#1", "/web", "acme/", "acme//web", "https://evil/x", "acme/web?q=1", ".", "a/.."];
const evil = "\x1b]8;;https://evil.example\x07click\x1b]8;;\x07 \x9b31m \x1b\\";
const snap = { status: "ok", decisions: list, errors: [], ageMs: 0 };
const view = d.buildView(snap, now, 34, d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" }));
const plain = d.viewLines(view);
const entries = view.sections.flatMap((s) => s.groups.flatMap((g) => g.entries));
const withEvilTitle = d.entryOf(dec("web#5", { repo: "acme/web", ask: 5, title: evil, headline: evil }), now);
console.log(JSON.stringify({
  urls: Object.fromEntries(list.map((x) => [x.id, d.urlOf(x)])),
  badRepos: bad.map((r) => d.urlOf({ id: "x", repo: r, ask: 1, title: "t" })),
  badNumbers: [0, -1, 1.5, "1x", "01", "", null, undefined, "1 ", 1e12, [1], { n: 1 }].map((n) => d.urlOf({ id: "x", repo: "acme/web", ask: n, title: "t" })),
  badNoticeNumbers: [0, "9/../x", null].map((n) => d.urlOf({ id: "x", repo: "acme/web", kind: "notice", number: n, title: "t" })),
  entryUrls: Object.fromEntries(entries.map((e) => [e.key, "url" in e ? e.url : "ABSENT"])),
  plain,
  stripped: d.stripControls(evil),
  safe: [d.safeUrl("https://github.com/acme/web/issues/5"), d.safeUrl("https://github.com/acme/web/pull/9"),
         d.safeUrl("https://github.com/acme/web/issues/5\x07"), d.safeUrl("https://github.com/acme/web/issues/5\x1b]8;;"),
         d.safeUrl("http://github.com/acme/web/issues/5"), d.safeUrl("https://evil.example/acme/web/issues/5"),
         d.safeUrl("https://github.com/acme/web/issues/5 "), d.safeUrl(null), d.safeUrl(5)],
  evilEntry: withEvilTitle,
  widths: plain.map((l) => l.length),
}));
"""


ORDER_PANEL_HARNESS = r"""
import * as d from "%(url)s";
const nowMs = Date.parse("2027-01-02T00:00:00Z");
const entry = (id, n, more = {}) => ({ id, kind: "issue", repo: "acme/api", ask: n, agent: "coder", since: "2027-01-01T0" + n + ":00:00Z", tier: "human", title: "Item " + n, headline: "Item " + n, ...more });
const snap = (decisions) => ({ status: "ok", errors: [], decisions, ageMs: 0 });
const plainList = [entry("#1", 1), entry("#2", 2)];
const ordered = [entry("#1", 1, { waits_for: ["acme/api#2"], order: 1, depth: 1, blocked_by: "#2" }), entry("#2", 2, { order: 0, depth: 0, mark: "next" }),
                 entry("#3", 3, { order: 2, depth: 0 })];
const cyc = [entry("#1", 1, { waits_for: ["acme/api#2"], order: 0, depth: 0, mark: "cycle" }), entry("#2", 2, { waits_for: ["acme/api#1"], order: 1, depth: 0, mark: "cycle" })];
const text = (list, v3) => d.styledText(d.styledRows(snap(list), nowMs, 34, d.HUMAN_VIEW, { v3, env: {} }), 34);
console.log(JSON.stringify({
  plain: d.viewLines(d.buildView(snap(ordered), nowMs)),
  styled: text(ordered, true),
  styledV2: text(ordered, false),
  cycle: text(cyc, true),
  noOrderPlain: d.viewLines(d.buildView(snap(plainList), nowMs)),
  noOrderStyled: text(plainList, true),
  notes: [d.orderNote({ mark: "next" }), d.orderNote({ mark: "cycle" }), d.orderNote({ blocked_by: "#2" }), d.orderNote({}), d.depthOf({ depth: 9 }), d.depthOf({ depth: "x" }), d.depthOf({})],
  hostile: d.orderNote({ blocked_by: "#2\u001b[31m" }),
}));
"""
