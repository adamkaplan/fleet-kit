# cp1480c prover notes (ask #1480, heads f6fd9d27 and 43c87804)
- test_cloud_end_alert + test_stop_cause: 81 tests OK at both heads; 166 assertEqual pairs identical.
- Mutants: 13 of 13 end-alert mutations applied; 20 of 20 launcher mutant count asserts.
- S18 tests (registry inventory, delegated-clear list, cloud watch tick, end tick without state dir): pass at both heads.
- U3/U1: test_chain_time FAILS at f6fd9d27 (no rows for test_stop_cause.py, test_cloud_end_alert.py); passes at 43c87804.
- test_cloud_k1, test_cloud_worker, test_cw_names fail identically at the integration tip (pod environment).
- Not run: chain entries after test_driver, chain at 43c87804, full switchboard suite.
