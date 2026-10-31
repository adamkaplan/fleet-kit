import subprocess,collections
g=lambda a:subprocess.run(["git","show",a],capture_output=True,text=True).stdout.splitlines()
b=collections.Counter(g("99a612b1:bin/test-switchboard"));h=collections.Counter(g("821ce612:bin/test-switchboard"))
rem=b-h
mod=collections.Counter(open("tests/switchboard/support/herdr_world.py").read().splitlines())
print("removed total",sum(rem.values()),"distinct",len(rem))
miss=rem-mod
print("missing from module",sum(miss.values()))
for l,c in miss.items(): print(c,repr(l[:60]))
