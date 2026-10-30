import ast,glob,re,symtable,builtins
src="bin/test-switchboard";L=open(src).read().split("\n")
ranges=[(168,182),(186,294),(305,319),(567,959)]  # 1-based inclusive
blocks=["\n".join(L[a-1:b]) for a,b in ranges]
body="\n\n\n".join(blocks)
tree=ast.parse(body)
defined=set()
for n in tree.body:
    if isinstance(n,(ast.FunctionDef,ast.ClassDef)): defined.add(n.name)
    elif isinstance(n,ast.Assign):
        for t in n.targets: defined|={x.id for x in ast.walk(t) if isinstance(x,ast.Name)}
used={x.id for x in ast.walk(tree) if isinstance(x,ast.Name)}
where={}
for p in sorted(glob.glob("tests/switchboard/support/*.py")):
    mod=p.split("/")[-1][:-3]
    if mod=="__init__":continue
    for n in ast.parse(open(p).read()).body:
        nm=set()
        if isinstance(n,(ast.FunctionDef,ast.ClassDef)): nm={n.name}
        elif isinstance(n,ast.Assign):
            for t in n.targets: nm|={x.id for x in ast.walk(t) if isinstance(x,ast.Name)}
        for x in nm: where.setdefault(x,mod)
need={}
for u in sorted(used-defined):
    if u in where: need.setdefault(where[u],[]).append(u)
    elif u in("REPO","BIN"): need.setdefault("paths",[]).append(u)
    elif u in("PidTable",): print("UNRESOLVED",u)
hdr=open("tests/switchboard/support/rigs.py").read().split("\n")[1:34]
hdr="\n".join(hdr)+"\nimport sys, threading, subprocess, unittest, time, types, signal, socket, tempfile, uuid, stat, tarfile, textwrap, zipfile, sysconfig\n"
out='"""Shared fakes and helpers of bin/test-switchboard (moved text, unchanged): made-up pids, FakeHerdr, World."""\n'+hdr+"\n".join("from support.%s import %s"%(m,", ".join(v)) for m,v in need.items())+"\n\n\n"+body+"\n"
open("tests/switchboard/support/herdr_world.py","w").write(out)
names=sorted(n for n in defined if n!="unittest")
drop=set()
for a,b in ranges: drop|=set(range(a-1,b))
anchor=max(i for i,l in enumerate(L) if l.rstrip().endswith("# support import") and i<400)
ins=['_support_herdr_world = importlib.import_module("support.herdr_world")   # support import']
for i in range(0,len(names),4):
    p=names[i:i+4];c="," if len(p)==1 else ""
    ins.append("%s%s = (%s%s)   # support import"%(", ".join(p),c,", ".join("_support_herdr_world."+x for x in p),c))
new=[]
for i,l in enumerate(L):
    if i in drop: continue
    new.append(l)
    if i==anchor: new+=ins
open(src,"w").write("\n".join(new))
print(names,len(need),{k:len(v) for k,v in need.items()})
