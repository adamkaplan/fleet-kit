# Agent registry

> The frozen interface text (#906) is not in this checkout; this file carries only the additive note below until the lanes' agreement (PC-5/PC-8) is linked.

## Additive note (#1074): endpoint field for an agent without a pane

- **Endpoint field.** A row may carry `endpoint`: an address (a cloud pod, tunnel or in-cluster address) and `kind: orchestrator`. It is written only when given (no `endpoint: null`), no version bump. It is never a secret: a URL with a userinfo part or a credential-shaped query (`token`, `key`, `secret`, ...) is refused and nothing is stored. A row with an endpoint holds no pane.
- **Liveness rule.** A row with no pane is never read as dead for that reason. Reconcile on read removes it only on a whole read, audited, when the endpoint's bounded check answers `dead`; `unknown`, unreachable, failed and partial reads remove nothing. The default check answers `unknown`.
- **Route rule.** A message goes to the agent's pane when it has one (as before), else to its registry `endpoint` (`registry.route_for`).
- **One tuple.** `registry.ROW_FIELDS` is the one tuple; discovery, the pane pass's change test and the retirement record derive from it (`EXEMPT` names each exception); a completeness test pins it.
