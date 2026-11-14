"""A fake transport for tests and for trying a caller without a remote service. In memory; it makes no outside call."""
from typing import Dict, List, Tuple

from .transport import TERMINAL_STATES, TransportError, WorkerHandle, WorkerSpec, WorkerStatus


class FakeTransport:
    name = "fake"

    def __init__(self):
        self.workers: Dict[str, dict] = {}
        self.calls: List[Tuple[str, str]] = []
        self.fail_next: Dict[str, TransportError] = {}   # method name -> the error its next call raises

    def _maybe_fail(self, method: str):
        err = self.fail_next.pop(method, None)
        if err:
            raise err

    def start(self, spec: WorkerSpec) -> WorkerHandle:
        self.calls.append(("start", spec.name))
        self._maybe_fail("start")
        if spec.name not in self.workers:
            self.workers[spec.name] = {"spec": spec, "state": "queued", "last_message": "", "landed_sha": "", "messages": [], "ids": set()}
        return WorkerHandle(name=spec.name, worker_id="fake-" + spec.name, transport=self.name)

    def _get(self, handle: WorkerHandle) -> dict:
        w = self.workers.get(handle.name)
        if w is None or handle.worker_id != "fake-" + handle.name:
            raise TransportError("unknown worker")
        return w

    def send(self, handle: WorkerHandle, text: str, message_id=None) -> None:
        self.calls.append(("send", handle.name))
        self._maybe_fail("send")
        w = self._get(handle)
        if w["state"] in TERMINAL_STATES:
            raise TransportError("worker has ended")
        if message_id is not None:
            if message_id in w["ids"]:
                return
            w["ids"].add(message_id)
        w["messages"].append(text)

    def status(self, handle: WorkerHandle) -> WorkerStatus:
        self.calls.append(("status", handle.name))
        self._maybe_fail("status")
        w = self._get(handle)
        return WorkerStatus(state=w["state"], busy=w["state"] in ("queued", "running"), last_update="2026-01-01T00:00:00Z", landed_sha=w["landed_sha"])

    def last_message(self, handle: WorkerHandle) -> str:
        self.calls.append(("last_message", handle.name))
        self._maybe_fail("last_message")
        return self._get(handle)["last_message"]

    def stop(self, handle: WorkerHandle, reason: str) -> None:
        self.calls.append(("stop", handle.name))
        self._maybe_fail("stop")
        w = self.workers.get(handle.name)
        if w is not None and w["state"] not in TERMINAL_STATES:
            w["state"] = "retired"

    def close(self) -> None:
        self.calls.append(("close", ""))

    # test helper: the remote side finishes
    def finish(self, name: str, state: str = "done", last_message: str = "", landed_sha: str = "") -> None:
        self.workers[name].update(state=state, last_message=last_message, landed_sha=landed_sha)
