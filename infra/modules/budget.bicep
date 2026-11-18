targetScope = 'resourceGroup'

@description('Each: name, amount, timeGrain, startDate, endDate, filter, notifications.')
param budgets array

resource b 'Microsoft.Consumption/budgets@2023-11-01' = [for x in budgets: {
  name: x.name
  properties: {
    category: 'Cost'
    amount: x.amount
    timeGrain: x.timeGrain
    timePeriod: { startDate: x.startDate, endDate: x.endDate }
    filter: x.filter
    notifications: x.notifications
  }
}]
