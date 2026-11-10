import subprocess
import time

PIDFILE = "@PIDFILE@"   # the test replaces this text with the path to write the grandchild's pid to
p = subprocess.Popen(["sleep", "77"])
open(PIDFILE, "w").write(str(p.pid))
time.sleep(77)
