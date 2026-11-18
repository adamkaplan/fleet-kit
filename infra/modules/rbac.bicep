// Role assignments keep their existing GUID names so what-if matches them.
// Each entry: name, roleDefinitionId, principalId, principalType, targetName, subTargetName.
param aksClusterName string
param acrName string
param keyVaultName string
param storageAccountName string
param sandboxGroupName string
param legacyVmName string
param rgRoleAssignments array
param aksRoleAssignments array
param acrRoleAssignments array
param keyVaultRoleAssignments array
param keyVaultSecretRoleAssignments array
param storageRoleAssignments array
param storageContainerRoleAssignments array
param storageQueueRoleAssignments array
param sandboxGroupRoleAssignments array
param vmRoleAssignments array

resource aks 'Microsoft.ContainerService/managedClusters@2026-05-02-preview' existing = { name: aksClusterName }
resource acr 'Microsoft.ContainerRegistry/registries@2026-03-01-preview' existing = { name: acrName }
resource vault 'Microsoft.KeyVault/vaults@2026-03-01-preview' existing = { name: keyVaultName }
resource sa 'Microsoft.Storage/storageAccounts@2026-06-01' existing = { name: storageAccountName }
resource sandbox 'Microsoft.App/sandboxGroups@2026-07-01' existing = { name: sandboxGroupName }
resource vm 'Microsoft.Compute/virtualMachines@2026-04-01' existing = if (!empty(legacyVmName)) { name: legacyVmName }
resource blob 'Microsoft.Storage/storageAccounts/blobServices@2026-06-01' existing = { parent: sa, name: 'default' }
resource queueSvc 'Microsoft.Storage/storageAccounts/queueServices@2026-06-01' existing = { parent: sa, name: 'default' }

// roleDefinitionId is the full resource id, or a bare GUID of a built-in role (documented in README).
func roleId(id string) string => contains(id, '/') ? id : subscriptionResourceId('Microsoft.Authorization/roleDefinitions', id)

resource rg 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in rgRoleAssignments: {
  name: a.name
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource onAks 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in aksRoleAssignments: {
  name: a.name
  scope: aks
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource onAcr 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in acrRoleAssignments: {
  name: a.name
  scope: acr
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource onVault 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in keyVaultRoleAssignments: {
  name: a.name
  scope: vault
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource secret 'Microsoft.KeyVault/vaults/secrets@2026-03-01-preview' existing = [for a in keyVaultSecretRoleAssignments: {
  parent: vault
  name: a.subTargetName
}]

resource onSecret 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (a, i) in keyVaultSecretRoleAssignments: {
  name: a.name
  scope: secret[i]
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource onStorage 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in storageRoleAssignments: {
  name: a.name
  scope: sa
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-06-01' existing = [for a in storageContainerRoleAssignments: {
  parent: blob
  name: a.subTargetName
}]

resource onContainer 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (a, i) in storageContainerRoleAssignments: {
  name: a.name
  scope: container[i]
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource queue 'Microsoft.Storage/storageAccounts/queueServices/queues@2026-06-01' existing = [for a in storageQueueRoleAssignments: {
  parent: queueSvc
  name: a.subTargetName
}]

resource onQueue 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (a, i) in storageQueueRoleAssignments: {
  name: a.name
  scope: queue[i]
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource onSandbox 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in sandboxGroupRoleAssignments: {
  name: a.name
  scope: sandbox
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]

resource onVm 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for a in vmRoleAssignments: {
  name: a.name
  scope: vm
  properties: { roleDefinitionId: roleId(a.roleDefinitionId), principalId: a.principalId, principalType: a.principalType }
}]
