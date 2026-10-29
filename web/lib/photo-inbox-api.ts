import { apiUrl, requestJson } from '@/shared/api/client'
import type { RecognitionDraft } from '@/lib/practice-api'
import { scopedUrl } from '@/lib/workspace-scope'

const ROOT = '/api/question-notebook/practice/photo-inbox'

export const PHOTO_SUBJECTS = {
  chinese: 'photoSubject.chinese', math: 'photoSubject.math', english: 'photoSubject.english', physics: 'photoSubject.physics',
  chemistry: 'photoSubject.chemistry', biology: 'photoSubject.biology', history: 'photoSubject.history', geography: 'photoSubject.geography',
  politics: 'photoSubject.politics', other: 'photoSubject.other', unclassified: 'photoSubject.unclassified',
} as const
export type PhotoSubject = keyof typeof PHOTO_SUBJECTS
export type PhotoStatus = 'queued' | 'recognizing' | 'ready' | 'needs_subject' | 'needs_retake' | 'non_question' | 'failed' | 'confirmed'
export const PHOTO_STATUS_LABELS: Record<PhotoStatus, string> = {
  queued: 'Waiting for recognition', recognizing: 'Recognizing photo', ready: 'Archived',
  needs_subject: 'Confirm subject', needs_retake: 'Retake needed', non_question: 'Not a question',
  failed: 'Recognition failed', confirmed: 'In review',
}
export const PHOTO_IN_FLIGHT = new Set<PhotoStatus>(['queued', 'recognizing'])
export interface PhotoAsset { id: string; url: string; filename: string; mime_type: string }
export interface PhotoDraft extends RecognitionDraft { subject: PhotoSubject; grade?: string; knowledge_point?: string }
export interface PhotoInboxItem {
  id: string
  status: PhotoStatus
  subject: PhotoSubject
  received_at: number
  source_sent_at: number | null
  last_received_at: number
  upload_count: number
  asset: PhotoAsset
  assets: PhotoAsset[]
  drafts: PhotoDraft[]
  version: number
  error: string
  entry_ids: number[]
  note: string
  source?: { channel?: string; partner_id?: string; partner_name?: string }
  content_workspace_id?: string
  content_workspace_name?: string
  archived_at?: number | null
  uploads?: { received_at: number; source_sent_at: number | null }[]
  result?: { created: number; duplicates: number; total: number }
}
export interface PhotoInboxList {
  items: PhotoInboxItem[]
  total: number
  subjects: { id: PhotoSubject; label: string }[]
  counts: { photos: number; pending: number; confirmed: number }
  unavailable_workspaces?: string[]
}
export function photoSubjectLabel(item: Pick<PhotoInboxItem, 'subject' | 'drafts'>): string {
  const subjects = new Set(item.drafts.map(draft => draft.subject))
  return subjects.size > 1 && !subjects.has('unclassified')
    ? 'photoSubject.mixed'
    : PHOTO_SUBJECTS[item.subject] || PHOTO_SUBJECTS.unclassified
}
export interface PhotoInboxFilters {
  subject?: string; status?: string; q?: string; since?: number; until?: number
  limit?: number; offset?: number
  all_workspaces?: boolean
}
const json = (body: unknown, method = 'POST'): RequestInit => ({
  method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})
export function listPhotoInbox(filters: PhotoInboxFilters = {}) {
  const params = new URLSearchParams()
  Object.entries(filters).forEach(([key, value]) => {
    if (value !== undefined && value !== '') params.set(key, String(value))
  })
  return requestJson<PhotoInboxList>(apiUrl(`${ROOT}?${params}`), { cache: 'no-store' })
}
const itemUrl = (id: string, workspaceId?: string, action = '') =>
  scopedUrl(`${ROOT}/${encodeURIComponent(id)}${action}`, workspaceId)
export const getPhotoInboxItem = (id: string, workspaceId?: string) =>
  requestJson<PhotoInboxItem>(itemUrl(id, workspaceId), { cache: 'no-store' })
export const updatePhotoInboxItem = (id: string, payload: {
  expected_version: number; subject?: PhotoSubject; drafts?: PhotoDraft[]; note?: string
}, workspaceId?: string) => requestJson<PhotoInboxItem>(itemUrl(id, workspaceId), json(payload, 'PATCH'))
export const retryPhotoInboxItem = (id: string, expectedVersion: number, workspaceId?: string) =>
  requestJson<PhotoInboxItem>(itemUrl(id, workspaceId, '/retry'), json({ expected_version: expectedVersion }))
export const confirmPhotoInboxItem = (id: string, expectedVersion: number, drafts: PhotoDraft[], workspaceId?: string) =>
  requestJson<PhotoInboxItem>(itemUrl(id, workspaceId, '/confirm'), json({ expected_version: expectedVersion, drafts, confirmed: true }))
export const removePhotoInboxItem = (id: string, expectedVersion: number, workspaceId?: string) =>
  requestJson<PhotoInboxItem>(itemUrl(id, workspaceId, '/remove'), json({ expected_version: expectedVersion }))
export const restorePhotoInboxItem = (id: string, expectedVersion: number, workspaceId?: string) =>
  requestJson<PhotoInboxItem>(itemUrl(id, workspaceId, '/restore'), json({ expected_version: expectedVersion }))
export function supplementPhotoInboxItem(id: string, expectedVersion: number, file: File, workspaceId?: string) {
  const body = new FormData()
  body.append('file', file)
  body.append('expected_version', String(expectedVersion))
  return requestJson<PhotoInboxItem>(itemUrl(id, workspaceId, '/supplement'), { method: 'POST', body })
}
