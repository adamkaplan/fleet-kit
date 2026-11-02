"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): scenario_and_delivery."""
import calendar
import json
from support.paths import BIN, REPO
from support.delivery_measure import REPO
from support.fake_hubs import LEDGER_PLAN
from support.scenario_roles import JEV_REPLAY


JEV_SCENARIO_CONFIG = {"jev": {"replay": str(JEV_REPLAY)}}  # the hand-written answers every scenario runs against


DELIVERY_FIXTURE = REPO / "tests" / "switchboard" / "fixtures" / "delivery_measure.json"


IDLE_CASES = ("idle_after_normal_turn", "idle_after_provider_error")


def delivery_fixture():
    return json.loads(DELIVERY_FIXTURE.read_text())


def _toasts(world):
    shown = []
    for argv in world.herdr.calls:
        if argv[1:3] == ["notification", "show"]:
            shown.append({"title": argv[3], "body": argv[argv.index("--body") + 1] if "--body" in argv else ""})
    return shown


def _delivered_texts(run, name):
    return [a["text"] for a in run.world.admissions(name) if a["delivered_at"] is not None]


DECISIONS_HARNESS = r"""
import * as d from "%(url)s";
import { mkdtempSync, writeFileSync, utimesSync, renameSync, rmSync } from "node:fs";
import { join } from "node:path";

const dir = mkdtempSync(join(%(tmp)s, "dec-"));
const now = Date.now();
const results = {};
const iso = (ms) => new Date(ms).toISOString();
const put = (body, ageMs = 0) => {
  const temp = join(dir, "decisions.json.tmp");
  writeFileSync(temp, typeof body === "string" ? body : JSON.stringify(body));
  utimesSync(temp, (now - ageMs) / 1000, (now - ageMs) / 1000);
  renameSync(temp, join(dir, "decisions.json"));
};
const list = [
  { id: "#2ab", title: "Close #2? and a second deploy run?", ask: 2, agent: "platform", kind: "report", since: iso(now - 12 * 60000) },
  { id: "platform-481", title: "permission: shell " + "x".repeat(200), ask: null, agent: "platform", kind: "permission", since: iso(now - 45000) },
];

results.unset = [d.stateDirOf({}), d.stateDirOf({ FLEET_SWITCHBOARD_STATE: "" }), d.stateDirOf({ FLEET_SWITCHBOARD_STATE: "/s" })];
results.stateDefaults = [
  d.stateDirOf({ XDG_STATE_HOME: "/x" }),
  d.stateDirOf({ HOME: "/h" }),
  d.stateDirOf({ FLEET_SWITCHBOARD_STATE: "/s", HOME: "/h" }),
  d.stateDirOf({ XDG_STATE_HOME: "rel", HOME: "/h" }),
  d.stateDirOf({ XDG_STATE_HOME: "rel" }),
];
results.noDir = d.loadSnapshot(null, now).status;
results.missing = d.loadSnapshot(dir, now).status;
put("{not json");
results.garbage = d.loadSnapshot(dir, now).status;
put({ nothing: true });
results.wrongShape = d.loadSnapshot(dir, now).status;
put({ decisions: list, errors: [] });
const ok = d.loadSnapshot(dir, now);
results.ok = { status: ok.status, n: ok.decisions.length };
results.view = d.buildView(ok, now);
put({ decisions: list, errors: [] }, d.STALE_MS + 1000);
results.staleStatus = d.loadSnapshot(dir, now).status;
results.staleView = d.buildView(d.loadSnapshot(dir, now), now);
put({ decisions: list, errors: [] }, d.STALE_MS - 1000);
results.edge = d.loadSnapshot(dir, now).status;
put({ decisions: [], errors: [] });
results.emptyView = d.buildView(d.loadSnapshot(dir, now), now);
put({ decisions: [], errors: [{ source: "github", error: "x" }] });
results.errorsView = d.buildView(d.loadSnapshot(dir, now), now);
put({ decisions: [{ id: 3 }, null, { id: "#1", title: "fine", since: "nonsense" }], errors: [] });
results.junkView = d.buildView(d.loadSnapshot(dir, now), now);
results.ages = [0, 59, 60, 719, 3599, 3600, 3900, 7200, 86399, 86400, 259200, -5].map((s) => d.formatAge(s));
results.wrap = {
  words: d.wrapText("aaa bbb ccc dddd", 7),
  long: d.wrapText("x" + "y".repeat(20) + " z", 8),
  whole: d.wrapText("word ".repeat(60).trim(), 34),
  capped: d.wrapText("w ".repeat(100), 4, 5),
  empty: d.wrapText("  ", 10),
};
const two = (id, repo, title, ago) => ({ id, repo, title, since: iso(now - ago * 60000) });
results.grouped = d.buildView({ status: "ok", errors: [], decisions: [
  two("web#9", "acme/web", "newer web", 5), two("api#1", "acme/api", "api only", 3),
  two("web#2", "acme/web", "older web " + "q".repeat(40), 50) ] }, now);
results.groupedLines = d.viewLines(results.grouped);
results.batchLines = d.viewLines(d.buildView({ status: "ok", errors: [], decisions: [
  { id: "api#101", repo: "acme/api", title: "Cleanup", headline: "Cleanup of stale issues (2 rows)", since: iso(now - 7200000), tier: "cos",
    rows: [{ text: "web#7: close it" }, { text: "web#9: close as duplicate of web#7 and also label the other issue" }] } ] }, now, 34,
  { kind: "all", repo: null, label: "all" }));
const cosAge = (id, mins) => ({ id, repo: "acme/api", title: id, tier: "cos", since: iso(now - mins * 60000) });
results.overTitles = d.buildView({ status: "ok", errors: [], decisions: [cosAge("a#1", 59), cosAge("a#2", 60), cosAge("a#3", 61),
  cosAge("a#4", 300), { id: "a#5", title: "x", tier: "cos", since: "junk" }] }, now, 34, { kind: "all", repo: null, label: "all" }).sections.map((x) => x.title);
results.longWait = d.LONG_WAIT_SECONDS;
results.oneLine = [d.oneLine("a\n b\t c", 20), d.oneLine("x".repeat(50), 10), d.oneLine(undefined, 5)];
results.until = [d.msUntilStale({ status: "ok", ageMs: 100000 }), d.msUntilStale({ status: "ok", ageMs: 0 }), d.msUntilStale({ status: "stale", ageMs: 1 })];
results.concerns = [d.concernsFile("decisions.json"), d.concernsFile("decisions.json.123.tmp"), d.concernsFile("audit.jsonl"), d.concernsFile(null), d.concernsFile(undefined)];
results.constants = [d.STALE_MS, d.SAFETY_MS, d.STATE_ENV, d.DECISIONS_FILE, d.NOT_UPDATING, d.WRAP_MAX_LINES];
rmSync(dir, { recursive: true });
console.log(JSON.stringify(results));
"""


