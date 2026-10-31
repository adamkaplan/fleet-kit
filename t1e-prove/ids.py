import sys,importlib.machinery,importlib.util,unittest
p=sys.argv[1]
l=importlib.machinery.SourceFileLoader("tsb_mod",p)
s=importlib.util.spec_from_loader("tsb_mod",l);m=importlib.util.module_from_spec(s);sys.modules["tsb_mod"]=m
sys.argv=[p];l.exec_module(m)
def ids(x):
    for t in x:
        if isinstance(t,unittest.TestSuite): yield from ids(t)
        else: yield t.id().replace("tsb_mod.","",1)
for i in sorted(ids(unittest.TestLoader().loadTestsFromModule(m))): print(i)
