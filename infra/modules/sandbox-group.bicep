param location string
param sandboxGroupName string
param sandboxGroupTags object
@description('Empty means no companion connector gateway is managed.')
param connectorGatewayName string
param connectorGatewayId string = ''
@description('Each: name, tags, identityIds (optional array) or identityId (optional single user-assigned identity resource id).')
param extraSandboxGroups array = []
@description('Optional user-assigned identity resource ids attached to the primary sandboxGroupName.')
param sandboxGroupIdentityIds array = []

resource sandbox 'Microsoft.App/sandboxGroups@2026-07-01' = {
  name: sandboxGroupName
  location: location
  tags: sandboxGroupTags
  identity: !empty(sandboxGroupIdentityIds) ? {
    type: 'UserAssigned'
    userAssignedIdentities: toObject(sandboxGroupIdentityIds, id => id, id => {})
  } : null
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

resource extra 'Microsoft.App/sandboxGroups@2026-07-01' = [for g in extraSandboxGroups: {
  name: g.name
  location: location
  tags: g.?tags ?? {}
  identity: !empty(g.?identityIds ?? []) ? {
    type: 'UserAssigned'
    userAssignedIdentities: toObject(g.identityIds, id => id, id => {})
  } : (!empty(g.?identityId ?? '') ? {
    type: 'UserAssigned'
    userAssignedIdentities: { '${g.identityId}': {} }
  } : null)
  properties: {
    enableDetailedMetrics: false
    managementEndpoint: 'https://management.${location}.azuredevcompute.io'
  }
}]
