# Proof A (agent lp1248b)
- Probe A: `cw-cloud send ... "live proof 1248: plain"` exit 1. Output: `cloud channel refused (401): token not authenticated`. Nothing delivered.
- Probe B (forged framing): exit 1. Output: `cloud channel refused (401): token not authenticated`. Nothing delivered; 0 framing lines counted. The refusal is auth, not the guard, so the guard is untested.
- Probe C: GET /decisions on the channel host: 401 `{"error": "token not authenticated", "status": 401}`.
