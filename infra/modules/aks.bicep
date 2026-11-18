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
param maintenanceStartDate string = '2026-11-11'
param aksExtraProperties object = {}

var poolProfiles = [for p in agentPools: union({
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
}, p.?extra ?? {})]

resource aks 'Microsoft.ContainerService/managedClusters@2026-05-02-preview' = {
  name: aksClusterName
  location: location
  kind: 'Base'
  sku: { name: 'Base', tier: 'Free' }
  identity: { type: 'SystemAssigned' }
  properties: union({
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
      podCidrs: ['10.244.0.0/16']
      serviceCidr: '10.0.0.0/16'
      serviceCidrs: ['10.0.0.0/16']
      dnsServiceIP: '10.0.0.10'
      ipFamilies: ['IPv4']
      podLinkLocalAccess: 'IMDS'
    }
    autoScalerProfile: {
      'balance-similar-node-groups': 'false'
      'daemonset-eviction-for-empty-nodes': false
      'daemonset-eviction-for-occupied-nodes': true
      expander: 'random'
      'ignore-daemonsets-utilization': false
      'max-empty-bulk-delete': '10'
      'max-graceful-termination-sec': '600'
      'max-node-provision-time': '15m'
      'max-total-unready-percentage': '45'
      'new-pod-scale-up-delay': '0s'
      'ok-total-unready-count': '3'
      'scale-down-delay-after-add': '5m'
      'scale-down-delay-after-delete': '10s'
      'scale-down-delay-after-failure': '3m'
      'scale-down-unneeded-time': '10m'
      'scale-down-unready-time': '20m'
      'scale-down-utilization-threshold': '0.5'
      'scan-interval': '30s'
      'skip-nodes-with-local-storage': 'false'
      'skip-nodes-with-system-pods': 'true'
    }
    agentPoolProfiles: poolProfiles
  }, aksExtraProperties)
}

resource maintenance 'Microsoft.ContainerService/managedClusters/maintenanceConfigurations@2026-05-02-preview' = {
  parent: aks
  name: 'aksManagedNodeOSUpgradeSchedule'
  properties: {
    maintenanceWindow: {
      schedule: { weekly: { intervalWeeks: 1, dayOfWeek: 'Saturday' } }
      durationHours: 4
      utcOffset: maintenanceUtcOffset
      startDate: maintenanceStartDate
      startTime: maintenanceStartTime
    }
  }
}
