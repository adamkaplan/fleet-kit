import ast,sys,symtable,builtins,glob,importlib.machinery,importlib.util
spec=importlib.util.spec_from_loader("m",importlib.machinery.SourceFileLoader("m","tools/arch-test-move"))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
src="bin/test-switchboard";text=open(src).read();tree=ast.parse(text);table=symtable.symtable(text,src,"exec")
scopes={}
for c in table.get_children(): scopes[(c.get_name(),c.get_lineno())]=c
where=set()
for p in glob.glob("tests/switchboard/support/*.py"):
    for n in ast.parse(open(p).read()).body: where|=m.defined(n)
std=set()
for n in tree.body:
    if isinstance(n,(ast.Import,ast.ImportFrom)):
        for a in n.names: std.add((a.asname or a.name).split(".")[0])
bi=set(dir(builtins))
nodes={}
for n in tree.body:
    for x in m.defined(n): nodes.setdefault(x,n)
for name in sys.argv[1:]:
    n=nodes[name]
    s=scopes.get((name,n.lineno))
    if s: u=m.used_globals(s)|{ast.unparse(b).split(".")[0] for b in getattr(n,"bases",[])}
    else: u={x.id for x in ast.walk(n) if isinstance(x,ast.Name)}
    miss=sorted(x for x in u if x in nodes and x not in where and x!=name and x not in std and x not in bi and x not in("REPO","BIN"))
    ntest=sum(1 for f in ast.walk(n) if isinstance(f,ast.FunctionDef) and f.name.startswith("test")) if isinstance(n,ast.ClassDef) else 0
    print(name,n.lineno,n.end_lineno,n.end_lineno-n.lineno+1,"tests",ntest,"needs",miss)
