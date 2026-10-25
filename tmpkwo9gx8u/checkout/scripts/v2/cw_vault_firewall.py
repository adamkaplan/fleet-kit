import json, os, sys, time
from pathlib import Path
root = Path(__file__).resolve().parents[2]
cfg = json.loads((root / "fake.json").read_text())
def note(kind):
    with open(str(root / "runs.jsonl"), "a") as f:
        f.write(json.dumps({"kind": kind, "t": time.time(), "argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ), "pid": os.getpid(),
                            "pgid": os.getpgid(0), "home": os.environ.get("HOME")}) + "\n")
note("start")
if cfg.get("grandchild"):
    import subprocess
    sub = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])   # keeps the check's stdout open
    with open(str(root / "grandchild.pid"), "w") as f:
        f.write(str(sub.pid))
time.sleep(cfg.get("sleep", 0))
if cfg.get("fixes"):
    (root / "fixed").write_text("x")
print("checking address " + cfg.get("address", "addr-marker"))
print(cfg.get("line", "rules 3 -> 4, 2026-10-19"))
sys.stderr.write("address " + cfg.get("address", "addr-marker") + "\n")
note("end")
sys.exit(cfg["code"])
