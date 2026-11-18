// The existing deallocated VM, its network and its metric alerts, captured as-is.
// Every property comes from the legacyVm object: name, nsg, vnet, publicIp, nic, vm, shutdownSchedule, actionGroup, alerts.
// Each part is optional: a missing key means that resource is not managed.
param location string
param legacyVm object

resource nsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = if (contains(legacyVm, 'nsg')) {
  name: legacyVm.nsg.name
  location: location
  tags: legacyVm.nsg.?tags
  properties: legacyVm.nsg.properties
}

resource vnet 'Microsoft.Network/virtualNetworks@2025-09-01' = if (contains(legacyVm, 'vnet')) {
  name: legacyVm.vnet.name
  location: location
  tags: legacyVm.vnet.?tags
  properties: legacyVm.vnet.properties
  dependsOn: [ nsg ]
}

resource pip 'Microsoft.Network/publicIPAddresses@2025-09-01' = if (contains(legacyVm, 'publicIp')) {
  name: legacyVm.publicIp.name
  location: location
  sku: legacyVm.publicIp.sku
  tags: legacyVm.publicIp.?tags
  properties: legacyVm.publicIp.properties
}

resource nic 'Microsoft.Network/networkInterfaces@2025-09-01' = if (contains(legacyVm, 'nic')) {
  name: legacyVm.nic.name
  location: location
  tags: legacyVm.nic.?tags
  properties: legacyVm.nic.properties
  dependsOn: [ nsg, vnet, pip ]
}

resource vm 'Microsoft.Compute/virtualMachines@2026-04-01' = if (!empty(legacyVm.?name ?? '')) {
  name: legacyVm.?name ?? 'unused'
  location: location
  tags: legacyVm.vm.?tags
  properties: legacyVm.vm.properties
  dependsOn: [ nic ]
}

resource shutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = if (contains(legacyVm, 'shutdownSchedule')) {
  name: legacyVm.shutdownSchedule.name
  location: location
  properties: legacyVm.shutdownSchedule.properties
  dependsOn: [ vm ]
}

resource actionGroup 'microsoft.insights/actionGroups@2024-10-01-preview' = if (contains(legacyVm, 'actionGroup')) {
  name: legacyVm.actionGroup.name
  location: 'global'
  properties: legacyVm.actionGroup.properties
}

resource alerts 'Microsoft.Insights/metricAlerts@2026-01-01' = [for a in (legacyVm.?alerts ?? []): {
  name: a.name
  location: 'global'
  tags: a.?tags
  properties: a.properties
  dependsOn: [ actionGroup ]
}]
