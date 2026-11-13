"""Registered system senders (ask #2518): a background process of the kit that is no agent and has no pane (the cloud reaper) may send as a NAMED system
sender, and only when the calling PROCESS proves it is that sender.

What proves it: the process that runs `fleet-switchboard send` is the child of the registered program. The registry row names the program (a script of
THIS kit, resolved from the switchboard's own location) and its first argument; the proof reads the parent process from the operating system (its
user, its executable and its argument list; /proc where there is one, else `ps`), never anything the caller says about itself. Nothing is a secret and
nothing is kept in a file or in the environment: the environment variable only NAMES the sender being claimed, and a claim with no proof is refused.

Honest limit: this is a process-level check, as the state directory's own trust is: a process of the same user can start the registered program itself
(that runs the real program), or could run a program with a forged argument list. A person's pane, a session id, a pane variable, the claim variable
or `--from` cannot make the parent process the reaper. Standard library only. Nothing here sends or writes."""
import os
import subprocess
import sys

SYSTEM_SENDER_ENV = "SWITCHBOARD_SYSTEM_SENDER"   # names the claimed sender; it is not a credential and proves nothing

# name -> the one program that is that sender, the one word that program must be run with, and who it may write to
SYSTEM_SENDERS = {
    "cloud-reaper": {"script": ("cloud", "launcher", "cloud_worker.py"), "verb": "reap", "recipients": ("cos",)},
}


PLAIN_OPTIONS = ("-B", "-u", "-E", "-s", "-S", "-I", "-O", "-OO", "-b", "-bb", "-q")


FIXED_INTERPRETERS = ("/usr/bin/python3", "/usr/local/bin/python3", "/opt/homebrew/bin/python3", "/usr/bin/python", "/bin/python3")


def interpreters(executable=None, fixed=FIXED_INTERPRETERS, base_prefix=None):
    """The real paths of the Python interpreters that count: this switchboard's own, the fixed system places, and (macOS framework builds) the framework's app binary of the SAME
    framework version, derived from this interpreter's own base prefix (`<prefix>/Resources/Python.app/Contents/MacOS/Python`): a service started through the launcher link
    (`/usr/local/bin/python3`) runs that app binary, whose real path differs from the link's. NEVER the PATH of the process asking: the process being checked chooses its own PATH,
    so a copy of a shell named python placed first in it would be trusted. A copy or a link to a shell is none of these."""
    own = sys.executable if executable is None else executable
    prefix = os.path.dirname(os.path.dirname(os.path.realpath(sys.executable))) if base_prefix is None else base_prefix   # <prefix>/bin/pythonX.Y of a framework build
    app = os.path.join(prefix, "Resources", "Python.app", "Contents", "MacOS", "Python") if prefix else None
    return {os.path.realpath(p) for p in (own,) + tuple(fixed) + (app,) if p and os.path.exists(p)}


def registered(name):
    return isinstance(name, str) and name in SYSTEM_SENDERS


def read_process(pid):
    """(uid, executable, argument list) of a process, or None when it cannot be read. /proc first; `ps` where there is none (an argument with a space cannot be told apart there)."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            argv = [a.decode("utf-8", "replace") for a in handle.read(65536).split(b"\0") if a]
        uid = os.stat("/proc/%d" % pid).st_uid
        try:
            exe = os.path.realpath("/proc/%d/exe" % pid)
        except OSError:
            exe = None
        return (uid, exe, argv) if argv else None
    except OSError:
        pass
    try:
        done = subprocess.run(["ps", "-ww", "-o", "uid=,command=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        fields = done.stdout.split()
        if done.returncode != 0 or len(fields) < 2:
            return None
        comm = subprocess.run(["ps", "-ww", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        return int(fields[0]), comm or None, fields[1:]   # the executable as the system names it, not the first word of the argument list
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def parent_is(name, kit_root, parent=None, reader=read_process, uid=None, known=None):
    """True only when the parent process is the registered program of `name`: the same user, a known Python interpreter (by real path, from fixed places, never the caller's PATH), running the registered script of this kit
    by an absolute path, with the registered word right after it. Anything unreadable, unregistered or different is False."""
    row = SYSTEM_SENDERS.get(name) if isinstance(name, str) else None
    if row is None:
        return False
    info = reader(os.getppid() if parent is None else parent)
    if info is None:
        return False
    owner, exe, argv = info
    if owner != (os.getuid() if uid is None else uid):
        return False
    if not exe or os.path.realpath(exe) not in (interpreters() if known is None else known):   # the executable must BE a known interpreter (by real path, never by name); unreadable, no proof
        return False
    options = 1
    while options < len(argv) and argv[options] in PLAIN_OPTIONS:   # only options that take no value and run no code: `-c`, `-m`, `-W x` and the like are never skipped
        options += 1
    if options + 1 >= len(argv) or argv[options + 1] != row["verb"]:
        return False
    script = argv[options]
    wanted = os.path.join(os.path.realpath(str(kit_root)), *row["script"])
    if not os.path.isabs(script) or os.path.realpath(script) != os.path.realpath(wanted):
        return False
    try:
        mode = os.stat(wanted).st_mode
    except OSError:
        return False
    return not mode & 0o022   # a program that others may write is no proof


def refusal(name, recipient, kit_root, from_name=None, **proof):
    """None when the process may send as `name` to `recipient`, else ONE plain line saying why. The line never carries a value the caller typed beyond the two names."""
    if not registered(name):
        return "not sent: this is not a registered system sender"
    if from_name is not None:
        return "not sent: a system sender is proved by the calling process, never named with --from"
    if not parent_is(name, kit_root, **proof):
        return "not sent: this process is not the system sender %s" % name
    if recipient not in SYSTEM_SENDERS[name]["recipients"]:
        return "not sent: the system sender %s may write only to %s" % (name, ", ".join(SYSTEM_SENDERS[name]["recipients"]))
    return None
