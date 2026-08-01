import apiClient from './client'

export interface GenBoxPushSettings {
  enabled: boolean
  base_url: string
  source_id: string
  has_push_key: boolean
  timeout_secs: number
}

export interface GenBoxPushProbeResult {
  ok: boolean
  contract_version: string
  max_image_bytes: number
}

export interface GenBoxPushImageResult {
  status: 'imported' | 'already-imported' | 'duplicate-local'
  sha256: string
  safe_to_delete_source: boolean
  source_retained: true
}

export type GenBoxPushBatchItemStatus = 'queued' | 'sending' | 'succeeded' | 'already-imported' | 'failed' | 'cancelled'

export interface GenBoxPushBatchItem {
  id: string
  path: string
  status: GenBoxPushBatchItemStatus
  attempts: number
  updated_at: string
  error: string
  receipt_status: 'imported' | 'already-imported' | 'duplicate-local' | ''
  retryable: boolean
  next_retry_at: string
  source_retained: true
}

export interface GenBoxPushBatch {
  id: string
  status: 'queued' | 'sending' | 'succeeded' | 'failed' | 'cancelled'
  created_at: string
  updated_at: string
  total: number
  queued: number
  sending: number
  succeeded: number
  already_imported: number
  retrying: number
  failed: number
  cancelled: number
  items: GenBoxPushBatchItem[]
  source_retained: true
}

export interface GenBoxPushBatchDateRangePreview {
  start_date: string
  end_date: string
  eligible_count: number
  skipped_count: number
  samples: string[]
  paths: string[]
}

export interface GenBoxPushSchedule {
  enabled: boolean
  weekday: number
  time: string
  start_date: string
  end_date: string
  cursor: string
  last_run_at: string
  last_error: string
  queued: number
  succeeded: number
  already_imported: number
  failed: number
  source_retained: true
}

export type GenBoxPushSchedulePayload = Pick<GenBoxPushSchedule, 'enabled' | 'weekday' | 'time' | 'start_date' | 'end_date'>

export type GenBoxPushSettingsPayload = Omit<GenBoxPushSettings, 'has_push_key'> & {
  push_key?: string
  clear_push_key?: boolean
}

export const genboxPushApi = {
  getSettings: () => apiClient.get<never, { settings: GenBoxPushSettings }>('/api/genbox-push/settings'),
  updateSettings: (payload: GenBoxPushSettingsPayload) =>
    apiClient.post<GenBoxPushSettingsPayload, { settings: GenBoxPushSettings }>('/api/genbox-push/settings', payload),
  probe: () => apiClient.post<Record<string, never>, { result: GenBoxPushProbeResult }>('/api/genbox-push/probe', {}),
  pushImage: (path: string) =>
    apiClient.post<{ path: string }, { result: GenBoxPushImageResult }>('/api/genbox-push/images', { path }),
  createBatch: (paths: string[]) =>
    apiClient.post<{ paths: string[] }, { batch: GenBoxPushBatch }>('/api/genbox-push/batches', { paths }),
  previewBatchDateRange: (startDate: string, endDate: string) =>
    apiClient.post<{ start_date: string; end_date: string }, { preview: GenBoxPushBatchDateRangePreview }>(
      '/api/genbox-push/batches/preview-date-range',
      { start_date: startDate, end_date: endDate },
    ),
  getBatch: (batchId: string) =>
    apiClient.get<never, { batch: GenBoxPushBatch }>(`/api/genbox-push/batches/${encodeURIComponent(batchId)}`),
  getLatestRecoverableBatch: () =>
    apiClient.get<never, { batch: GenBoxPushBatch | null }>('/api/genbox-push/batches/latest-recoverable'),
  cancelBatch: (batchId: string) =>
    apiClient.post<Record<string, never>, { batch: GenBoxPushBatch }>(`/api/genbox-push/batches/${encodeURIComponent(batchId)}/cancel`, {}),
  retryFailedBatch: (batchId: string) =>
    apiClient.post<Record<string, never>, { batch: GenBoxPushBatch }>(`/api/genbox-push/batches/${encodeURIComponent(batchId)}/retry-failed`, {}),
  getSchedule: () => apiClient.get<never, { schedule: GenBoxPushSchedule }>('/api/genbox-push/schedule'),
  updateSchedule: (payload: GenBoxPushSchedulePayload) =>
    apiClient.put<GenBoxPushSchedulePayload, { schedule: GenBoxPushSchedule }>('/api/genbox-push/schedule', payload),
  runScheduleNow: () => apiClient.post<Record<string, never>, { schedule: GenBoxPushSchedule }>('/api/genbox-push/schedule/run-now', {}),
}
