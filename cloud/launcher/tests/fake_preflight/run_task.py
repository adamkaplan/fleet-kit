#!/usr/bin/env python3
"""Runs the operator driver exactly as run_task.py does, with the repository preflight replaced by a passing fake (a test hook of the
launcher library, #665). Used by test_driver.py, which drives the driver as a subprocess."""
import os
import sys

sys.path.pop(0)   # this directory, which holds a file of the same name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import run_task  # noqa: E402

run_task.cw.PREFLIGHT_ARGV = ["sh", "-c", 'echo "preflight: preflight ok: deletion, non_fast_forward, pull_request"']
sys.exit(run_task.main())
