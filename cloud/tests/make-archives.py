#!/usr/bin/env python3
"""Builds tiny layered image archives for test-cloud (no docker). Usage: make-archives.py <outdir> <token>
Writes: dockersave-clean.tar, dockersave-deleted.tar (token written in layer 2, deleted in layer 3),
oci-deleted.tar (same, gzip layers), oci-utf16.tar (token as UTF-16LE in layer 2), oci-badlayer.tar (a layer that is
not readable), oci-zstd.tar (a zstd layer), oci-env.tar (token only in the image config), notimage.tar."""
import gzip, hashlib, io, json, sys, tarfile, os

out, token = sys.argv[1], sys.argv[2]


def layer(files):  # {path: bytes}
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w") as t:
        for p, d in files.items():
            ti = tarfile.TarInfo(p); ti.size = len(d); t.addfile(ti, io.BytesIO(d))
    return b.getvalue()


def add(t, name, data):
    ti = tarfile.TarInfo(name); ti.size = len(data); t.addfile(ti, io.BytesIO(data))


def sha(d):
    return hashlib.sha256(d).hexdigest()


def layers_for(kind):
    l1 = {"etc/os": b"base\n"}
    tok = (token + "\n").encode()
    if kind == "clean":
        return [l1, {"app/a": b"hello\n"}, {"app/b": b"hi\n"}]
    if kind == "deleted":
        return [l1, {"x": b"GITHUB_TOKEN=" + tok}, {".wh.x": b""}]
    if kind == "utf16":
        return [l1, {"y": ("GH_TOKEN=" + token + "\n").encode("utf-16-le")}, {"z": b"1"}]
    return [l1, {"a": b"1"}, {"b": b"2"}]


def dockersave(name, kind):
    with tarfile.open(os.path.join(out, name), "w") as t:
        names = []
        for i, f in enumerate(layers_for(kind), 1):
            n = f"l{i}/layer.tar"; add(t, n, layer(f)); names.append(n)
        add(t, "cfg.json", json.dumps({"config": {"Env": ["A=b"]}, "history": [{"created_by": "true"}]}).encode())
        add(t, "manifest.json", json.dumps([{"Config": "cfg.json", "Layers": names}]).encode())


def oci(name, kind, gz=True, cfg_env="A=b", mutate=None):
    with tarfile.open(os.path.join(out, name), "w") as t:
        ds = []
        for i, f in enumerate(layers_for(kind), 1):
            data = layer(f)
            data = gzip.compress(data) if gz else data
            if mutate and i == 2:
                data = mutate(data)
            ds.append(sha(data)); add(t, f"blobs/sha256/{ds[-1]}", data)
        cfg = json.dumps({"config": {"Env": [cfg_env]}, "history": [{"created_by": "true"}]}).encode()
        add(t, f"blobs/sha256/{sha(cfg)}", cfg)
        man = json.dumps({"config": {"digest": "sha256:" + sha(cfg)},
                          "layers": [{"digest": "sha256:" + d} for d in ds]}).encode()
        add(t, f"blobs/sha256/{sha(man)}", man)
        add(t, "index.json", json.dumps({"manifests": [{"digest": "sha256:" + sha(man)}]}).encode())


dockersave("dockersave-clean.tar", "clean")
dockersave("dockersave-deleted.tar", "deleted")
oci("oci-clean.tar", "clean")
oci("oci-deleted.tar", "deleted")
oci("oci-utf16.tar", "utf16", gz=False)
oci("oci-env.tar", "clean", cfg_env="GH_TOKEN=" + token)
oci("oci-badlayer.tar", "clean", mutate=lambda d: d[:len(d) // 2])
oci("oci-zstd.tar", "clean", mutate=lambda d: b"\x28\xb5\x2f\xfd" + b"not a zstd frame")
with open(os.path.join(out, "notimage.tar"), "wb") as f:
    f.write(b"not a tar")

# the MERGED filesystem of the deleted image (what a scan of the finished image sees): whiteouts applied
merged = os.path.join(out, "merged-deleted"); os.makedirs(merged)
for f in layers_for("deleted"):
    for p, d in f.items():
        base = os.path.basename(p)
        if base.startswith(".wh."):
            os.remove(os.path.join(merged, os.path.dirname(p), base[4:]))
        else:
            os.makedirs(os.path.join(merged, os.path.dirname(p)), exist_ok=True)
            open(os.path.join(merged, p), "wb").write(d)
