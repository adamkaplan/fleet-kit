param location string
param acrName string

@description('ACR pull-through cache rules. Each: name, sourceRepository, targetRepository, credentialSetResourceId (optional).')
param acrCacheRules array = []

resource acr 'Microsoft.ContainerRegistry/registries@2026-03-01-preview' = {
  name: acrName
  location: location
  sku: { name: 'Standard' }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    dataEndpointEnabled: false
    publicNetworkAccess: 'Enabled'
    networkRuleBypassOptions: 'AzureServices'
    networkRuleBypassAllowedForTasks: false
    zoneRedundancy: 'Disabled'
    encryption: { status: 'disabled' }
    endpointProtocol: 'IPv4'
    regionalEndpoints: 'Disabled'
    roleAssignmentMode: 'LegacyRegistryPermissions'
    writableCacheRepos: 'Disabled'
  }
}

resource cacheRules 'Microsoft.ContainerRegistry/registries/cacheRules@2023-07-01' = [for rule in acrCacheRules: {
  parent: acr
  name: rule.name
  properties: union({
    sourceRepository: rule.sourceRepository
    targetRepository: rule.targetRepository
  }, !empty(rule.?credentialSetResourceId ?? '') ? {
    credentialSetResourceId: rule.credentialSetResourceId
  } : {})
}]
