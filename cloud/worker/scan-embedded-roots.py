#!/usr/bin/env python3
"""Operator tool (not installed in the image): look for listed certificates embedded in binaries.

Usage: CW_ROOT_LIST=<file> python3 scan-embedded-roots.py <binary>...
The list file holds one root per line: '<64-hex sha256 of the DER certificate> <label>'.
Matching is by SHA-256 only. Certificates are found as PEM blocks and as DER SEQUENCE framing
(30 82 LL LL 30, total length 4+LLLL between 300 and 2500).
Exit: 1 if any listed root is found, 0 if none, 2 if the input is unusable (never reports clean then)."""
import base64
import hashlib
import os
import re
import sys

PEM = re.compile(rb"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", re.S)


def load_list(path):
    roots = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2 or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                raise ValueError("malformed line in the list")
            roots.append((parts[0].lower(), parts[1].strip()))
    if not roots:
        raise ValueError("the list is empty")
    return roots


def fingerprints(data):
    fps = set()
    for m in PEM.finditer(data):
        body = re.sub(rb"[^A-Za-z0-9+/=]", b"", m.group(1).replace(b"\\n", b""))
        try:
            fps.add(hashlib.sha256(base64.b64decode(body)).hexdigest())
        except Exception:
            continue
    pos = data.find(b"\x30\x82")
    while pos >= 0:
        if pos + 5 <= len(data) and data[pos + 4] == 0x30:
            total = int.from_bytes(data[pos + 2:pos + 4], "big") + 4
            if 300 <= total <= 2500 and pos + total <= len(data):
                fps.add(hashlib.sha256(data[pos:pos + total]).hexdigest())
        pos = data.find(b"\x30\x82", pos + 1)
    return fps


def main(argv):
    path = os.environ.get("CW_ROOT_LIST")
    if not path:
        print("refused: CW_ROOT_LIST is not set")
        return 2
    if not argv:
        print("refused: no binary given")
        return 2
    try:
        roots = load_list(path)
    except (OSError, ValueError) as e:
        print("refused: %s" % e)
        return 2
    results = []
    for b in argv:
        try:
            with open(b, "rb") as f:
                data = f.read()
        except OSError as e:
            print("refused: cannot read %s: %s" % (b, e.strerror))
            return 2
        results.append((b, fingerprints(data)))
    found = False
    for b, fps in results:
        print("%s: %d certificates examined" % (b, len(fps)))
        for fp, label in roots:
            if fp in fps:
                found = True
                print("FOUND %s %s" % (label, fp[:16]))
            else:
                print("absent %s" % label)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
