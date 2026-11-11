bin/agent_calls.py
release_notice gives back a once-an-hour message claim for an account's budget notice. Trace: its only caller is the budget notice command in bin/fleet-switchboard, which calls it after claim_notice when the reminder could not be added; it truncates a rate-limit file in the state directory and returns nothing, so no role, tier, identity, verdict or clear is produced or read.
