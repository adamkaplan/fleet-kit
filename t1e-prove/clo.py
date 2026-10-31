import sys
exec(open('/tmp/opencode/dep.py').read().split("for name in sys.argv")[0])
def deps(name):
    n=nodes[name];s=scopes.get((name,n.lineno))
    if s: u=m.used_globals(s)|{ast.unparse(b).split(".")[0] for b in getattr(n,"bases",[])}
    else: u={x.id for x in ast.walk(n) if isinstance(x,ast.Name)}
    return {x for x in u if x in nodes and x not in where and x!=name and x not in std and x not in bi and x not in("REPO","BIN")}
seen=[];st=list(sys.argv[1:])
while st:
    x=st.pop()
    if x in seen: continue
    seen.append(x);st+=sorted(deps(x))
tot=0
for x in seen:
    n=nodes[x];ln=n.end_lineno-n.lineno+1;tot+=ln;print(x,n.lineno,n.end_lineno,ln,sorted(deps(x)))
print("total",tot,len(seen))
