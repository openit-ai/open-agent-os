export interface SalesKpi {
  today: number
  week: number
  month: number
}

export interface SalesStub {
  kpi: SalesKpi
  sparkline: number[]
}

export const SALES_STUB: SalesStub = {
  kpi: { today: 2_380_000, week: 18_450_000, month: 67_900_000 },
  sparkline: [42, 58, 45, 72, 68, 85, 62, 90, 78, 88, 95, 110, 102, 118, 105, 130],
}

// thresholds for alert badges (Financial Dashboard colors)
export const SALES_THRESHOLDS = {
  warning: 50,
  critical: 30,
}

export const HISTORY_KEY = 'openit-sales-history'
export const MAX_HISTORY = 60
