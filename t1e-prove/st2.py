exec(open('/tmp/opencode/an.py').read().split("blk=collections")[0])
cls={n.name:n for n in tree.body if isinstance(n,ast.ClassDef)}
def tests(n): return sum(1 for f in n.body if isinstance(f,ast.FunctionDef) and f.name.startswith("test"))
def miss(n):
    sc=scopes[(n.name,n.lineno)]
    return {u for u in m.used_globals(sc)|{ast.unparse(b).split(".")[0] for b in n.bases} if u not in bi and u not in std and u!=n.name and u not in("REPO","BIN") and u not in where and u in defs}
helpers={k for k,n in cls.items() if tests(n)==0 and not isinstance(n,type(None))}
moved=[];ch=True
while ch:
    ch=False
    for k in sorted(helpers):
        if k in where_h if False else False: pass
    for k in sorted(helpers-set(moved)):
        if not miss(cls[k]) :
            moved.append(k);where[k]=1;ch=True
print("helper classes movable to support (iteratively):",len(moved),sum(cls[k].end_lineno-cls[k].lineno+1 for k in moved))
print(moved)
ok=[(n,x.end_lineno-x.lineno+1) for n,x in cls.items() if n not in moved and not miss(x)]
print("test classes movable after:",len(ok),sum(s for _,s in ok))
