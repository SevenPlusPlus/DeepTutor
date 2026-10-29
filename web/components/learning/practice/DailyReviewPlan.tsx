'use client'

import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowRight, Clock3, Loader2, Search } from 'lucide-react'
import {
  getPracticeRecommendations,
  type PracticeQuestion,
  type PracticeRecommendations,
  type PracticeRef,
  type PracticeSummary,
} from '@/lib/practice-api'
import { PHOTO_SUBJECTS } from '@/lib/photo-inbox-api'
import { WorkspaceLabel } from '../LibraryWorkspace'
import { LearningStatus, MasteryDetails } from './MasteryDetails'
import { VariantPractice } from './VariantPractice'

export function DailyReviewPlan({
  courseId = '',
  workspaceId,
  summary,
  revision = 0,
  onStart,
  onChanged,
}: {
  courseId?: string
  workspaceId?: string
  summary: PracticeSummary | null
  revision?: number
  onStart: (references: PracticeRef[]) => void
  onChanged?: () => void
}) {
  const { t } = useTranslation()
  const [budget, setBudget] = useState(15)
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState('')
  const [subject, setSubject] = useState('')
  const [appliedSubject, setAppliedSubject] = useState('')
  const [grade, setGrade] = useState('')
  const [appliedGrade, setAppliedGrade] = useState('')
  const [early, setEarly] = useState(false)
  const [plan, setPlan] = useState<PracticeRecommendations | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [details, setDetails] = useState<PracticeQuestion | null>(null)
  const [variant, setVariant] = useState<PracticeRef | null>(null)
  useEffect(() => {
    let active = true
    void Promise.resolve().then(() => {
      if (active) {
        setLoading(true)
        setError('')
      }
    })
    void getPracticeRecommendations(courseId, workspaceId, {
      budget_minutes: budget,
      query,
      subject: appliedSubject,
      grade: appliedGrade,
      include_early: early,
    })
      .then(value => {
        if (active) setPlan(value)
      })
      .catch(err => {
        if (active) {
          setPlan(null)
          setError(err instanceof Error ? err.message : String(err))
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [
    courseId,
    workspaceId,
    budget,
    query,
    appliedSubject,
    appliedGrade,
    early,
    revision,
    refresh,
    summary,
  ])
  const reference = (question: PracticeQuestion): PracticeRef => ({
    id: question.entry.id,
    content_workspace_id:
      question.content_workspace_id ?? (workspaceId === '*' ? undefined : workspaceId),
    content_workspace_name: question.content_workspace_name,
  })
  if (variant)
    return (
      <VariantPractice
        reference={variant}
        onClose={() => setVariant(null)}
        onPractice={value => {
          setVariant(null)
          onStart([value])
        }}
      />
    )
  return (
    <section
      className="overflow-hidden rounded-2xl border border-border bg-card"
      aria-labelledby="practice-today"
    >
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-4 sm:px-6">
        <div>
          <h2 id="practice-today" className="text-base font-semibold">
            {t("Today's review")}
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            {t('Practice the questions that matter today, within your time budget.')}
          </p>
        </div>
        <div
          className="inline-flex gap-1 rounded-lg border border-border p-1"
          aria-label={t('Review time budget')}
        >
          {[10, 15, 20].map(value => (
            <button
              key={value}
              type="button"
              aria-pressed={budget === value}
              onClick={() => setBudget(value)}
              className={`rounded-md px-3 py-1.5 text-xs ${budget === value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted'}`}
            >
              {t('{{count}} min', { count: value })}
            </button>
          ))}
        </div>
      </header>
      <div className="grid grid-cols-2 divide-x divide-border border-b border-border bg-muted/20 sm:grid-cols-4">
        {(
          [
            ['Still learning', summary?.open],
            ['Awaiting verification', summary?.verify],
            ['Verified mastery', summary?.closed],
            ['Paused', summary?.paused],
          ] as const
        ).map(([label, count]) => (
          <div key={label} className="px-4 py-4">
            <p className="text-2xl font-medium tabular-nums">{count ?? '—'}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t(label)}</p>
          </div>
        ))}
      </div>
      <div className="p-5 sm:p-6">
        <form
          className="mb-4 flex flex-wrap gap-2"
          onSubmit={event => {
            event.preventDefault()
            setQuery(search.trim())
            setAppliedSubject(subject)
            setAppliedGrade(grade.trim())
          }}
        >
          <label className="flex min-w-40 flex-1 items-center gap-2 rounded-lg border border-border bg-background px-3">
            <Search size={15} className="text-muted-foreground" />
            <input
              value={search}
              onChange={event => setSearch(event.target.value)}
              aria-label={t('Search mistake topics or tags')}
              placeholder={t('Topic, question or mistake tag')}
              className="h-10 w-full min-w-0 bg-transparent text-xs outline-none"
            />
          </label>
          <select
            value={subject}
            onChange={event => setSubject(event.target.value)}
            aria-label={t('Subject filter')}
            className="h-10 w-36 rounded-lg border border-border bg-background px-3 text-xs"
          >
            <option value="">{t('All subjects')}</option>
            {Object.entries(PHOTO_SUBJECTS)
              .filter(([id]) => id !== 'unclassified')
              .map(([id, label]) => (
                <option key={id} value={id}>
                  {t(label)}
                </option>
              ))}
          </select>
          <input
            value={grade}
            onChange={event => setGrade(event.target.value)}
            aria-label={t('Grade filter')}
            placeholder={t('Grade (optional)')}
            maxLength={100}
            className="h-10 w-36 rounded-lg border border-border bg-background px-3 text-xs"
          />
          <button
            type="submit"
            className="rounded-lg border border-border px-3 text-xs hover:bg-muted"
          >
            {t('Find questions')}
          </button>
        </form>
        <label className="mb-5 flex items-center gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={early}
            onChange={event => setEarly(event.target.checked)}
            className="accent-primary"
          />
          {t('Include not-yet-due questions for focused practice')}
        </label>
        {error && (
          <p
            role="alert"
            className="mb-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive"
          >
            {error}
            <button
              type="button"
              onClick={() => setRefresh(value => value + 1)}
              className="ml-3 underline"
            >
              {t('Retry')}
            </button>
          </p>
        )}
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Clock3 size={14} />
            {loading
              ? t('Loading…')
              : t('{{count}} questions · about {{minutes}} min', {
                  count: plan?.items.length ?? 0,
                  minutes: plan?.estimated_minutes ?? 0,
                })}
          </p>
          <button
            type="button"
            disabled={loading || !plan?.items.length}
            onClick={() => onStart((plan?.items ?? []).map(reference))}
            className="inline-flex items-center gap-2 rounded-xl bg-primary px-4 py-2.5 text-sm text-primary-foreground disabled:opacity-40"
          >
            {loading ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />}
            {t("Start today's review")}
          </button>
        </div>
        {!loading && !error && !plan?.items.length && (
          <p className="rounded-xl border border-dashed border-border px-4 py-8 text-center text-sm text-muted-foreground">
            {t(
              'No matching questions are due. Adjust the filters or choose focused early practice.'
            )}
          </p>
        )}
        <div className={loading ? 'pointer-events-none opacity-50' : ''}>
          {plan?.items.map((question, index) => (
            <article
              key={JSON.stringify([question.content_workspace_id, question.entry.id])}
              className="border-b border-border py-4 last:border-b-0"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex min-w-0 gap-3">
                  <span className="pt-0.5 font-serif text-lg text-muted-foreground">
                    {String(index + 1).padStart(2, '0')}
                  </span>
                  <div className="min-w-0">
                    <button
                      type="button"
                      onClick={() => setDetails(question)}
                      className="line-clamp-2 text-left text-sm font-medium leading-6 [overflow-wrap:anywhere] hover:text-primary"
                    >
                      {question.entry.question}
                    </button>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <LearningStatus state={question.state} />
                      <WorkspaceLabel row={question} />
                      {question.entry.categories?.map(tag => (
                        <span
                          key={tag.id}
                          className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground"
                        >
                          {tag.name}
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => onStart([reference(question)])}
                  aria-label={t('Practice question {{number}}', { number: index + 1 })}
                  className="mt-1 rounded-full border border-border p-2 hover:bg-muted"
                >
                  <ArrowRight size={14} />
                </button>
              </div>
              {question.recommendation?.reasons?.length ? (
                <ul className="ml-9 mt-2 space-y-1">
                  {question.recommendation.reasons.map(reason => (
                    <li key={reason} className="text-xs leading-5 text-muted-foreground">
                      {t(reason)}
                    </li>
                  ))}
                </ul>
              ) : null}
            </article>
          ))}
        </div>
        <p className="mt-4 border-t border-border pt-3 text-xs leading-6 text-muted-foreground">
          {t(
            'Verified mastery and paused questions stay out of daily recommendations. Intervals use your review history, not a fixed forgetting calendar.'
          )}
        </p>
      </div>
      {details && (
        <MasteryDetails
          question={details}
          reference={reference(details)}
          onClose={() => setDetails(null)}
          onChanged={() => {
            setRefresh(value => value + 1)
            onChanged?.()
          }}
          onPractice={value => {
            setDetails(null)
            onStart([value])
          }}
          onVariant={value => {
            setDetails(null)
            setVariant(value)
          }}
        />
      )}
    </section>
  )
}
