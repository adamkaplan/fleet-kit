import subprocess,re,collections
d=subprocess.run(["git","diff","-U0","99a612b1","821ce612","--","bin/test-switchboard","tests",":!tests/switchboard/support/herdr_world.py"],capture_output=True,text=True).stdout
f=None;c=collections.Counter();pure=collections.Counter();ex=[]
for l in d.splitlines():
    if l.startswith("+++ "): f=l[6:]
    elif l.startswith("@@"):
        m=re.match(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@",l);r=int(m.group(1) or 1)
        c[f]+=r
    elif l.startswith("-") and not l.startswith("---"): ex.append((f,l[:100]))
print(dict(c),sum(c.values()))
for e in ex: print(e)
