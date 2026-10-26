"""Rebinding a moved name for a test: the one place that follows a name to every support module that holds it.

A moved fake or helper reads `sb` (or another moved global) from its own module, not from the test module, so a test that rebinds the name in the test
module (`mock.patch.dict(globals(), {"sb": module})`) must rebind it in the support modules too, or the moved code keeps reading the old object."""
import contextlib
import sys
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
