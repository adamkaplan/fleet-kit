"""Rebinding a moved name for a test: the one place that follows a name to every support module that holds it.

A moved fake or helper reads `sb` (or another moved global) from its own module, not from the test module, so a test that rebinds the name in the test
module (`mock.patch.dict(globals(), {"sb": module})`) must rebind it in the support modules too, or the moved code keeps reading the old object."""
import contextlib
import sys
from pathlib import Path
from unittest import mock


@contextlib.contextmanager
def rebound(name, value):
    """For the length of the block, `name` is `value` in every support module that has a global of that name."""
    modules = [module for key, module in list(sys.modules.items()) if key.startswith("support.") and name in vars(module)]
    if not modules:
        raise LookupError("no support module has a global named %r: a typo, or the module is not imported yet" % name)
    with contextlib.ExitStack() as stack:
        for module in modules:
            stack.enter_context(mock.patch.object(module, name, value))
        yield


def spliced_head(cache, src, real_ast):
    """The tree of `src`, the daemon file with a few lines changed: when it has as many lines as the real file, only the module-level
    definitions that hold a changed line are parsed again (at their own line numbers) and put in place of theirs in the real tree; the rest of the
    file is the same text. Another shape is parsed whole. A head that differs only by trailing ` # x` comments IS the real tree (and its closure)."""
    base, head = cache["source"].split("\n"), src.split("\n")
    tree = None
    if len(base) == len(head):
        changed = [k + 1 for k, (a, b) in enumerate(zip(base, head)) if a != b]
        if all((head[k - 1].endswith("  # x") and head[k - 1][:-5] == base[k - 1])
               or (not base[k - 1].strip() and head[k - 1].lstrip().startswith("#")) for k in changed):
            cache.setdefault("same_closure_ids", set()).add(id(cache["tree"]))
            tree = cache["tree"]
        else:
            body = cache["tree"].body
            span = lambda n: (min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])]), n.end_lineno)
            hit = [i for i, n in enumerate(body) if any(span(n)[0] <= k <= span(n)[1] for k in changed)]
            if hit and len(hit) == hit[-1] - hit[0] + 1 and all(any(span(body[i])[0] <= k <= span(body[i])[1] for i in hit) for k in changed):
                first, last = span(body[hit[0]])[0], span(body[hit[-1]])[1]
                try:
                    parsed = real_ast.parse("\n" * (first - 1) + "\n".join(head[first - 1:last])).body
                    tree = real_ast.Module(body=body[:hit[0]] + parsed + body[hit[-1] + 1:], type_ignores=[])
                except SyntaxError:
                    tree = None
    elif len(head) > len(base) and head[:len(base)] == base:
        try:
            tail = real_ast.parse("\n" * len(base) + "\n".join(head[len(base):])).body
            tree = real_ast.Module(body=list(cache["tree"].body) + tail, type_ignores=[])
            cache.setdefault("same_closure_ids", set()).add(id(tree))
        except SyntaxError:
            tree = None
    if tree is None:
        return real_ast.parse(src)
    cache["heads"][src] = tree
    return tree


_REAL_SHIM = Path(__file__).resolve().parents[3] / "shims" / "gh"
SHIM = _REAL_SHIM
_REAL_SHIM_CODE = []   # the compiled real shim, once per process


def _shim_code(shim_path=None):
    path = Path(shim_path or SHIM)
    if path.resolve() != _REAL_SHIM:   # a planted mutant of the shim
        return compile(path.read_text(), str(path), "exec")
    if not _REAL_SHIM_CODE:
        _REAL_SHIM_CODE.append(compile(_REAL_SHIM.read_text(), str(_REAL_SHIM), "exec"))
    return _REAL_SHIM_CODE[0]


def run_in_process(runner, args, environ, stdin="", shim_path=None, setup=None, cwd=None):
    """`_run_in_process`, in the directory `cwd` when one is given (the shim and the children it runs see it)."""
    import os
    before = os.getcwd()
    try:
        if cwd is not None:
            os.chdir(str(cwd))
        return _run_in_process(runner, args, environ, stdin, shim_path, setup)
    finally:
        os.chdir(before)


def _run_in_process(runner, args, environ, stdin="", shim_path=None, setup=None):
    """The runner text of a shim test run in this process (no interpreter start) as `python -c runner *args` in `environ`: the CompletedProcess it
    would have given. The shim's exec of the real gh becomes a child run whose output is kept. With `runner` None the shim file itself is run:
    its code is executed as a module that is not __main__, `setup(namespace)` may set its test seams, and its `main(args)` is called."""
    import contextlib, io, os, subprocess, sys, types
    from unittest import mock
    class Stream(io.StringIO):   # text and bytes written to one stream, in order (the shim writes bodies to sys.stdout.buffer)
        @property
        def buffer(self):
            return types.SimpleNamespace(write=lambda data: self.write(bytes(data).decode("utf-8", "replace")), flush=lambda: None)
    out, err, code = Stream(), Stream(), 0

    def child(path, argv, env=None):
        done = subprocess.run([path] + list(argv[1:]), capture_output=True, text=True, env=env if env is not None else os.environ,
                              input=sys.stdin.read() if stdin else None)
        out.write(done.stdout)
        err.write(done.stderr)
        raise SystemExit(done.returncode)
    real_run, real_popen = subprocess.run, subprocess.Popen

    def text_of(value):
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")

    def run(a, *rest, **kw):   # a child that would inherit this process's output: its output is kept here
        if "stdout" in kw or kw.get("capture_output"):
            return real_run(a, *rest, **kw)
        kw.setdefault("stderr", subprocess.PIPE)
        res = real_run(a, *rest, stdout=subprocess.PIPE, **kw)
        out.write(text_of(res.stdout))
        err.write(text_of(res.stderr))
        return res

    def popen(a, *rest, **kw):
        if "stdout" in kw or "stderr" in kw:
            return real_popen(a, *rest, **kw)
        p = real_popen(a, *rest, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        real_wait = p.wait

        def wait(*w, **k):
            result = real_wait(*w, **k)
            out.write(text_of(p.stdout.read()))
            err.write(text_of(p.stderr.read()))
            return result
        p.wait = wait
        return p
    with mock.patch.dict(os.environ, environ, clear=True), mock.patch.object(sys, "argv", ["-c"] + list(args)), mock.patch.object(os, "execv", child), \
            mock.patch.object(os, "execve", lambda path, argv, env: child(path, argv, env)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.object(sys, "stdin", io.StringIO(stdin)), mock.patch.object(subprocess, "run", run), mock.patch.object(subprocess, "Popen", popen):
        try:
            if runner is None:
                ns = {"__name__": "__mp_main__", "__file__": str(shim_path or SHIM)}
                exec(_shim_code(shim_path), ns)
                if setup is not None:
                    setup(ns)
                result = ns["main"](list(args))
                if result:
                    raise SystemExit(result)
            else:
                exec(compile(runner, "<runner>", "exec"), {"__name__": "__main__"})
        except SystemExit as stop:
            code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
            if isinstance(stop.code, str):
                err.write(stop.code + "\n")
    return subprocess.CompletedProcess(["gh"] + list(args), code, out.getvalue(), err.getvalue())
