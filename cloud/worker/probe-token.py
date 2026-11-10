#!/usr/bin/env python3
"""probe-token.py THROWAWAY_OWNER/NAME: the plan of the live probes of the token table's "to be probed live" rows.

usage: probe-token.py OWNER/NAME [--list]

It refuses (exit 2, one line) any target but the ONE throwaway repository named by the environment variable CW_PROBE_THROWAWAY:
a product repository is never a target, and an unset variable refuses everything. THIS VERSION MAKES NO REQUEST AND READS NO
CREDENTIAL: it prints, from cloud/token-table.txt, each row a tester must probe by hand on the throwaway repository with a
worker-scoped token (the live step is after merge, plan #1031 scenario S3, claim epic). Exit 0 after printing the rows.
"""
import os
import re
import sys

TABLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "token-table.txt")


def rows():
    with open(TABLE, encoding="utf-8") as f:
        return [tuple(x.strip() for x in l.rstrip("\n").split(" | ")) for l in f if l.strip() and not l.startswith("#")]


def main(argv):
    args = [a for a in argv if a != "--list"]
    allowed = os.environ.get("CW_PROBE_THROWAWAY", "")
    if len(args) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args[0]) or not allowed or args[0].lower() != allowed.lower():
        print("probe-token: refused: the target must be exactly the throwaway repository named by CW_PROBE_THROWAWAY")
        return 2
    for action, token, _, status in rows():
        if "to be probed live" in status:
            print("probe: %s | documented: %s | status: %s" % (action, token, status))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
