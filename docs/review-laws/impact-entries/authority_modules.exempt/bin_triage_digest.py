bin/triage_digest.py
gate is a rate limit on the stale digest (warm-up and interval). Trace: its only caller is the triage digest in bin/fleet-switchboard, where `allowed` decides whether a digest message is SENT now; no role, tier, verdict or clear is produced or read.
