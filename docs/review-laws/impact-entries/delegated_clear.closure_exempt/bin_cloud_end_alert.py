bin/cloud_end_alert.py
loaded only by `cloud_end_tick` and `cloud_turn_tick` (via `load_module`), both called only by `finish_pass` (after `cloud_watch_tick`), not by the clear
