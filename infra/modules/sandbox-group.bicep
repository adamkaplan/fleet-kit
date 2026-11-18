param location string
param sandboxGroupName string
param sandboxGroupTags object
@description('Empty means no companion connector gateway is managed.')
param connectorGatewayName string

resource sandbox 'Microsoft.App/sandboxGroups@2026-07-01' = {
  name: sandboxGroupName
  location: location
  tags: sandboxGroupTags
  properties: { enableDetailedMetrics: false }
}

resource gateway 'Microsoft.Web/connectorGateways@2024-04-01' = if (!empty(connectorGatewayName)) {
  name: connectorGatewayName
  location: location
  identity: { type: 'SystemAssigned' }
}
