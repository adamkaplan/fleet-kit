#!/bin/sh
# cw-probe-reach: what a worker's unix user can still reach, said plainly. Run inside the image as the worker user.
#   exit 0  the real gh is NOT reachable at its absolute path (running it gives 127), and the three routes that remain are listed
#   exit 1  the real gh is still there
# The routes are a LIST, not a boundary: the token's scope and the repository's rules are the boundary (cloud/README.md).
# CW_PROBE_GH names the path to check (tests); the default is where the package put it.
real="${CW_PROBE_GH:-/usr/bin/gh}"
"$real" --version >/dev/null 2>&1; rc=$?
echo "real gh at $real: exit $rc (127 = not there)"
[ "$rc" = 127 ] || { echo "FAIL: the real gh is reachable"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1 && echo present || echo absent; }
echo "route: curl ($(have curl)) with the token file: any HTTP call the token allows"
echo "route: python3 ($(have python3)) with the token file: any HTTP call the token allows"
echo "route: git credential helper ($(have git)): git push and fetch to github.com as the token"
exit 0
