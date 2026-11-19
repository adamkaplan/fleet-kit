param location string
param storageAccountName string
param storageTags object
param blobContainers array
param storageQueues array
param blobContainerMetadata object = {}

resource sa 'Microsoft.Storage/storageAccounts@2026-06-01' = {
  name: storageAccountName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  tags: storageTags
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    minimumTlsVersion: 'TLS1_2'
    publicNetworkAccess: 'Enabled'
    supportsHttpsTrafficOnly: true
    networkAcls: {
      bypass: 'None'
      defaultAction: 'Allow'
      ipRules: []
      ipv6Rules: []
      virtualNetworkRules: []
    }
  }
}

resource blob 'Microsoft.Storage/storageAccounts/blobServices@2026-06-01' = {
  parent: sa
  name: 'default'
  properties: {
    deleteRetentionPolicy: { allowPermanentDelete: false, enabled: false }
    staticWebsite: { enabled: false }
  }
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-06-01' = [for c in blobContainers: {
  parent: blob
  name: c
  properties: {
    publicAccess: 'None'
    defaultEncryptionScope: '$account-encryption-key'
    denyEncryptionScopeOverride: false
    metadata: blobContainerMetadata[?c]
  }
}]

resource queueSvc 'Microsoft.Storage/storageAccounts/queueServices@2026-06-01' = {
  parent: sa
  name: 'default'
}

resource queues 'Microsoft.Storage/storageAccounts/queueServices/queues@2026-06-01' = [for q in storageQueues: {
  parent: queueSvc
  name: q
}]

resource fileSvc 'Microsoft.Storage/storageAccounts/fileServices@2026-06-01' = {
  parent: sa
  name: 'default'
  properties: {
    protocolSettings: { smb: { directoryLease: { enabled: false } } }
    shareDeleteRetentionPolicy: { days: 7, enabled: true }
  }
}

resource tableSvc 'Microsoft.Storage/storageAccounts/tableServices@2026-06-01' = {
  parent: sa
  name: 'default'
}
