#!/usr/bin/env python3
"""Per-layer scanner for scan-secrets.sh (stdlib only). Usage: scan-layers.py <patterns-file> <image-archive> <allowlist>

Reads an image archive as a stream, one layer at a time, without extracting anything to disk: the output of
`docker save` (manifest.json + layer tarballs) or an OCI image layout (index.json -> manifest -> layer blobs, plain or
gzip; zstd only where python can read it). EVERY regular file of EVERY layer is scanned (also a file a later layer
deletes: the deletion is only a whiteout entry in the later layer), each layer once, plus the image configuration
(environment, history) as bytes. Files are searched with scan-files.py's byte scanner (raw and NUL-stripped).
Files that are UNMODIFIED files of installed distro packages (in a dpkg .list AND with their md5 in the same package's
.md5sums, read from the MERGED view of the image's layers) are not scanned; the decision is made after the last layer. An
unusable dpkg database means nothing is skipped (stderr says so). See cloud/README.md.
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


DBFILE = re.compile(r"/var/lib/dpkg/info/[^/]+\.(list|md5sums)")


def image_skipmap(layer_infos):
    """The skip map of one image from its layers in order. Whiteouts (.wh.<name>) delete the path and what is under it in
    the lower layers; an opaque marker (.wh..wh..opq) deletes what is under its directory; a later entry at a path
    replaces the lower one (a regular file or hardlink replaces a symlink or a dpkg file; a directory replaces a symlink)."""
    sym, db = {}, {}
    prefixes = set()

    def under(p):
        return [k for d in (sym, db) for k in d if k.startswith(p.rstrip("/") + "/")]

    def drop(p, tree):
        for d in (sym, db):
            d.pop(p, None)
        if tree or p in prefixes:
            for k in under(p):
                sym.pop(k, None)
                db.pop(k, None)

    def note(p):
        parts = p.split("/")
        for i in range(2, len(parts)):
            prefixes.add("/".join(parts[:i]))

    for info in layer_infos:
        for p in info["wh"]:
            drop(p, True)
        for p in info["opq"]:
            for k in under(p):
                sym.pop(k, None)
                db.pop(k, None)
        for p in info["dirs"]:
            sym.pop(p, None)
            db.pop(p, None)
        for p in info["repl"]:
            drop(p, False)
        for p, _m in info["recs"]:
            drop(p, False)
        for p, t in info["syms"]:
            drop(p, False)
            sym[p] = t
            note(p)
        for p, data in info["db"].items():
            db[p] = data
            note(p)
    lists, sums = {}, {}
    for p, data in db.items():
        d, _, f = p.rpartition("/")
        if d != sf.DPKG_INFO:
            continue
        stem, _, ext = f.rpartition(".")
        (lists if ext == "list" else sums)[stem] = data
    return sf.build_skipmap(lists, sums, lambda p: sym.get(p))


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

    # Pass over the layers (each once). Per layer we keep what the dpkg rule needs: every regular file's md5, the hits
    # (decided at the end, when the merged view of the dpkg database is known), the symlinks, directories, whiteouts and
    # the dpkg list/md5sums files. Nothing else is kept.
    infos = {}   # layer path -> info
    order = []   # layer paths, in the order they are scanned
    layer_imgs = {}
    img_layers = []
    for idx, (_cfg, layers) in enumerate(images):
        img_layers.append(list(layers))
        for lp in layers:
            layer_imgs.setdefault(lp, []).append(idx)
            if lp in seen:
                continue
            seen.add(lp)
            n += 1
            order.append(lp)
            info = infos[lp] = {"n": n, "recs": [], "pending": [], "syms": [], "dirs": [], "repl": [], "wh": [], "opq": [],
                                "db": {}, "files": 0, "bytes": 0, "short": os.path.basename(lp)[:12] if lp.startswith("blobs/") else lp.split("/")[0][:12]}
            try:
                with open_layer(blob(lp)) as stream, tarfile.open(fileobj=stream, mode="r|") as lt:
                    for m in lt:
                        path = "/" + norm(m.name)
                        if m.isdir():
                            info["dirs"].append(path)
                            continue
                        if m.issym():
                            info["syms"].append((path, m.linkname))
                            continue
                        if not m.isreg():  # devices; a hardlink's data is in the layer that holds it (it replaces the path)
                            info["repl"].append(path)
                            continue
                        base = path.rpartition("/")[2]
                        if base == ".wh..wh..opq":
                            info["opq"].append(path.rpartition("/")[0] or "/")
                        elif base.startswith(".wh."):
                            info["wh"].append(path.rpartition("/")[0] + "/" + base[4:])
                        found = set()
                        if CRED_NAMES.search(path) and not path.endswith(".pub"):
                            found.add("credential-file")
                        fo = lt.extractfile(m)
                        h = hashlib.sha256()
                        m5 = sf.md5()
                        dbm = DBFILE.fullmatch(path)
                        if dbm:  # a dpkg list or md5sums file: kept (bounded), and scanned like every file
                            data = fo.read(sf.DPKG_MAX + 1)
                            info["db"][path] = data if len(data) <= sf.DPKG_MAX else None
                            fo = io.BytesIO(data)
                        sf.scan_fileobj(fo, pats, found, sf.Multi(h, m5))
                        info["files"] += 1
                        info["bytes"] += m.size
                        info["recs"].append((path, m5.hexdigest()))
                        if found:
                            info["pending"].append((path, found, h.hexdigest(), m5.hexdigest()))
            except Exception as e:  # a corrupt zstd/gzip/tar stream, a missing codec: all 'not scanned'
                print(f"scan-secrets: cannot read layer {n} {info['short']}: not scanned ({type(e).__name__})", file=sys.stderr)
                errors += 1

    # The merged view of the dpkg database of each image (layers in order, whiteouts applied), then the skip map.
    skipmaps = []
    for idx, lps in enumerate(img_layers):
        try:
            skipmaps.append(image_skipmap([infos[lp] for lp in lps]))
        except sf.DpkgError as e:
            skipmaps.append({})
            print(f"scan-secrets: dpkg database not usable in image {idx + 1} ({e}): skip set EMPTY, every file is scanned", file=sys.stderr)

    def owned(lp, path, md5hex):  # an unmodified file of an installed distro package in EVERY image that holds the layer
        return all(md5hex in skipmaps[i].get(path, ()) for i in layer_imgs[lp])

    skipped_t = 0
    for lp in order:
        info = infos[lp]
        lhits = 0
        for path, found, sha, md5hex in info["pending"]:
            if owned(lp, path, md5hex):
                found = found & {"credential-file"}
            lh, la = sf.classify(path, found, sha, allow, pats,
                                 lambda kind, p, name: print(
                                     f"layer {info['n']}: {p}: {name}" if kind == "HIT"
                                     else f"allowlisted: layer {info['n']}: {p}: {name}"))
            lhits += lh
            hits += lh
            allowed_t += la
        skipped_t += sum(1 for path, md5hex in info["recs"] if owned(lp, path, md5hex))
        print(f"layer {info['n']} {info['short']}: {info['files']} files, {info['bytes'] / 1048576:.1f} MiB, {lhits} hits")
        files_t += info["files"]
        bytes_t += info["bytes"]
    if n == 0 and not errors:
        print("scan-secrets: no layers found in the image: not scanned", file=sys.stderr)
        return 2
    print(f"total: {n} layers, {files_t} files, {bytes_t / 1048576:.1f} MiB, {hits} hits, {allowed_t} allowlisted")
    print(f"dpkg: {skipped_t} unmodified files of installed distro packages not scanned")
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
