bin/path_suites.py
gate_finding words an INFORMATION line (a pull request changes a path and names none of the suites that exercise it). Trace: its only caller is the gates finding in bin/fleet-switchboard, which prints the line for people to read and never changes a gate verdict; no role, tier, identity, verdict, clear or merge is produced or read (ask #1045).
