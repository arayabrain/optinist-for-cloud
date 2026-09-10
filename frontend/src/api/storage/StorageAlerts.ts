import { LimitAlertType } from "const/Subscription"
import axios from "utils/axios"

export interface StorageAlert {
  user_name: string
  user_email: string
  alert_level: "critical" | "danger"
  storage_usage_bytes: number
  storage_quota_bytes: number
  storage_usage_percent: number
  timestamp: string
  message: string
  subscription_plan: string
}

export interface StorageUsage {
  storage_usage_bytes: number
  storage_usage_formatted: string
  storage_quota_bytes: number | null
  storage_quota_formatted: string | null
  storage_usage_percent: number | null
  alert_level: "critical" | "danger" | null
  thresholds: {
    critical: number
    danger: number
  }
}

export interface StorageAlertResponse {
  has_alert: boolean
  storage_usage_bytes?: number
  storage_usage_formatted?: string
  alert: StorageAlert | null
}

export interface RefreshStorageResponse {
  success: boolean
  updated_usage_bytes: number
  updated_usage_formatted: string
  database_updated: boolean
}

export const getMyStorageAlertApi = async (): Promise<StorageAlertResponse> => {
  const response = await axios.get("/storage-limit-alerts/me")
  return response.data
}

export const getMyStorageUsageApi = async (): Promise<StorageUsage> => {
  const response = await axios.get("/storage-limit-alerts/usage")
  return response.data
}

export const getAllStorageAlertsApi = async (): Promise<StorageAlert[]> => {
  const response = await axios.get("/storage-limit-alerts/all")
  return response.data
}

export const refreshStorageUsageApi =
  async (): Promise<RefreshStorageResponse> => {
    const response = await axios.post("/storage-limit-alerts/refresh")
    return response.data
  }

// Limit Alert Types
export interface LimitAlert {
  has_alert: boolean
  alert_type: LimitAlertType
  days_remaining: number
  excess_data_bytes: number
  excess_data_gb: number
  storage_usage_bytes: number
  storage_usage_gb: number
  storage_quota_bytes: number
  storage_quota_gb: number
  subscription_end_date?: string
  grace_end_date?: string
  deletion_date?: string // Optional: only present for free users and expired subscriptions
  message: string
}

export interface LimitAlertStatus {
  has_alert: boolean
  alert_type: string | null
  days_remaining: number | null
}

// Limit Alert API Functions
export const getMyLimitAlertApi = async (): Promise<LimitAlert | null> => {
  const response = await axios.get("/storage-limit-alerts/limit-warning")
  return response.data
}

export const checkLimitAlertStatusApi = async (): Promise<LimitAlertStatus> => {
  const response = await axios.get("/storage-limit-alerts/limit-warning/check")
  return response.data
}
