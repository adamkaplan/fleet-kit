param location string
param acrName string

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
