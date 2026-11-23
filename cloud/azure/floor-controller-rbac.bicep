targetScope = 'resourceGroup'

@description('Object (principal) ID of the floor-controller User-Assigned Managed Identity')
param principalId string = '00000000-0000-0000-0000-000000000000'

@description('Container Registry name')
param acrName string = 'acrExample'

@description('Key Vault name')
param kvName string = 'kv-example'

@description('Storage Account name')
param storageAccountName string = 'stfloor'

@description('ACA sandbox groups managed by floor-controller at runtime')
param sandboxGroupNames array = [
  'sbxg-floor-worker'
  'sbxg-floor-research'
  'sbxg-floor-orch-service'
]

@description('Blob containers accessed by floor-controller at runtime (floor-f1-fleet, floor-bundles, floor-sessions, floor-artifacts)')
param blobContainerNames array = [
  'floor-f1-fleet'
  'floor-bundles'
  'floor-sessions'
  'floor-artifacts'
]

@description('Existing AcrPull role assignment GUID')
param acrPullAssignmentName string = '5d56c64a-ebd1-425d-abd4-a3fdc1f5122d'

@description('Existing Key Vault Secrets User role assignment GUID')
param kvRoleAssignmentName string = 'e6938b20-a935-4cb7-99d8-88727057c1a4'

@description('Existing Storage Queue Data Contributor role assignment GUID')
param queueContributorAssignmentName string = '5cfd5199-35b1-47b9-9554-92431208515d'

@description('Existing SandboxGroup Data Owner assignment GUID for index 0')
param primarySandboxGroupAssignmentName string = '14df6205-d14e-44f0-9def-9e4f3cba7379'

var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var kvSecretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var storageBlobDataContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
var storageBlobDelegatorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'db58b8e5-c6ad-4a2a-8342-4190687cbf4a')
var storageQueueDataContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '974c5e8b-45b9-4653-ba55-5f855dd0fb88')
var sandboxGroupDataOwnerRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'c24cf47c-5077-412d-a19c-45202126392c')

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: kvName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' existing = {
  parent: storage
  name: 'default'
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' existing = [for name in blobContainerNames: {
  parent: blobServices
  name: name
}]

resource sandboxGroups 'Microsoft.App/sandboxGroups@2024-02-02-preview' existing = [for name in sandboxGroupNames: {
  name: name
}]

// 1. AcrPull ONLY (no AcrPush, no RG Contributor)
resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: acrPullAssignmentName
  scope: acr
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: acrPullRoleId
  }
}

// 2. Key Vault Secrets User
resource kvSecretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: kvRoleAssignmentName
  scope: kv
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: kvSecretsUserRoleId
  }
}

// 3. Storage Queue Data Contributor
resource queueContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: queueContributorAssignmentName
  scope: storage
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageQueueDataContributorRoleId
  }
}

// 4. Storage Blob Delegator (replaces RG Contributor for generateUserDelegationKey)
resource blobDelegatorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, 'blob-delegator', principalId, storageBlobDelegatorRoleId)
  scope: storage
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageBlobDelegatorRoleId
  }
}

// 5. Storage Blob Data Contributor per-container on floor-f1-fleet, floor-bundles, floor-sessions, floor-artifacts
resource blobContainerAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in blobContainerNames: {
  name: name == 'floor-sessions' ? 'eddd41ca-97fe-5e2b-a1bf-1bfc8361f8f1' : (name == 'floor-artifacts' ? '329d56ac-1e3f-56bd-8d98-5b5c8e028860' : guid(storage.id, 'blob-container', name, principalId, storageBlobDataContributorRoleId))
  scope: containers[i]
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: storageBlobDataContributorRoleId
  }
}]

// 6. Container Apps SandboxGroup Data Owner scoped per-sandboxGroup (no RG-wide Contributor or SandboxGroup Contributor)
resource sandboxGroupDataOwnerAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in sandboxGroupNames: {
  name: i == 0 ? primarySandboxGroupAssignmentName : guid(resourceGroup().id, 'sbxg', name, principalId, sandboxGroupDataOwnerRoleId)
  scope: sandboxGroups[i]
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: sandboxGroupDataOwnerRoleId
  }
}]
