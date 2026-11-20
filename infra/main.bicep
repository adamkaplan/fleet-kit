// Fleet infrastructure, resource-group scope. Every private value is a parameter.
// Deploy ONLY with --mode Incremental (see README.md).
targetScope = 'resourceGroup'

param tenantId string
param location string
param postgresLocation string

param aksClusterName string
param aksDnsPrefix string
param aksKubernetesVersion string
@description('AKS-owned node resource group. Named only so AKS keeps it; Bicep never deploys into it.')
param aksNodeResourceGroup string
param agentPools array
param aksMaintenanceStartTime string = '07:00'
param aksMaintenanceUtcOffset string = '+00:00'
param aksMaintenanceStartDate string = '2026-11-11'
param aksExtraProperties object = {}

param sandboxGroupName string
param sandboxGroupTags object = {}
param connectorGatewayName string = ''
param connectorGatewayId string = ''
@description('Per-profile sandbox groups. Each: name, tags, identityId (optional).')
param extraSandboxGroups array = []
@description('Optional user-assigned identity resource ids attached to the primary sandboxGroupName.')
param sandboxGroupIdentityIds array = []
@description('Name of the role-less connector user-assigned identity (in userAssignedIdentities); empty for none.')
param connectorIdentityName string = ''
@description('Name of the reader user-assigned identity (in userAssignedIdentities); empty for none.')
param readerIdentityName string = ''

param acrName string
@description('ACR pull-through cache rules. Each: name, sourceRepository, targetRepository, credentialSetResourceId (optional).')
param acrCacheRules array = [
  {
    name: 'dockerhub-library'
    sourceRepository: 'public.ecr.aws/docker/library/*'
    targetRepository: 'dockerhub/library/*'
  }
]
param keyVaultName string

param storageAccountName string
param storageTags object = {}
param blobContainers array
param storageQueues array
param blobContainerMetadata object = {}

param postgresServerName string
param postgresAdminLogin string
param postgresTags object = {}
param postgresDatabases array
param postgresFirewallRules array

param userAssignedIdentities array
param federatedIdentityCredentials array

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

param budgets array
param legacyVm object = {}

module aks 'modules/aks.bicep' = {
  name: 'aks'
  params: {
    location: location
    tenantId: tenantId
    aksClusterName: aksClusterName
    aksDnsPrefix: aksDnsPrefix
    aksKubernetesVersion: aksKubernetesVersion
    aksNodeResourceGroup: aksNodeResourceGroup
    agentPools: agentPools
    maintenanceStartTime: aksMaintenanceStartTime
    maintenanceUtcOffset: aksMaintenanceUtcOffset
    maintenanceStartDate: aksMaintenanceStartDate
    aksExtraProperties: aksExtraProperties
  }
}

module sandbox 'modules/sandbox-group.bicep' = {
  name: 'sandbox-group'
  params: {
    location: location
    sandboxGroupName: sandboxGroupName
    sandboxGroupTags: sandboxGroupTags
    sandboxGroupIdentityIds: sandboxGroupIdentityIds
    connectorGatewayName: connectorGatewayName
    connectorGatewayId: connectorGatewayId
    extraSandboxGroups: extraSandboxGroups
  }
}

module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: { location: location, acrName: acrName, acrCacheRules: acrCacheRules }
}

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  params: { location: location, tenantId: tenantId, keyVaultName: keyVaultName }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    storageAccountName: storageAccountName
    storageTags: storageTags
    blobContainers: blobContainers
    storageQueues: storageQueues
    blobContainerMetadata: blobContainerMetadata
  }
}

module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    postgresLocation: postgresLocation
    postgresServerName: postgresServerName
    postgresAdminLogin: postgresAdminLogin
    postgresTags: postgresTags
    postgresDatabases: postgresDatabases
    postgresFirewallRules: postgresFirewallRules
  }
}

module identities 'modules/identities.bicep' = {
  name: 'identities'
  params: {
    userAssignedIdentities: userAssignedIdentities
    federatedIdentityCredentials: federatedIdentityCredentials
    readerIdentityName: readerIdentityName
    connectorIdentityName: connectorIdentityName
  }
}

module rbac 'modules/rbac.bicep' = {
  name: 'rbac'
  dependsOn: [ aks, acr, keyvault, storage, sandbox, identities ]
  params: {
    aksClusterName: aksClusterName
    acrName: acrName
    keyVaultName: keyVaultName
    storageAccountName: storageAccountName
    sandboxGroupName: sandboxGroupName
    legacyVmName: legacyVm.?name ?? ''
    rgRoleAssignments: rgRoleAssignments
    aksRoleAssignments: aksRoleAssignments
    acrRoleAssignments: acrRoleAssignments
    keyVaultRoleAssignments: keyVaultRoleAssignments
    keyVaultSecretRoleAssignments: keyVaultSecretRoleAssignments
    storageRoleAssignments: storageRoleAssignments
    storageContainerRoleAssignments: storageContainerRoleAssignments
    storageQueueRoleAssignments: storageQueueRoleAssignments
    sandboxGroupRoleAssignments: sandboxGroupRoleAssignments
    vmRoleAssignments: vmRoleAssignments
  }
}

module budget 'modules/budget.bicep' = {
  name: 'budget'
  params: { budgets: budgets }
}

module legacyVmMonitoring 'modules/legacy-vm-monitoring.bicep' = if (!empty(legacyVm)) {
  name: 'legacy-vm-monitoring'
  params: { location: location, legacyVm: legacyVm }
}

output readerIdentityPrincipalId string = identities.outputs.readerIdentityPrincipalId
output connectorIdentityId string = identities.outputs.connectorIdentityId
