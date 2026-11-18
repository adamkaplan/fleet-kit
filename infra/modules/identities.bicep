@description('Each: name, location, tags.')
param userAssignedIdentities array
@description('Each: identityName, credentialName, issuer, subject, audiences.')
param federatedIdentityCredentials array

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
