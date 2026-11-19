@description('Each: name, location, tags.')
param userAssignedIdentities array
@description('Each: identityName, credentialName, issuer, subject, audiences.')
param federatedIdentityCredentials array

@description('Name of the reader identity whose principal id is output; empty for none.')
param readerIdentityName string = ''

resource ids 'Microsoft.ManagedIdentity/userAssignedIdentities@2025-05-31-preview' = [for i in userAssignedIdentities: {
  name: i.name
  location: i.location
  tags: i.tags
  properties: { isolationScope: 'None' }
}]

@batchSize(1)
resource creds 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2025-05-31-preview' = [for c in federatedIdentityCredentials: {
  name: '${c.identityName}/${c.credentialName}'
  properties: { issuer: c.issuer, subject: c.subject, audiences: c.audiences }
  dependsOn: [ ids ]
}]

var identityNames = [for i in userAssignedIdentities: i.name]
output readerIdentityPrincipalId string = empty(readerIdentityName) || !contains(identityNames, readerIdentityName) ? '' : ids[indexOf(identityNames, readerIdentityName)].properties.principalId
