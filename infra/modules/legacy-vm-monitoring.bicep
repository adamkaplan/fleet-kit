// The existing deallocated VM, its network and its metric alerts, captured as-is.
// Every property comes from the legacyVm object: name, nsg, vnet, publicIp, nic, vm, shutdownSchedule, actionGroup, alerts.
param location string
param legacyVm object

resource nsg 'Microsoft.Network/networkSecurityGroups@2025-09-01' = {
  name: legacyVm.nsg.name
  location: location
  properties: legacyVm.nsg.properties
}

resource vnet 'Microsoft.Network/virtualNetworks@2025-09-01' = {
  name: legacyVm.vnet.name
  location: location
  properties: legacyVm.vnet.properties
}

resource pip 'Microsoft.Network/publicIPAddresses@2025-09-01' = {
  name: legacyVm.publicIp.name
  location: location
  sku: legacyVm.publicIp.sku
  properties: legacyVm.publicIp.properties
}

resource nic 'Microsoft.Network/networkInterfaces@2025-09-01' = {
  name: legacyVm.nic.name
  location: location
  properties: legacyVm.nic.properties
  dependsOn: [ nsg, vnet, pip ]
}

resource vm 'Microsoft.Compute/virtualMachines@2026-04-01' = {
  name: legacyVm.name
  location: location
  properties: legacyVm.vm.properties
  dependsOn: [ nic ]
}

resource shutdown 'Microsoft.DevTestLab/schedules@2018-09-15' = {
  name: legacyVm.shutdownSchedule.name
  location: location
  properties: legacyVm.shutdownSchedule.properties
  dependsOn: [ vm ]
}

resource actionGroup 'microsoft.insights/actionGroups@2024-10-01-preview' = {
  name: legacyVm.actionGroup.name
  location: 'global'
  properties: legacyVm.actionGroup.properties
}

resource alerts 'Microsoft.Insights/metricAlerts@2026-01-01' = [for a in legacyVm.alerts: {
  name: a.name
  location: 'global'
  properties: a.properties
  dependsOn: [ actionGroup, vm ]
}]
