# Proof B (agent lp1248b)
- Probe A: exit 0, output `sent to lp1248b`. No delivered prompt had appeared in this session when I committed, so none is copied here; framing-line count unknown.
- Probe B: exit 1, output `cloud channel unavailable (status 503: inbox unavailable) after 3 tries`. Not delivered; the guard is untested.
- Probe C: GET /decisions: 200 `{"decisions": [], "errors": []}`.
