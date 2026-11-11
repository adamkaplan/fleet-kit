"""Remote workers: launch a coding worker onto a remote execution service through a transport.

This package is separate from the switchboard script and from the pane, headless and pod launch paths; none of them imports it. A caller that
wants a remote worker imports it where it needs it. See README.md for the transport interface and its inputs.
"""
from .transport import (  # noqa: F401
    Transport, TransportError, WorkerSpec, WorkerHandle, WorkerStatus, STATES, TERMINAL_STATES,
)
from .launch import (  # noqa: F401
    launch_remote_worker, wait_for_end, validate_spec, build_brief, load_config, Config, ConfigError, SpecError,
    register_transport, transport_from_config,
)
