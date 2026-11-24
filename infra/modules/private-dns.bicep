// Private DNS zone with VNet links and A records.
param privateDns object

resource zone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: privateDns.zoneName
  location: 'global'
  tags: privateDns.?tags ?? {}
}

resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for link in (privateDns.?vnetLinks ?? []): {
  parent: zone
  name: link.name
  location: 'global'
  properties: {
    registrationEnabled: link.?registrationEnabled ?? false
    virtualNetwork: {
      id: link.vnetId
    }
  }
}]

resource aRecords 'Microsoft.Network/privateDnsZones/A@2024-06-01' = [for rec in (privateDns.?aRecords ?? []): {
  parent: zone
  name: rec.name
  properties: {
    ttl: rec.?ttl ?? 60
    aRecords: [
      {
        ipv4Address: rec.ipv4Address
      }
    ]
  }
}]
