"""The names every switchboard command needs: the program name and the exit codes (fix for #1811).

A command's glue imports this module (`load_module("switchboard_names")`) instead of reading these names from the entry's
namespace, so adding a command costs no `namespace` or `entry-import` row in docs/architecture.md. Standard library only.
The entry binds its own `PROG` and `EXIT_*` from here, with the same values as before.
"""

PROG = "fleet-switchboard"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_PARTIAL = 4   # `prove`: at least one named scenario was not recorded (a usage error is 2)
EXIT_HELD = 3   # `send`: the delivery rule says hold; nothing was sent
EXIT_MEMORY = 5   # `launch`: the memory test refused a NEW launch (the machine cannot take it); `--memory-ok <reason>` overrides
EXIT_CRASH = 70  # the `crash-after-send` fault stopped the process, as a crash would