VIEW_HARNESS = r"""
import * as d from "%(url)s";
const now = Date.now();
const iso = (ms) => new Date(ms).toISOString();
const list = [
  { id: "a", title: "cos one", repo: "acme/api", tier: "cos", since: iso(now - 60000) },
  { id: "b", title: "orch one", repo: "acme/api", tier: "orchestrator", since: iso(now - 60000) },
  { id: "c", title: "web cos", repo: "acme/web", tier: "cos", since: iso(now - 60000) },
  { id: "d", title: "yours", repo: "acme/web", tier: "human", since: iso(now - 60000) },
  { id: "e", title: "old file, no tier", repo: null, since: iso(now - 60000) },
];
const snap = (decisions, errors = []) => ({ status: "ok", decisions, errors, ageMs: 0 });
const ids = (view) => d.selectFor(list, view).map((x) => x.id).join("");
const results = {
  cos: d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" }),
  cosWithRepo: d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff", FLEET_SWITCHBOARD_REPO: "acme/api" }),
  orch: d.viewOf({ FLEET_SWITCHBOARD_ROLE: "orchestrator", FLEET_SWITCHBOARD_REPO: "acme/api" }),
  coderWithRepo: d.viewOf({ FLEET_SWITCHBOARD_ROLE: "coder", FLEET_SWITCHBOARD_REPO: " acme/web " }),
  orchNoRepo: d.viewOf({ FLEET_SWITCHBOARD_ROLE: "orchestrator" }),
  plain: d.viewOf({}),
  nothing: [d.viewOf(undefined), d.viewOf(null), d.viewOf({ FLEET_SWITCHBOARD_REPO: "" })],
  picked: { cos: ids(d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" })), orch: ids(d.viewOf({ FLEET_SWITCHBOARD_REPO: "acme/api" })),
            web: ids(d.viewOf({ FLEET_SWITCHBOARD_REPO: "acme/web" })), plain: ids(d.viewOf({})) },
  titles: [
    d.buildView(snap(list), now, 34, d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" })).title,
    d.buildView(snap(list), now, 34, d.viewOf({ FLEET_SWITCHBOARD_REPO: "acme/api" })).title,
    d.buildView(snap(list), now, 34, d.viewOf({})).title,
    d.buildView(snap(list), now).title,
  ],
  empty: d.buildView(snap(list.slice(0, 3)), now, 34, d.viewOf({})),
  emptyRepo: d.buildView(snap([]), now, 34, d.viewOf({ FLEET_SWITCHBOARD_REPO: "acme/api" })),
  stale: d.buildView({ status: "stale", decisions: list, errors: [], ageMs: 999999 }, now, 34, d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" })),
  unset: d.buildView({ status: "unset", decisions: [], errors: [], ageMs: null }, now, 34, d.viewOf({})),
  errors: d.buildView(snap(list, [{ source: "github", error: "x" }]), now, 34, d.viewOf({})).rows.map((r) => r.key),
  envNames: [d.ROLE_ENV, d.REPO_ENV],
  cosGroups: d.buildView(snap(list), now, 34, d.viewOf({ FLEET_SWITCHBOARD_ROLE: "chief-of-staff" })).groups.map((g) => [g.repo, g.entries.map((e) => e.key)]),
  orchGroups: d.buildView(snap(list), now, 34, d.viewOf({ FLEET_SWITCHBOARD_REPO: "acme/api" })).groups.map((g) => [g.repo, g.entries.map((e) => e.key)]),
  humanGroups: d.buildView(snap(list), now, 34, d.viewOf({})).groups.map((g) => [g.repo, g.entries.map((e) => e.key)]),
};
console.log(JSON.stringify(results));
"""


# ======================================================================
# #583: a message says when it was sent and delivered; a newer send replaces older waiting ones (marked, kept); the pane warning is
# only for a real mismatch; a launch goes to the caller's own workspace.
# ======================================================================
def _clock_at(hour, minute, second=0):
    return calendar.timegm((2027, 1, 15, hour, minute, second))


WORK_ITEM_PLAN = LEDGER_PLAN.split("```yaml", 1)[0].split("## Intent", 1)[0] + "```yaml" + LEDGER_PLAN.split("```yaml", 1)[1]
