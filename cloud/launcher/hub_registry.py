"""The hub's registry Secret: one place for its name and key names (ask #1161). The manifest cloud/k8s/cloud-hub.yaml is tested against it;
the launcher's per-pod registration imports these names."""
HUB_REGISTRY_SECRET = "cloud-hub-registry"
HUB_SECRETS_KEY = "secrets.json"   # {agent: sha256 hex of that pod's secret}
HUB_AGENTS_KEY = "agents.json"     # {agent: {url, session, password_file}}
HUB_LABEL = ("app.kubernetes.io/part-of", "cloud-hub")
