bin/cloud_end_alert.py
chief_not_needed only decides whether the Chief of Staff is among the recipients of a pod-end message (#2202); it clears, releases and merges nothing. Trace: its only caller is the pass in bin/cloud_end_alert.py that builds the send list, and its value only drops the chief from a recipient list handed to the send function; no value of it feeds an authority function.
