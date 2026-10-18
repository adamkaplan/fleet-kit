#!/usr/bin/env python3
"""File scanner for scan-secrets.sh (stdlib only). Usage: scan-files.py <patterns-file> <root>...

The patterns file holds one "name@@regex" per line. Every regular file under the roots is read as BYTES
(binary files are scanned, never skipped) in streamed chunks with an overlap, so a token that straddles a chunk edge
is found and memory stays bounded; there is no file-size limit. Each file is searched in two forms: as it is, and with
every NUL byte removed (which turns UTF-16LE, UTF-16BE and NUL-padded text into plain ASCII). Base64, compressed
or otherwise encoded forms are NOT searched. A "$" also matches before a CR, so a CRLF line end does not hide a key.
Prints "path: name" per file and pattern, never the matched value. Exit 0 = no hit, 1 = hits, 2 = something could not
be read (never a pass).
"""
import os
import re
import sys

CHUNK = 4 * 1024 * 1024
OVERLAP = 64 * 1024


def load(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line:
                continue
            name, _, rx = line.partition("@@")
            rx = rx.replace("$", r"\r?$") if rx.endswith("$") else rx
            out.append((name, re.compile(rx.encode(), re.M)))
    return out


def scan_fileobj(fh, pats, found):
    """Search a binary stream chunk by chunk (as it is and NUL-stripped); add the names of the patterns that hit."""
    tail = b""
    while True:
        data = fh.read(CHUNK)
        if not data and tail:
            break
        buf = tail + data
        for name, rx in pats:
            if name in found:
                continue
            if rx.search(buf) or rx.search(buf.replace(b"\x00", b"")):
                found.add(name)
        if not data:
            break
        tail = buf[-OVERLAP:]


def scan_file(path, pats, found):
    with open(path, "rb") as fh:
        scan_fileobj(fh, pats, found)


def main(argv):
    if len(argv) < 3:
        print("usage: scan-files.py <patterns-file> <root>...", file=sys.stderr)
        return 2
    pats = load(argv[1])
    hits = 0
    bad = []

    def onerr(e):
        bad.append(e)

    def one(p):
        nonlocal hits
        found = set()
        try:
            scan_file(p, pats, found)
        except OSError:
            bad.append(p)
            return
        for name, _ in pats:
            if name in found:
                print(f"{p}: {name}")
                hits += 1

    for root in argv[2:]:
        if os.path.islink(root):
            continue
        if os.path.isfile(root):
            one(root)
            continue
        for d, _dirs, files in os.walk(root, onerror=onerr):
            for f in files:
                p = os.path.join(d, f)
                if os.path.islink(p) or not os.path.isfile(p):
                    continue
                one(p)
    if bad:
        print("scan-secrets: could not read some files: not scanned", file=sys.stderr)
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
