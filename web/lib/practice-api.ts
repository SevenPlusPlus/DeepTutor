import { scopedUrl } from '@/lib/workspace-scope'
import { apiFetch, apiUrl } from '@/lib/api'
import type { NotebookEntry } from '@/lib/notebook-api'

const ROOT = '/api/question-notebook/practice'
export type PracticeRating = 'again' | 'hard' | 'good' | 'easy'
export interface PracticeSummary {
  total: number
  mistakes: number
  due: number
  overdue: number
  reviewed_today: number
  next_due_at: number | null
  day_end: number
  timezone: string
  unavailable_workspaces?: string[]
  open?: number
  verify?: number
  closed?: number
  paused?: number
}
export interface PracticeRef {
  id: number
  content_workspace_id?: string
  content_workspace_name?: string
  attempt_kind?: 'original' | 'spotcheck'
}
export interface MasteryEvidence {
  spaced: boolean
  transfer: boolean
  delayed: boolean
  independent_successes?: number
  policy_version?: string
  attempt_ids?: string[]
  missing?: string[]
}
export interface PracticeState {
  version: number
  review_count: number
  is_mistake: boolean
  due_at: number | null
  learning_status?: 'open' | 'verify' | 'closed'
  paused?: boolean
  closed_at?: number | null
  evidence?: MasteryEvidence
}
export interface PracticeQuestion {
  content_workspace_id?: string
  content_workspace_name?: string
  entry: NotebookEntry
  state: PracticeState
  recommendation?: { reasons: string[]; score: number; estimated_minutes: number }
}
export interface PracticeRecommendations {
  items: PracticeQuestion[]
  estimated_minutes: number
  budget_minutes: number
  unavailable_workspaces?: string[]
}
export interface PracticeMastery {
  state: PracticeState
  evidence: MasteryEvidence
  history: {
    entry_id: number
    target_entry_id?: number
    kind: string
    correct: boolean | null
    independent: boolean
    reviewed_at: number
    metadata?: Record<string, unknown>
  }[]
  decisions: { action: string; decided_at: number; reason: string; policy_version?: string }[]
}
export interface PracticeReviewResult {
  due_at: number
  correct: boolean | null
  rating: PracticeRating
  is_mistake: boolean
  learning_status?: 'open' | 'verify' | 'closed'
  evidence?: MasteryEvidence
  closed?: boolean
  reopened?: boolean
  independent?: boolean
  parent_entry_id?: number
  parent_state?: PracticeState
}
export interface PracticeReviewRequest {
  request_id: string
  version: number
  rating: PracticeRating
  answer: string
  self_report?: boolean
  hint_used?: boolean
  answer_revealed?: boolean
  attempt_kind?: 'original' | 'spotcheck'
  timezone?: string
  auto_grade?: boolean
}
export type PracticeMetric = 'questions' | 'mistakes' | 'reviews'
export type PracticeCounts = Record<PracticeMetric, number>
export interface PracticeAnalytics {
  timezone: string
  days: number
  start_date: string
  end_date: string
  updated_at: number
  daily: (PracticeCounts & { date: string })[]
  sources: (PracticeCounts & { source: string })[]
  totals: PracticeCounts
}
export const getPracticeAnalytics = (courseId = '', days = 30, workspaceId?: string) =>
  request<PracticeAnalytics>(
    `/analytics?${params(courseId)}&days=${days}&all_workspaces=${workspaceId === '*'}`,
    undefined,
    workspaceId === '*' ? '' : workspaceId
  )
export interface ImportPreview {
  token: string | null
  total: number
  valid: number
  errors: { row: number; message: string }[]
  samples: { question: string; question_type: string; correct_answer: string; tags: string[] }[]
}
export type RecognitionStatus =
  | 'queued'
  | 'validating'
  | 'parsing'
  | 'recognizing'
  | 'ready'
  | 'staged'
  | 'committed'
  | 'failed'
  | 'expired'
