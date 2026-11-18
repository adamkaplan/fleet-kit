param location string
param sandboxGroupName string
param sandboxGroupTags object
@description('Empty means no companion connector gateway is managed.')
param connectorGatewayName string
param connectorGatewayId string = ''

resource sandbox 'Microsoft.App/sandboxGroups@2026-07-01' = {
  name: sandboxGroupName
  location: location
  tags: sandboxGroupTags
  properties: {
    enableDetailedMetrics: false
    managementEndpoint: 'https://management.${location}.azuredevcompute.io'
  }
}

resource gateway 'Microsoft.Web/connectorGateways@2026-05-01-preview' = if (!empty(connectorGatewayName)) {
  name: connectorGatewayName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: !empty(connectorGatewayId) ? { connectorGatewayId: connectorGatewayId } : {}
}
