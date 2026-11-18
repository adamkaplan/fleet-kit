# infra: the fleet's Azure resources in Bicep

Everything the fleet runs on in one resource group is defined here, and every change goes through it. Every private
value (subscription, tenant, names, principal ids, IPs, hosts) is a parameter. `example.bicepparam` holds placeholders only.

## Rules

- **Incremental mode only.** Always `--mode Incremental`. NEVER `--mode Complete`: it deletes anything these files miss. Do not run it, and refuse any request to.
- First capture, then manage: before any deploy, `what-if` against today's resources must show zero `Create`, `Delete` and `Modify`.
- Never commit a real `.bicepparam`. `infra/*.bicepparam` is gitignored except `example.bicepparam`.

## Resource groups

- **Fleet resource group** (`<fleet-rg>`, region `<location>`): holds every fleet-managed resource. The PostgreSQL flexible server in it has its own `postgresLocation`, which may differ from the group's region.
- **AKS node resource group** (`nodeResourceGroup`, for example `<aks-name>-nodes`): owned and reconciled only by the AKS resource provider. **Bicep must NEVER target the AKS node resource group.** It is only named in the cluster's `nodeResourceGroup` property.

## Real parameters

The real parameter file is the Key Vault secret `infra-live-bicepparam` in the fleet Key Vault, so a cloud deploy can read it.
No new identity gets Key Vault access for it: only identities that already have it can read it (the operator, `Key Vault Secrets Officer`; the existing workload identity, `Key Vault Secrets User`). Writes to `infra-live-bicepparam` are done by the operator only.

```
az keyvault secret show --vault-name <vault> --name infra-live-bicepparam --query value -o tsv > infra/live.bicepparam
# update (operator only):
az keyvault secret set --vault-name <vault> --name infra-live-bicepparam --file infra/live.bicepparam
rm infra/live.bicepparam   # when done
```

Role definitions are passed as a full id or a bare GUID of a built-in role (these are public Azure ids, supplied through parameters).

## What-if

```
az deployment group what-if -g <fleet-rg> -f infra/main.bicep -p infra/live.bicepparam \
  --mode Incremental --exclude-change-types Ignore NoChange
```

`Ignore`, `NoChange` and `NoEffect` are the what-if change types that carry no change; the CLI flag takes `Ignore` and `NoChange`.

Expect no `Create`, `Delete` or `Modify`. Otherwise fix the parameters (not the live resources) until it is clean.

## Deploy

```
az deployment group create -g <fleet-rg> -f infra/main.bicep -p infra/live.bicepparam --mode Incremental
```

Never `--mode Complete`. Smoke checks afterwards: repeat the what-if (zero changes), `az aks show -g <fleet-rg> -n <aks>` reports `Succeeded`, `az acr show`, `az keyvault show` and `az postgres flexible-server show` still answer, and a worker pod still starts.

## Rollback

Deployments are incremental, so nothing is removed by a bad one. Revert the commit (and, operator only, set the Key Vault secret back to its earlier value), then run what-if and deploy again with `--mode Incremental`. A resource a bad deploy created is deleted by hand after checking it by name.

## Inputs and outside calls

| Input or call | Test | Run-by-hand mutant |
|---|---|---|
| `infra/main.bicep` and `infra/modules/*.bicep` (resource types) | `test_infra_bicep.py` `TestFiles` | delete a `resource` line from a module copy; the check names the missing type |
| Any UUID or private name in `infra/` | `TestNoPrivate` | add a non-placeholder UUID to a scratch copy; the check names the file |
| `infra/example.bicepparam` vs required `param`s | `TestParams` | remove one `param` line from a scratch copy; the check names it |
| `.gitignore` for `infra/*.bicepparam` | `TestGitignore` | `git check-ignore infra/x.bicepparam` must exit 0 |
| `az bicep build` / `bicep build-params` (when installed) | `TestBicepCli` | drop a required param, run `bicep build-params`; it fails |
| `az deployment group what-if` / `create` (not run by tests) | `TestReadme` checks the documented commands | change `--mode Incremental` to `Complete` in the text; the test fails |
| Key Vault secret `infra-live-bicepparam` (not run by tests) | `TestReadme` | delete the section; the test fails |
