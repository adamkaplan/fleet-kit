#!/usr/bin/env python3
"""cloud_cost_report: USD per day of the cloud fleet, from the cloud launcher's audit lines (read-only).

    bin/cloud_cost_report.py DAY AUDIT_FILE --node-price USD_PER_HOUR --node-vcpu N (--node-hours N | --node-hours-file F)
                             [--shape S|M|L] [--pod-rate USD_PER_HOUR]

DAY is YYYY-MM-DD (UTC). AUDIT_FILE holds JSON lines as in docs/cloud-worker-interface.md section 4. Node inputs are
arguments or a file of lines "HOUR_UTC COUNT" (one line per hour, e.g. 2026-10-14T05 3), never read from Azure.
USD per day = node-hours x node price. Each pod is also priced two ways, printed side by side: the launcher's own
list-price estimate (a stopped pod: its stop line; an open pod: --pod-rate, default 0) and its vCPU share of the node
price (pod vCPU / node vCPU x node price x pod-hours; shapes S 2, M 4, L 6 vCPU, --shape applies to all pods because the
audit does not record the shape). A pod created on the day and not stopped on it runs to the end of the day, and the
report says so. Actions the report does not cost (create.interrupted, create.failed, create-attempt, renew-git-token) are
skipped and counted; any other unknown action is malformed. Lines with ok false are listed, not costed.
Exit 0 on a report, 2 on a malformed line or file. Every float argument must be finite (nan and inf are refused). An audit
file over 64 MB is refused with a one-line message (it is read with a bounded read, never whole).

Known limits (behaviour left as it is):
- a pod spanning midnight is costed only on its stop day (its hours start at the start of that day);
- the shape applies to all pods, because the audit does not record it;
- two creates of one name resolve to the last one;
- a clock going backwards is hidden by max(0, ...).
"""
import argparse
import datetime
import json
import math
import re
import sys

ACTIONS = ("create", "stop", "extend", "delete")
SKIPPED = ("create.interrupted", "create.failed", "create-attempt", "renew-git-token")
MAX_AUDIT_BYTES = 64 * 1024 * 1024
SHAPE_VCPU = {"S": 2, "M": 4, "L": 6}


class Malformed(Exception):
    pass


def parse_ts(text, where):
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        raise Malformed("%s: bad ts %r" % (where, text))


