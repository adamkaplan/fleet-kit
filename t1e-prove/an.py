import importlib.machinery
import ast,sys,symtable,builtins,glob,os,collections,importlib.util
spec=importlib.util.spec_from_loader("m",importlib.machinery.SourceFileLoader("m","tools/arch-test-move"))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
src=sys.argv[1]; sd=sys.argv[2]
text=open(src).read();tree=ast.parse(text);table=symtable.symtable(text,src,"exec")
scopes={(c.get_name(),c.get_lineno()):c for c in table.get_children()}
where={}
for p in glob.glob(sd+"/*.py"):
    for n in ast.parse(open(p).read()).body:
        for x in m.defined(n): where.setdefault(x,1)
std=set()
for n in tree.body:
    if isinstance(n,(ast.Import,ast.ImportFrom)):
        for a in n.names: std.add((a.asname or a.name).split(".")[0])
bi=set(dir(builtins))
defs=set()
for n in tree.body:
    defs|=m.defined(n)
blk=collections.defaultdict(list);ok=[];tot=0;allc=0
for n in tree.body:
    if not isinstance(n,ast.ClassDef):continue
    allc+=1
    sz=n.end_lineno-n.lineno+1
    sc=scopes[(n.name,n.lineno)]
    miss=set()
    for u in m.used_globals(sc)|{ast.unparse(b).split(".")[0] for b in n.bases}:
        if u in bi or u in std or u==n.name or u in("REPO","BIN") or u in where: continue
        if u in defs: miss.add(u)
    if miss:
        for u in miss: blk[u].append(sz)
    else: ok.append((n.name,sz))
print("classes",allc,"movable",len(ok),sum(s for _,s in ok),"refused",allc-len(ok))
for k,v in sorted(blk.items(),key=lambda kv:-len(kv[1]))[:int(sys.argv[3]) if len(sys.argv)>3 else 30]:
    print("%-34s %4d %7d"%(k,len(v),sum(v)))
