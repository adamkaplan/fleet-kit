param location string
param tenantId string
param aksClusterName string
param aksDnsPrefix string
param aksKubernetesVersion string
param aksNodeResourceGroup string
@description('Each pool: name, mode, vmSize, osDiskSizeGB, osDiskType, count, minCount, maxCount, enableAutoScaling, nodeLabels, nodeTaints, tags, orchestratorVersion.')
param agentPools array
param maintenanceStartTime string
param maintenanceUtcOffset string
param autoScalerProfile object = {
  expander: 'random'
  'scan-interval': '30s'
  'scale-down-unneeded-time': '10m'
}

resource aks 'Microsoft.ContainerService/managedClusters@2026-05-02-preview' = {
  name: aksClusterName
  location: location
  kind: 'Base'
  sku: { name: 'Base', tier: 'Free' }
  identity: { type: 'SystemAssigned' }
  properties: {
    kubernetesVersion: aksKubernetesVersion
    dnsPrefix: aksDnsPrefix
    nodeResourceGroup: aksNodeResourceGroup
    enableRBAC: true
    disableLocalAccounts: false
    supportPlan: 'KubernetesOfficial'
    aadProfile: { managed: true, enableAzureRBAC: true, tenantID: tenantId }
    oidcIssuerProfile: { enabled: true }
    servicePrincipalProfile: { clientId: 'msi' }
    autoUpgradeProfile: { nodeOSUpgradeChannel: 'NodeImage' }
    bootstrapProfile: { artifactSource: 'Direct' }
    storageProfile: {
      diskCSIDriver: { enabled: true }
      fileCSIDriver: { enabled: true }
      snapshotController: { enabled: true }
    }
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkPolicy: 'cilium'
      networkDataplane: 'cilium'
      loadBalancerSku: 'standard'
      outboundType: 'loadBalancer'
      podCidr: '10.244.0.0/16'
      serviceCidr: '10.0.0.0/16'
      dnsServiceIP: '10.0.0.10'
    }
    autoScalerProfile: autoScalerProfile
    agentPoolProfiles: [for p in agentPools: {
      name: p.name
      mode: p.mode
      vmSize: p.vmSize
      osDiskSizeGB: p.osDiskSizeGB
      osDiskType: p.osDiskType
      count: p.count
      minCount: p.?minCount
      maxCount: p.?maxCount
      enableAutoScaling: p.enableAutoScaling
      nodeLabels: p.?nodeLabels
      nodeTaints: p.?nodeTaints
      tags: p.?tags
      orchestratorVersion: p.?orchestratorVersion
      type: 'VirtualMachineScaleSets'
    }]
  }
}

resource maintenance 'Microsoft.ContainerService/managedClusters/maintenanceConfigurations@2026-05-02-preview' = {
  parent: aks
  name: 'aksManagedNodeOSUpgradeSchedule'
  properties: {
    maintenanceWindow: {
      schedule: { weekly: { intervalWeeks: 1, dayOfWeek: 'Saturday' } }
      durationHours: 4
      utcOffset: maintenanceUtcOffset
      startTime: maintenanceStartTime
    }
  }
}