export interface RecognitionDraft {
  draft_id: string
  ordinal: number
  question: string
  question_type: 'single_choice' | 'multi_choice' | 'true_false' | 'fill_blank' | 'short_answer'
  options: Record<string, string>
  correct_answer: string
  explanation: string
  difficulty: string
  tags: string[]
  question_images: { id?: string; url?: string; filename?: string; mime_type?: string }[]
  source_locator: { page_numbers?: number[]; bounding_boxes?: number[][] }
  answer_origin: 'document' | 'user' | 'model_suggested' | 'missing'
  review_level: 'normal' | 'review' | 'required'
  warnings: string[]
  selected?: boolean
}
export interface RecognitionSnapshot {
  job_id: string
  filename?: string
  target?: 'bank' | 'mistakes'
  status: RecognitionStatus
  stage: string
  progress_message: string
  completed_units: number
  total_units: number
  version: number
  drafts: RecognitionDraft[]
  summary: { total: number; normal: number; review: number; required: number }
  error_code: string
  error_message: string
  expires_at: number
}
export class PracticeRequestError extends Error {
  constructor(
    message: string,
    public status: number
  ) {
    super(message)
  }
}
async function request<T>(path: string, init?: RequestInit, workspaceId?: string): Promise<T> {
  const response = await apiFetch(
    apiUrl(workspaceId === undefined ? `${ROOT}${path}` : scopedUrl(`${ROOT}${path}`, workspaceId)),
    { cache: 'no-store', ...init }
  )
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new PracticeRequestError(
      typeof body.detail === 'string' ? body.detail : `Request failed: ${response.status}`,
      response.status
    )
  }
  return response.json() as Promise<T>
}
const json = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})
function params(courseId = ''): string {
  return new URLSearchParams({
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    course_id: courseId,
  }).toString()
}
export const getPracticeSummary = (courseId = '', workspaceId?: string) =>
  request<PracticeSummary>(
    `/summary?${params(courseId)}&all_workspaces=${workspaceId === '*'}`,
    undefined,
    workspaceId === '*' ? '' : workspaceId
  )
export const getPracticeQueue = (courseId = '', workspaceId?: string) =>
  request<PracticeQuestion[]>(
    `/queue?${params(courseId)}&all_workspaces=${workspaceId === '*'}`,
    undefined,
    workspaceId === '*' ? '' : workspaceId
  )
export const getPracticeQuestion = (id: number, workspaceId?: string) =>
  request<PracticeQuestion>(`/questions/${id}`, undefined, workspaceId)
export function getPracticeRecommendations(
  courseId = '',
  workspaceId?: string,
  filters: {
    budget_minutes?: number
    category_id?: number
    query?: string
    subject?: string
    grade?: string
    include_early?: boolean
  } = {}
) {
  const search = new URLSearchParams(params(courseId))
  search.set('all_workspaces', String(workspaceId === '*'))
  search.set('budget_minutes', String(filters.budget_minutes ?? 15))
  if (filters.category_id !== undefined) search.set('category_id', String(filters.category_id))
  if (filters.query?.trim()) search.set('query', filters.query.trim())
  if (filters.subject?.trim()) search.set('subject', filters.subject.trim())
  if (filters.grade?.trim()) search.set('grade', filters.grade.trim())
  search.set('include_early', String(filters.include_early ?? false))
  return request<PracticeRecommendations>(
    `/recommendations?${search}`,
    undefined,
    workspaceId === '*' ? '' : workspaceId
  )
}
export const getPracticeMastery = (id: number, workspaceId?: string) =>
  request<PracticeMastery>(`/questions/${id}/mastery`, undefined, workspaceId)
export const updatePracticeLearning = (
  id: number,
  version: number,
  paused: boolean,
  workspaceId?: string
) => request<PracticeState>(`/questions/${id}/learning`, json({ version, paused }), workspaceId)
export const recordPracticeExposure = (
  id: number,
  requestId: string,
  kind: 'hint' | 'answer',
  workspaceId?: string
) =>
  request<{ recorded: boolean }>(
    `/questions/${id}/exposure`,
    json({ request_id: requestId, kind }),
    workspaceId
  )
