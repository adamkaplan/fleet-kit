using './main.bicep'

// Placeholders only. Copy to infra/<name>.bicepparam (gitignored) and fill in real values outside the repo.
param tenantId = '00000000-0000-0000-0000-000000000000'
param location = 'eastus2'
param postgresLocation = 'northcentralus'

param aksClusterName = 'aks-fleetkit-example'
param aksDnsPrefix = 'aks-fleetkit-example-dns'
param aksKubernetesVersion = '1.36'
param aksNodeResourceGroup = 'aks-fleetkit-example-nodes'
param agentPools = [
  {
    name: 'system'
    mode: 'System'
    vmSize: 'Standard_D2s_v5'
    osDiskSizeGB: 128
    osDiskType: 'Managed'
    count: 1
    minCount: 1
    maxCount: 2
    enableAutoScaling: true
    nodeLabels: {}
    tags: {}
  }
  {
    name: 'floorkatav7'
    mode: 'User'
    vmSize: 'Standard_D32ads_v7'
    osDiskSizeGB: 1760
    osDiskType: 'Ephemeral'
    workloadRuntime: 'KataVmIsolation'
    count: 0
    minCount: 0
    maxCount: 2
    enableAutoScaling: true
    nodeLabels: { 'spire-floor-pool': 'heavy', 'sysbox-install': 'yes' }
    nodeTaints: ['spire-floor/heavy=true:NoSchedule']
    tags: {}
  }
]

param sandboxGroupName = 'sbxg-fleetkit-example'
param sandboxGroupTags = {}
param connectorGatewayName = ''
param extraSandboxGroups = [
  { name: 'sbxg-fleetkit-research-example', tags: {}, identityId: '' }
]
param readerIdentityName = ''
param sandboxGroupIdentityIds = []
param connectorIdentityName = 'id-fleetkit-connector-example'

param acrName = 'acrflexample'
param acrCacheRules = [{ name: 'dockerhub-library', sourceRepository: 'public.ecr.aws/docker/library/*', targetRepository: 'dockerhub/library/*' }]
param keyVaultName = 'kv-fleetkit-example'

param storageAccountName = 'stfleetkitexample'
param storageTags = {}
param blobContainers = ['floor-bundles', 'floor-jobs-example', 'floor-sessions', 'floor-artifacts']
param storageQueues = ['cmd-jobs-example', 'cmd-jobs-poison-example']

param postgresServerName = 'pg-fleetkit-example'
param postgresAdminLogin = 'pgadminexample'
param postgresTags = {}
param postgresDatabases = ['db_example']
param postgresFirewallRules = [
  { name: 'office-example', startIpAddress: '203.0.113.10', endIpAddress: '203.0.113.10' }
]

param userAssignedIdentities = [
  { name: 'id-fleetkit-example', location: 'eastus2', tags: {} }
  { name: 'id-fleetkit-connector-example', location: 'eastus2', tags: {} }
]
param federatedIdentityCredentials = [
  {
    identityName: 'id-fleetkit-example'
    credentialName: 'fc-example'
    issuer: 'https://oidc.example.com/00000000-0000-0000-0000-000000000000/'
    subject: 'system:serviceaccount:example:example'
    audiences: ['api://AzureADTokenExchange']
  }
]

// Role assignment entries: { name (existing GUID), roleDefinitionId (GUID or full id), principalId, principalType, targetName, subTargetName }
param rgRoleAssignments = [
  {
    name: '00000000-0000-0000-0000-000000000001'
    roleDefinitionId: '00000000-0000-0000-0000-000000000002'
    principalId: '00000000-0000-0000-0000-000000000003'
    principalType: 'ServicePrincipal'
    targetName: ''
    subTargetName: ''
  }
]
param aksRoleAssignments = []
param acrRoleAssignments = []
param keyVaultRoleAssignments = []
param keyVaultSecretRoleAssignments = []
param storageRoleAssignments = []
param storageContainerRoleAssignments = []
param storageQueueRoleAssignments = []
param sandboxGroupRoleAssignments = []
param vmRoleAssignments = []

param budgets = [
  {
    name: 'budget-fleetkit-example'
    amount: 100
    timeGrain: 'Monthly'
    startDate: '2026-01-01T00:00:00Z'
    endDate: '2036-01-01T00:00:00Z'
    filter: {}
    notifications: {
      actual80: {
        enabled: true
        operator: 'GreaterThan'
        threshold: 80
        contactEmails: ['ops@example.com']
      }
    }
  }
]
param legacyVm = {}
