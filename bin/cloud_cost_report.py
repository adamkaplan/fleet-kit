#!/usr/bin/env python3
"""cloud_cost_report: USD per day of the cloud fleet, from the cloud launcher's audit lines (read-only).

    bin/cloud_cost_report.py DAY AUDIT_FILE --node-price USD_PER_HOUR --node-count N --node-hours H [--pod-rate USD_PER_HOUR]

DAY is YYYY-MM-DD (UTC). AUDIT_FILE holds JSON lines as in docs/cloud-worker-interface.md section 4. Node inputs are
arguments, never read from Azure. A stopped pod costs what its stop line says; a pod created on the day and not stopped
on it is costed to the end of the day at --pod-rate (default 0) and the report says so. Lines with ok false are listed,
not costed. Exit 0 on a report, 2 on a malformed line or file.
"""
import argparse
import datetime
import json
import sys

ACTIONS = ("create", "stop", "extend", "delete")


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
            raw = handle.read().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise Malformed("cannot read %s: %s" % (path, exc.__class__.__name__))
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
        if line["action"] not in ACTIONS:
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


def build(day, lines, node_price, node_count, node_hours, pod_rate):
    start = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    end = start + datetime.timedelta(days=1)
    today = [l for l in lines if start <= l["_ts"] < end]
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
    nodes_hours = node_count * node_hours
    node_usd = node_price * nodes_hours
    return {"pods": pods, "failed": failed, "pod_usd": pod_usd, "pod_hours": pod_hours,
            "node_usd": node_usd, "node_hours": nodes_hours, "total": pod_usd + node_usd, "n": len(today)}


def render(day, r, out):
    if r["n"] == 0:
        out.write("cloud cost %s: USD 0 (no audit lines on this day)\n" % day)
        return
    out.write("cloud cost %s: USD %.2f per day\n" % (day, r["total"]))
    out.write("pods: %d, pod-hours %.2f, pod USD %.2f\n" % (len(r["pods"]), r["pod_hours"], r["pod_usd"]))
    out.write("nodes: node-hours %.2f, node USD %.2f\n" % (r["node_hours"], r["node_usd"]))
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
    parser.add_argument("--node-count", type=float, required=True)
    parser.add_argument("--node-hours", type=float, required=True)
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
        if min(args.node_price, args.node_count, args.node_hours, args.pod_rate) < 0:
            raise Malformed("negative node or pod input")
        lines = read_lines(args.audit)
    except Malformed as exc:
        sys.stderr.write("cloud_cost_report: %s\n" % exc)
        return 2
    render(args.day, build(args.day, lines, args.node_price, args.node_count, args.node_hours, args.pod_rate), out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
