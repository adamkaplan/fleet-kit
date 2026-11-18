param postgresLocation string
param postgresServerName string
param postgresAdminLogin string
param postgresTags object
param postgresDatabases array
@description('Each rule: name, startIpAddress, endIpAddress.')
param postgresFirewallRules array

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2026-04-01-preview' = {
  name: postgresServerName
  location: postgresLocation
  sku: { name: 'Standard_B1ms', tier: 'Burstable' }
  tags: postgresTags
  properties: {
    version: '16'
    dataEncryption: { type: 'SystemManaged' }
    fipsMode: 'Disabled'
    replica: { role: 'Primary' }
    replicationRole: 'Primary'
    administratorLogin: postgresAdminLogin
    authConfig: { activeDirectoryAuth: 'Disabled', passwordAuth: 'Enabled' }
    storage: { storageSizeGB: 32, autoGrow: 'Disabled', tier: 'P4', iops: 120, type: 'Premium_LRS' }
    backup: { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability: { mode: 'Disabled' }
    network: { publicNetworkAccess: 'Enabled' }
    maintenanceWindow: { customWindow: 'Disabled', dayOfWeek: 0, startHour: 0, startMinute: 0 }
  }
}

resource dbs 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2026-04-01-preview' = [for d in postgresDatabases: {
  parent: pg
  name: d
  properties: { charset: 'UTF8', collation: 'en_US.utf8' }
}]

@batchSize(1)
resource rules 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2026-04-01-preview' = [for r in postgresFirewallRules: {
  parent: pg
  name: r.name
  properties: {
    startIpAddress: r.startIpAddress
    endIpAddress: r.endIpAddress
    sourceAddressPrefixes: r.?sourceAddressPrefixes
  }
}]

resource atp 'Microsoft.DBforPostgreSQL/flexibleServers/advancedThreatProtectionSettings@2026-04-01-preview' = {
  parent: pg
  name: 'Default'
  properties: { state: 'Enabled' }
}
