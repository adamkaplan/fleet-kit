"""The record a launch stores from the launcher's answer (ask #2246): `_record_from_create`, which only builds a cloud record.

A part of bin/cloud_worker.py (the one that loads it and re-exports every name defined here); it holds the code that file needs so that it stays within
its size row. The text of the function is the text that stood in bin/cloud_worker.py: the only change is that a name which stayed behind there is read
as `D["name"]`, and `D` reads that file's own globals at each call, so a name patched on bin/cloud_worker.py reaches this code.
"""

D: object   # the names that stayed in bin/cloud_worker.py, set by its loader before this file runs


def _record_from_create(name, body):
    if body is None:
        raise D["CloudError"]("the launcher printed no record")
    if body.get("worker") != name:
        raise D["CloudError"]("the launcher answered for another worker")
    record = {key: body.get(key) for key in D["RECORD_FIELDS"]}
    record["idle_seconds"] = body.get("idle_seconds")   # the limits the launcher applied (ask #1474)
    record["hard_seconds"] = body.get("hard_seconds")
    if any(not isinstance(record[key], str) or not record[key] for key in ("url", "runtime_id")):
        raise D["CloudError"]("the launcher's record has no url or runtime_id")
    problem = D["endpoint_problem"](record["url"])
    if problem:
        raise D["CloudError"]("the launcher named an endpoint that is refused: %s" % problem)
    if isinstance(body.get("pod_uid"), str) and D["POD_UID_RE"].fullmatch(body["pod_uid"]):
        record["pod_uid"] = body["pod_uid"]   # the pod's identity at create time: a retire acts only on the pod that has it
    return record
