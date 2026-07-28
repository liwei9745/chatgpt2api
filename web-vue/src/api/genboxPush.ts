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
}
