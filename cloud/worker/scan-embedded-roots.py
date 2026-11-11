#!/usr/bin/env python3
"""Operator tool (not installed in the image): look for listed certificates embedded in binaries.

Usage: CW_ROOT_LIST=<file> python3 scan-embedded-roots.py <binary>...
The list file holds one root per line: '<64-hex sha256 of the DER certificate> <label>'. Blank lines are
allowed; '#' comment lines are REFUSED (fail closed: a line that is not a root is an error). At most 10000
lines (a larger file is refused). The label is never printed: the output names a root by its index in the
list and the first 8 hex characters of its hash, so the output can be pasted without the list's content.
Matching is by SHA-256 only. The binary is read in chunks (CHUNK, 8 MiB), never whole, with an overlap
(OVERLAP, 16 KiB) at least as long as the longest accepted PEM block (16 KiB) and the longest accepted DER
certificate (2500 bytes); hits are de-duplicated by absolute offset.

Detected: PEM blocks (real newlines, or a literal backslash-n escape; other escapes are not handled) of at
most 16 KiB; DER SEQUENCE framing 30 82 LL LL 30 (total 300..2500 bytes), 30 83 LL LL LL 30 (total
300..2500) and 30 81 LL 30 (total 128..258).
NOT detected (no match is not proof of absence outside the forms listed): DER with any other length form,
a certificate shorter than 300 or longer than 2500 bytes (30 81 form: outside 128..258), indefinite
length, PEM with a split or compressed body, a compressed section of the binary, a certificate split in
pieces, UTF-16 strings, base64 outside PEM markers.
Exit: 1 if any listed root is found, 0 if none, 2 if the input is unusable or NOTHING was examined (a real
binary embeds a root bundle; zero certificates means the scan did not work, never 'clean')."""
import base64
import hashlib
import os
import re
import sys

CHUNK = 8 * 1024 * 1024
OVERLAP = 16384
MAX_PEM = 16384
MAX_LIST_LINES = 10000
PEM = re.compile(rb"-----BEGIN CERTIFICATE-----(.{0,%d}?)-----END CERTIFICATE-----" % MAX_PEM, re.S)
# (marker, header length, length-field bytes, min total, max total)
FORMS = ((b"\x30\x82", 4, 2, 300, 2500), (b"\x30\x83", 5, 3, 300, 2500), (b"\x30\x81", 3, 1, 128, 258))


def load_list(path):
    roots = []
    with open(path, "rb") as f:
        raw = f.read(2000000)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("the list is not UTF-8 text")
    lines = text.splitlines()
    if len(lines) > MAX_LIST_LINES:
        raise ValueError("the list has more than %d lines" % MAX_LIST_LINES)
    for line in lines:
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


def scan_buffer(data, base, hits):
    """Add (absolute offset, sha256) of every certificate found in data to hits."""
    for m in PEM.finditer(data):
        body = re.sub(rb"[^A-Za-z0-9+/=]", b"", m.group(1).replace(b"\\n", b""))
        try:
            der = base64.b64decode(body)
        except Exception:
            continue
        hits.add((base + m.start(), hashlib.sha256(der).hexdigest()))
    for marker, hlen, nlen, lo, hi in FORMS:
        pos = data.find(marker)
        while pos >= 0:
            if pos + hlen + 1 <= len(data) and data[pos + hlen] == 0x30:
                total = int.from_bytes(data[pos + 2:pos + 2 + nlen], "big") + hlen
                if lo <= total <= hi and pos + total <= len(data):
                    hits.add((base + pos, hashlib.sha256(data[pos:pos + total]).hexdigest()))
            pos = data.find(marker, pos + 1)


def scan_file(path):
    fps = set()
    base = 0
    tail = b""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            buf = tail + chunk
            scan_buffer(buf, base, fps)
            keep = min(OVERLAP, len(buf))
            tail = buf[len(buf) - keep:] if keep else b""
            base += len(buf) - len(tail)
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
        print("refused: %s" % (e if isinstance(e, ValueError) else "cannot read the list"))
        return 2
    results = []
    for b in argv:
        try:
            hits = scan_file(b)
        except OSError as e:
            print("refused: cannot read %s: %s" % (b, e.strerror))
            return 2
        if len(hits) == 0:
            print("refused: %s: nothing examined: the scan did not work" % b)
            return 2
        results.append((b, hits))
    print("list entries used: %d" % len(roots))
    found = False
    for b, hits in results:
        fps = {h[1] for h in hits}
        print("%s: %d certificates examined" % (b, len(hits)))
        for i, (fp, label) in enumerate(roots, 1):
            if fp in fps:
                found = True
                print("FOUND #%d %s" % (i, fp[:8]))
            else:
                print("absent #%d %s" % (i, fp[:8]))
    return 1 if found else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        print("refused: unexpected error")
        sys.exit(2)