def read_lines(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = handle.read(MAX_AUDIT_BYTES + 1)
        raw = data.splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise Malformed("cannot read %s: %s" % (path, exc.__class__.__name__))
    if len(data) > MAX_AUDIT_BYTES:
        raise Malformed("audit file is over %d MB" % (MAX_AUDIT_BYTES // (1024 * 1024)))
    lines = []
    for number, text in enumerate(raw, 1):
        if not text.strip():
            continue
        where = "line %d" % number
        try:
            line = json.loads(text)
        except ValueError:
            raise Malformed("%s: not JSON" % where)
        if not isinstance(line, dict):
            raise Malformed("%s: not an object" % where)
        for key in ("ts", "worker", "runtime_id", "action", "cost_estimate_usd", "ok"):
            if key not in line:
                raise Malformed("%s: missing %s" % (where, key))
        if line["action"] not in ACTIONS and line["action"] not in SKIPPED:
            raise Malformed("%s: bad action %r" % (where, line["action"]))
        cost = line["cost_estimate_usd"]
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
            raise Malformed("%s: bad cost_estimate_usd" % where)
        if not isinstance(line["ok"], bool):
            raise Malformed("%s: bad ok" % where)
        if not isinstance(line["worker"], str) or not isinstance(line["runtime_id"], str):
            raise Malformed("%s: bad worker or runtime_id" % where)
        line["_ts"] = parse_ts(line["ts"], where)
        line["_n"] = number
        lines.append(line)
    return lines


def read_hours_file(path, day):
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise Malformed("cannot read %s: %s" % (path, exc.__class__.__name__))
    seen = set()
    total = 0.0
    for number, text in enumerate(raw, 1):
        if not text.strip():
            continue
        parts = text.split()
        match = re.fullmatch(r"(\d{4}-\d\d-\d\d)T(\d\d)(?::00(?::00)?Z?)?", parts[0]) if len(parts) == 2 else None
        try:
            count = float(parts[1]) if match else -1
        except ValueError:
            count = -1
        if not match or count < 0 or count != count or count == float("inf") or int(match.group(2)) > 23:
            raise Malformed("hours file line %d: want HOUR_UTC COUNT" % number)
        if match.group(1) != day:
            raise Malformed("hours file line %d: hour is not on %s" % (number, day))
        if parts[0][:13] in seen:
            raise Malformed("hours file line %d: hour given twice" % number)
        seen.add(parts[0][:13])
        total += count
    return total


def build(day, lines, node_price, node_vcpu, node_hours, pod_rate, shape="S"):
    start = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)
    today = [l for l in lines if start <= l["_ts"] < end]
    skipped = [l for l in today if l["action"] in SKIPPED]
    today = [l for l in today if l["action"] not in SKIPPED]
    failed = [l for l in today if not l["ok"]]
    good = [l for l in today if l["ok"]]
    created = {}
    for l in lines:  # creates before the day still give a stop its hours
        if l["ok"] and l["action"] == "create" and l["_ts"] < end:
            created[l["runtime_id"]] = l["_ts"]
    stopped = {}
    for l in sorted(good, key=lambda x: x["_ts"]):
        if l["action"] == "stop":
            stopped[l["runtime_id"]] = l
    pods = []
    for rid, l in stopped.items():
        hours = 0.0
        if rid in created:
            hours = max(0.0, (l["_ts"] - max(created[rid], start)).total_seconds() / 3600.0)
        pods.append((rid, l["cost_estimate_usd"], hours, False))
    for l in good:
        if l["action"] == "create" and l["runtime_id"] not in stopped:
            hours = (end - l["_ts"]).total_seconds() / 3600.0
            pods.append((l["runtime_id"], pod_rate * hours, hours, True))
    pod_usd = sum(p[1] for p in pods)
    pod_hours = sum(p[2] for p in pods)
    share_usd = SHAPE_VCPU[shape] / node_vcpu * node_price * pod_hours
    node_usd = node_price * node_hours
    return {"pods": pods, "failed": failed, "pod_usd": pod_usd, "pod_hours": pod_hours, "share_usd": share_usd,
            "shape": shape, "node_usd": node_usd, "node_hours": node_hours, "total": node_usd,
            "n": len(today), "skipped": len(skipped)}


def render(day, r, out):
    if r["n"] == 0:
        out.write("cloud cost %s: USD 0 (no audit lines on this day)\n" % day)
        out.write("skipped lines: %d\n" % r["skipped"])
        return
    out.write("cloud cost %s: USD %.2f per day\n" % (day, r["total"]))
    out.write("pods: %d, pod-hours %.2f\n" % (len(r["pods"]), r["pod_hours"]))
    out.write("pod USD, launcher estimate %.2f; vCPU share of node price (shape %s) %.2f\n" % (r["pod_usd"], r["shape"], r["share_usd"]))
    out.write("nodes: node-hours %.2f, node USD %.2f\n" % (r["node_hours"], r["node_usd"]))
    out.write("skipped lines: %d\n" % r["skipped"])
    for rid, usd, hours, open_ in r["pods"]:
        if open_:
            out.write("open pod %s: no stop on this day, costed to the end of the day (%.2f h, USD %.2f)\n" % (rid, hours, usd))
    for l in r["failed"]:
        out.write("not costed (ok false) line %d: %s %s %s\n" % (l["_n"], l["action"], l["worker"], l["runtime_id"]))


def main(argv, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="cloud_cost_report", add_help=True)
    parser.add_argument("day")
    parser.add_argument("audit")
    parser.add_argument("--node-price", type=float, required=True)
    parser.add_argument("--node-vcpu", type=float, required=True)
    parser.add_argument("--node-hours", type=float)
    parser.add_argument("--node-hours-file")
    parser.add_argument("--shape", choices=sorted(SHAPE_VCPU), default="S")
    parser.add_argument("--pod-rate", type=float, default=0.0)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    try:
        try:
            datetime.datetime.strptime(args.day, "%Y-%m-%d")
        except ValueError:
            raise Malformed("bad day %r (want YYYY-MM-DD)" % args.day)
        if (args.node_hours is None) == (args.node_hours_file is None):
            raise Malformed("give exactly one of --node-hours and --node-hours-file")
        if args.node_hours_file is not None:
            args.node_hours = read_hours_file(args.node_hours_file, args.day)
        for value in (args.node_price, args.node_vcpu, args.node_hours, args.pod_rate):
            if not math.isfinite(value):
                raise Malformed("node or pod input is not a finite number")
        if min(args.node_price, args.node_hours, args.pod_rate) < 0 or args.node_vcpu <= 0:
            raise Malformed("negative node or pod input, or node vCPU not above 0")
        lines = read_lines(args.audit)
    except Malformed as exc:
        sys.stderr.write("cloud_cost_report: %s\n" % exc)
        return 2
    render(args.day, build(args.day, lines, args.node_price, args.node_vcpu, args.node_hours, args.pod_rate, args.shape), out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
