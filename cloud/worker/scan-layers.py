#!/usr/bin/env python3
"""Per-layer scanner for scan-secrets.sh (stdlib only). Usage: scan-layers.py <patterns-file> <image-archive> <allowlist>

Reads an image archive as a stream, one layer at a time, without extracting anything to disk: the output of
`docker save` (manifest.json + layer tarballs) or an OCI image layout (index.json -> manifest -> layer blobs, plain or
gzip; zstd only where python can read it). EVERY regular file of EVERY layer is scanned (also a file a later layer
deletes: the deletion is only a whiteout entry in the later layer), each layer once, plus the image configuration
(environment, history) as bytes. Files are searched with scan-files.py's byte scanner (raw and NUL-stripped).
Prints "layer <n> <digest>: <files> files, <MiB> MiB, <hits> hits" per layer, "layer <n>: /<path>: <pattern>" per hit
(never a value) and a totals line. Exit 0 = every layer scanned, no hit; 1 = hits; 2 = a layer, member or the archive
could not be read: "not scanned", never a pass.
"""
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("scan_files", os.path.join(HERE, "scan-files.py"))
sf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf)

CRED_NAMES = re.compile(r"(^|/)(\.ssh/id_[^/]*|\.git-credentials|\.netrc|\.config/gh/hosts\.yml|kubeconfig)$")


class Unreadable(Exception):
    pass


def zstd_open(fh):
    try:
        from compression import zstd  # python 3.14+
        return zstd.ZstdFile(fh)
    except ImportError:
        pass
    try:
        import zstandard
        return zstandard.ZstdDecompressor().stream_reader(fh)
    except ImportError:
        raise Unreadable("zstd layer and python has no zstd support")


def open_layer(raw):
    """A readable, decompressed byte stream of a layer blob (by magic bytes, not by media type)."""
    head = raw.read(4)
    raw.seek(0)
    if head[:2] == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=raw)
    if head[:4] == b"\x28\xb5\x2f\xfd":
        return zstd_open(raw)
    return raw


def norm(p):
    return os.path.normpath(p).lstrip("/")


def main(argv):
    if len(argv) != 4:
        print("usage: scan-layers.py <patterns-file> <image-archive> <allowlist>", file=sys.stderr)
        return 2
    pats = sf.load(argv[1])
    try:
        allow = sf.load_allow(argv[3])
    except sf.AllowlistError as e:
        print(f"scan-secrets: {e}: not scanned", file=sys.stderr)
        return 2
    try:
        outer = tarfile.open(argv[2], "r:")
        members = {norm(m.name): m for m in outer.getmembers()}
    except (OSError, tarfile.TarError) as e:
        print(f"scan-secrets: cannot read the archive: not scanned ({type(e).__name__})", file=sys.stderr)
        return 2

    def blob(path):
        m = members.get(norm(path))
        if m is None:
            raise Unreadable(f"{path} is not in the archive")
        f = outer.extractfile(m)
        if f is None:
            raise Unreadable(f"{path} is not a file")
        return f

    def jload(path):
        try:
            return json.loads(blob(path).read())
        except (ValueError, OSError, tarfile.TarError) as e:
            raise Unreadable(f"cannot parse {path}") from e

    # work list: (config path, [layer paths]) per image
    images = []
    try:
        if "manifest.json" in members:  # docker save
            for e in jload("manifest.json"):
                images.append((e["Config"], list(e["Layers"])))
        elif "index.json" in members:  # OCI layout
            def digest_path(d):
                algo, _, hexd = d.partition(":")
                return f"blobs/{algo}/{hexd}"

            def walk(desc_list, depth=0):
                if depth > 4:
                    raise Unreadable("image index nested too deep")
                for d in desc_list:
                    ann = d.get("annotations") or {}
                    if ann.get("vnd.docker.reference.type") == "attestation-manifest":
                        continue
                    doc = jload(digest_path(d["digest"]))
                    if "manifests" in doc:
                        walk(doc["manifests"], depth + 1)
                    else:
                        images.append((digest_path(doc["config"]["digest"]),
                                       [digest_path(l["digest"]) for l in doc["layers"]]))
            walk(jload("index.json")["manifests"])
        else:
            raise Unreadable("neither manifest.json nor index.json: not a docker save or OCI image archive")
    except (Unreadable, KeyError, TypeError, AttributeError) as e:
        print(f"scan-secrets: cannot read the image manifest: not scanned ({e})", file=sys.stderr)
        return 2

    hits = errors = files_t = allowed_t = 0
    bytes_t = 0
    n = 0
    seen = set()

    def report_hit(label, name):
        nonlocal hits
        hits += 1
        print(f"{label}: {name}")

    # the configuration (environment, labels, history) as bytes
    for cfg, _ in images:
        if cfg in seen:
            continue
        seen.add(cfg)
        found = set()
        try:
            sf.scan_fileobj(blob(cfg), pats, found)
        except Exception:  # fail closed: any error is 'not scanned', never an exit that reads as hits
            print(f"scan-secrets: cannot read the image configuration {cfg}: not scanned", file=sys.stderr)
            errors += 1
            continue
        for name, _rx in pats:
            if name in found:
                report_hit("config", name)

    for _cfg, layers in images:
        for lp in layers:
            if lp in seen:
                continue
            seen.add(lp)
            n += 1
            nfiles = nbytes = lhits = 0
            short = os.path.basename(lp)[:12] if lp.startswith("blobs/") else lp.split("/")[0][:12]
            try:
                with open_layer(blob(lp)) as stream, tarfile.open(fileobj=stream, mode="r|") as lt:
                    for m in lt:
                        if not m.isreg():  # directories, symlinks, devices; a hardlink's data is in the layer that holds it
                            continue
                        path = "/" + norm(m.name)
                        found = set()
                        if CRED_NAMES.search(path) and not path.endswith(".pub"):
                            found.add("credential-file")
                        fo = lt.extractfile(m)
                        h = hashlib.sha256()
                        sf.scan_fileobj(fo, pats, found, h)
                        nfiles += 1
                        nbytes += m.size
                        lh, la = sf.classify(path, found, h.hexdigest(), allow, pats,
                                             lambda kind, p, name: print(
                                                 f"layer {n}: {p}: {name}" if kind == "HIT"
                                                 else f"allowlisted: layer {n}: {p}: {name}"))
                        lhits += lh
                        hits += lh
                        allowed_t += la
            except Exception as e:  # a corrupt zstd/gzip/tar stream, a missing codec: all 'not scanned'
                print(f"scan-secrets: cannot read layer {n} {short}: not scanned ({type(e).__name__})", file=sys.stderr)
                errors += 1
            print(f"layer {n} {short}: {nfiles} files, {nbytes / 1048576:.1f} MiB, {lhits} hits")
            files_t += nfiles
            bytes_t += nbytes
    if n == 0 and not errors:
        print("scan-secrets: no layers found in the image: not scanned", file=sys.stderr)
        return 2
    print(f"total: {n} layers, {files_t} files, {bytes_t / 1048576:.1f} MiB, {hits} hits, {allowed_t} allowlisted")
    if errors:
        return 2
    if hits:
        print(f"scan-secrets: hits found in {n} layers (files + configuration + history)", file=sys.stderr)
        return 1
    print(f"scan-secrets: all {n} layers scanned (files + configuration + history)")
    return 0


if __name__ == "__main__":
    try:
        code = main(sys.argv)
    except Exception as e:  # nothing may end as a traceback with exit 1: that would read as "hits found"
        print(f"scan-secrets: cannot scan the archive: not scanned ({type(e).__name__})", file=sys.stderr)
        code = 2
    sys.exit(code)