export const checkPracticeAnswer = (id: number, answer: string, workspaceId?: string) =>
  request<{ correct: boolean | null }>(`/questions/${id}/check`, json({ answer }), workspaceId)
export const savePracticeReview = (id: number, body: PracticeReviewRequest, workspaceId?: string) =>
  request<PracticeReviewResult>(`/questions/${id}/review`, json(body), workspaceId)
export interface PracticeVariantQuestion {
  question: string
  question_type: 'single_choice' | 'multi_choice' | 'true_false' | 'fill_blank' | 'short_answer'
  options: Record<string, string>
  correct_answer: string
  explanation: string
  difficulty: 'easy' | 'medium' | 'hard'
  knowledge_point: string
  target_skill: string
  introduces_new_skill: boolean
}
export interface PracticeVariantDraft extends PracticeVariantQuestion {
  draft_id: string
  version: number
  parent_entry_id: number
  kind: 'similar' | 'advanced'
  status: 'ready' | 'confirmed'
  entry_id?: number
}
export const generatePracticeVariant = (
  id: number,
  requestId: string,
  kind: 'similar' | 'advanced',
  workspaceId?: string
) =>
  request<PracticeVariantDraft>(
    `/questions/${id}/variants/generate`,
    json({ request_id: requestId, kind }),
    workspaceId
  )
export const confirmPracticeVariant = (
  id: number,
  draft: PracticeVariantDraft,
  workspaceId?: string
) =>
  request<PracticeQuestion>(
    `/questions/${id}/variants/confirm`,
    json({
      draft_id: draft.draft_id,
      expected_version: draft.version,
      confirmed: true,
      question: {
        question: draft.question,
        question_type: draft.question_type,
        options: draft.options,
        correct_answer: draft.correct_answer,
        explanation: draft.explanation,
        difficulty: draft.difficulty,
        knowledge_point: draft.knowledge_point,
        target_skill: draft.target_skill,
        introduces_new_skill: draft.introduces_new_skill,
      },
    }),
    workspaceId
  )
export function previewPracticeImport(file: File, target: 'bank' | 'mistakes', courseId = '') {
  const data = new FormData()
  data.append('file', file)
  data.append('target', target)
  data.append('course_id', courseId)
  return request<ImportPreview>('/import/preview', { method: 'POST', body: data })
}
export function startPracticeRecognition(file: File, target: 'bank' | 'mistakes', courseId = '') {
  const data = new FormData()
  data.append('file', file)
  data.append('target', target)
  data.append('course_id', courseId)
  return request<{ job_id: string; status: RecognitionStatus; created_at: number }>(
    '/import/recognize',
    { method: 'POST', body: data }
  )
}
export const getPracticeRecognition = (jobId: string) =>
  request<RecognitionSnapshot>(`/import/recognize/${encodeURIComponent(jobId)}`)
export const getCurrentPracticeRecognition = () =>
  request<RecognitionSnapshot | null>('/import/recognize')
export const stagePracticeRecognition = (
  jobId: string,
  expectedVersion: number,
  drafts: RecognitionDraft[]
) =>
  request<ImportPreview>(
    `/import/recognize/${encodeURIComponent(jobId)}/stage`,
    json({ expected_version: expectedVersion, drafts })
  )
export const commitPracticeImport = (token: string) =>
  request<{ created: number; duplicates: number }>('/import/commit', json({ token }))
export async function downloadPracticeTemplate(format: 'csv' | 'xlsx') {
  const response = await apiFetch(apiUrl(`${ROOT}/import/template?format=${format}`))
  if (!response.ok) throw new Error(`Request failed: ${response.status}`)
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `practice-template.${format}`
  anchor.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
