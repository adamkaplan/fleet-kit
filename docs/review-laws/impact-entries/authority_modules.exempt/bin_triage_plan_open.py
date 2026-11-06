bin/triage_plan_open.py
merged_links reads which links a merged unit names. Trace: its callers are the triage's plan-open check in bin/fleet-switchboard, which only lists findings for people to read; nothing it returns reaches answer_authority, a caller check, a verdict or a clear.
