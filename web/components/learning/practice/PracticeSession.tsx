'use client'

import Markdown from '@/components/common/MarkdownRenderer'
import { practiceMarkdown } from '@/lib/practice-content'
import { randomUuid } from '@/lib/random-uuid'
import { apiUrl } from '@/shared/api/client'
import { scopedUrl } from '@/lib/workspace-scope'
import { WorkspaceLabel } from '../LibraryWorkspace'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowLeft, ArrowRight, CheckCircle2, Loader2, ShieldCheck } from 'lucide-react'
import {
  getPracticeQuestion,
  PracticeRequestError,
  recordPracticeExposure,
  savePracticeReview,
  type PracticeQuestion,
  type PracticeRef,
  type PracticeRating,
  type PracticeReviewRequest,
  type PracticeReviewResult,
} from '@/lib/practice-api'
import { LearningStatus, MasteryDetails, MasteryEvidenceList } from './MasteryDetails'
import { VariantPractice } from './VariantPractice'

export function PracticeSession({
  ids,
  questions,
  onClose,
}: {
  ids?: number[]
  questions?: PracticeRef[]
  onClose: () => void
}) {
  const [queue] = useState<PracticeRef[]>(() => questions ?? (ids ?? []).map(id => ({ id })))
  const [index, setIndex] = useState(0)
  const [saved, setSaved] = useState(0)
  const [skipped, setSkipped] = useState(0)
  const [closed, setClosed] = useState(0)
  const [saving, setSaving] = useState(false)
  const [nextDue, setNextDue] = useState<number | null>(null)
  const dueDates = useRef(new Map<string, number>())
  const { t } = useTranslation()
  const complete = index >= queue.length
  return (
    <section className="mx-auto max-w-3xl">
      <div className="mb-5 flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={onClose}
          disabled={saving}
          className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft size={15} />
          {t('Back to practice')}
        </button>
        <p className="text-xs text-muted-foreground" aria-live="polite">
          {t('{{count}} remaining', { count: queue.length - index })}
        </p>
      </div>
      <progress
        max={queue.length || 1}
        value={index}
        aria-label={t('Practice progress')}
        className="mb-6 h-1.5 w-full accent-primary"
      />
      {complete ? (
        <div className="rounded-2xl border border-border bg-card px-6 py-12 text-center">
          <CheckCircle2 className="mx-auto mb-4 text-emerald-600" size={32} />
          <h2 className="font-serif text-2xl font-semibold">{t('Practice complete')}</h2>
          <p className="mt-3 text-sm text-muted-foreground">
            {t('{{count}} reviews saved. Your next review dates have been updated.', {
              count: saved,
            })}
          </p>
          <p className="mt-2 text-sm text-emerald-700 dark:text-emerald-400">
            {t('{{count}} questions reached verified mastery.', { count: closed })}
          </p>
          {skipped > 0 && (
            <p className="mt-2 text-sm text-muted-foreground">
              {t('{{count}} skipped questions remain due.', { count: skipped })}
            </p>
          )}
          {nextDue && (
            <p className="mt-2 text-xs text-muted-foreground">
              {t('Next review: {{date}}', { date: new Date(nextDue * 1000).toLocaleDateString() })}
            </p>
          )}
          <p className="mx-auto mt-4 max-w-md text-xs leading-6 text-muted-foreground">
            {t(
              'A correct attempt is progress. Verified mastery also needs spaced practice, a different question and delayed verification.'
            )}
          </p>
          <button
            type="button"
            onClick={onClose}
            className="mt-6 rounded-xl bg-primary px-5 py-2.5 text-sm text-primary-foreground"
          >
            {t('Back to practice')}
          </button>
        </div>
      ) : (
        <PracticeTurn
          key={`${index}:${queue[index].content_workspace_id}:${queue[index].id}`}
          reference={queue[index]}
          onBusy={setSaving}
          onSaved={result => {
            setSaved(value => value + 1)
            if (result.closed) setClosed(value => value + 1)
            if (result.due_at && result.learning_status !== 'closed') {
              dueDates.current.set(
                JSON.stringify([queue[index].content_workspace_id, queue[index].id]),
                result.due_at
              )
              setNextDue(Math.min(...dueDates.current.values()))
            }
            setIndex(value => value + 1)
          }}
          onSkip={() => {
            setSkipped(value => value + 1)
            setIndex(value => value + 1)
          }}
        />
      )}
    </section>
  )
}

function PracticeTurn({
  reference,
  onSaved,
  onSkip,
  onBusy,
}: {
  reference: PracticeRef
  onSaved: (result: PracticeReviewResult) => void
  onSkip: () => void
  onBusy: (busy: boolean) => void
}) {
  const { id, content_workspace_id: workspaceId, content_workspace_name: workspaceName } = reference
  const { t } = useTranslation()
  const [question, setQuestion] = useState<PracticeQuestion | null>(null)
  const [answer, setAnswer] = useState('')
  const [exposed, setExposed] = useState(false)
  const [rating, setRating] = useState<PracticeRating>('good')
  const [result, setResult] = useState<PracticeReviewResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const [reload, setReload] = useState(0)
  const [details, setDetails] = useState(false)
  const [variant, setVariant] = useState<PracticeRef | null>(null)
  const [variantSession, setVariantSession] = useState<PracticeRef | null>(null)
  const pending = useRef(false)
  const exposureId = useRef<string | null>(null)
  const submission = useRef<PracticeReviewRequest | null>(null)
  useEffect(() => {
    let active = true
    void getPracticeQuestion(id, workspaceId)
      .then(value => {
        if (active) setQuestion(value)
      })
      .catch(err => {
        if (active) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      active = false
    }
  }, [id, reload, workspaceId])
  async function showReference() {
    if (pending.current || exposed || result || !question) return
    pending.current = true
    setBusy(true)
    onBusy(true)
    setError('')
    exposureId.current ??= randomUuid()
    try {
      await recordPracticeExposure(id, exposureId.current, 'answer', workspaceId)
      setExposed(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      pending.current = false
      setBusy(false)
      onBusy(false)
    }
  }
  async function submit() {
    if (pending.current || !question || conflict || result || !answer.trim()) return
    pending.current = true
    setBusy(true)
    onBusy(true)
    setError('')
    const selfAssessed = ['short_answer', 'essay', 'free_response', 'qualitative'].includes(
      question.entry.question_type
    )
    submission.current ??= {
      request_id: randomUuid(),
      version: question.state.version,
      rating,
      answer,
      self_report: selfAssessed,
      auto_grade: true,
      hint_used: false,
      answer_revealed: exposed,
      attempt_kind:
        reference.attempt_kind ??
        (question.state.learning_status === 'closed' ? 'spotcheck' : 'original'),
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
    }
    try {
      // Persist and grade before revealing feedback, so the current answer is
      // evidence while a later same-day retry cannot erase answer exposure.
      const saved = await savePracticeReview(id, submission.current, workspaceId)
      setResult(saved)
      setQuestion(current =>
        current
          ? {
              ...current,
              state: {
                ...current.state,
                learning_status: saved.learning_status ?? current.state.learning_status,
                evidence: saved.evidence ?? current.state.evidence,
              },
            }
          : current
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      if (err instanceof PracticeRequestError && err.status === 409) setConflict(true)
    } finally {
      pending.current = false
      setBusy(false)
      onBusy(false)
    }
  }
  function reset() {
    setQuestion(null)
    setAnswer('')
    setExposed(false)
    setResult(null)
    setError('')
    setConflict(false)
    submission.current = null
    exposureId.current = null
    setReload(value => value + 1)
  }
  const entry = question?.entry
  const multi =
    entry?.question_type === 'multi_choice' || entry?.question_type === 'multiple_select'
  const choices = Object.entries(entry?.options || {})
  const subjective =
    ['short_answer', 'essay', 'free_response', 'qualitative'].includes(
      entry?.question_type ?? ''
    ) ||
    (choices.length === 0 && !['fill_blank', 'true_false'].includes(entry?.question_type ?? ''))
  const questionImages = (entry?.question_images || []).filter(image => Boolean(image.url))
  const locked = busy || !!result || !!submission.current
  if (variantSession)
    return (
      <PracticeSession
        questions={[variantSession]}
        onClose={() => {
          setVariantSession(null)
          setVariant(null)
        }}
      />
    )
  if (variant)
    return (
      <VariantPractice
        reference={variant}
        onClose={() => setVariant(null)}
        onPractice={value => {
          setVariant(null)
          setVariantSession(value)
        }}
      />
    )
  return (
    <div className="rounded-2xl border border-border bg-card p-5 sm:p-8">
      {error && (
        <div role="alert" className="mb-4 rounded-lg bg-destructive/5 p-3 text-sm text-destructive">
          <p>{error}</p>
          {(conflict || !entry) && (
            <button type="button" onClick={reset} className="mt-2 underline">
              {t('Reload question')}
            </button>
          )}
        </div>
      )}
      {!entry ? (
        <p role="status" className="py-8 text-sm text-muted-foreground">
          {t('Loading…')}
        </p>
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>{entry.material_title || t('Practice')}</span>
            {workspaceId !== undefined && (
              <WorkspaceLabel
                row={{ content_workspace_id: workspaceId, content_workspace_name: workspaceName }}
              />
            )}
            {entry.categories?.map(tag => (
              <span key={tag.id} className="rounded-full bg-muted px-2 py-1">
                {tag.name}
              </span>
            ))}
            {question && <LearningStatus state={question.state} />}
          </div>
          <Markdown
            content={practiceMarkdown(entry.question)}
            allowHtml={false}
            className="text-base leading-relaxed [overflow-wrap:anywhere] [&_pre]:max-w-full [&_pre]:overflow-x-auto"
          />
          {questionImages.length > 0 && (
            <div className={`mt-4 grid gap-3 ${questionImages.length > 1 ? 'sm:grid-cols-2' : ''}`}>
              {questionImages.map((image, imageIndex) => {
                const url = apiUrl(
                  scopedUrl(image.url, workspaceId ?? question?.content_workspace_id)
                )
                return (
                  <a
                    key={`${image.id || image.url}:${imageIndex}`}
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className="overflow-hidden rounded-xl border border-border bg-muted/20"
                  >
                    {/* Authenticated attachment URLs retain the question's workspace. */}
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={url}
                      alt={t('Saved original photo {{number}}', { number: imageIndex + 1 })}
                      className="max-h-[60vh] w-full object-contain"
                    />
                    {image.filename && (
                      <span className="block truncate border-t border-border px-3 py-2 text-xs text-muted-foreground">
                        {image.filename}
                      </span>
                    )}
                  </a>
                )
              })}
            </div>
          )}
          {multi && (
            <p className="mt-3 text-xs text-muted-foreground">{t('Select all correct answers')}</p>
          )}
          {choices.length > 0 ? (
            <fieldset disabled={locked} className="mt-6 grid gap-2">
              <legend className="sr-only">{t('Your answer')}</legend>
              {choices.map(([key, text]) => {
                const selected = multi ? answer.split(',').includes(key) : answer === key
                return (
                  <label
                    key={key}
                    className={`flex cursor-pointer items-start gap-3 rounded-xl border p-3 text-sm ${selected ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted/40'}`}
                  >
                    <input
                      type={multi ? 'checkbox' : 'radio'}
                      name={`practice-${id}`}
                      checked={selected}
                      value={key}
                      onChange={() =>
                        setAnswer(
                          multi
                            ? (selected
                                ? answer.split(',').filter(value => value !== key)
                                : [...answer.split(',').filter(Boolean), key]
                              )
                                .sort()
                                .join(',')
                            : key
                        )
                      }
                      className="mt-1 accent-primary"
                    />
                    <span className="font-semibold">{key}</span>
                    <div className="min-w-0">
                      <Markdown content={practiceMarkdown(text)} allowHtml={false} />
                    </div>
                  </label>
                )
              })}
            </fieldset>
          ) : (
            <label className="mt-6 block text-sm font-medium">
              {t('Your answer')}
              <textarea
                value={answer}
                onChange={event => setAnswer(event.target.value)}
                disabled={locked}
                maxLength={20000}
                rows={5}
                placeholder={t('Try recalling the answer before revealing it.')}
                className="mt-2 block w-full resize-y rounded-xl border border-border bg-background p-3 font-normal outline-none focus:border-primary"
              />
            </label>
          )}
          {!result ? (
            <>
              <label className="mt-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                {t(subjective ? 'My self-review' : 'How did this attempt feel?')}
                <select
                  disabled={locked}
                  value={rating}
                  onChange={event => setRating(event.target.value as PracticeRating)}
                  className="rounded-lg border border-border bg-background px-2 py-1.5 text-foreground"
                >
                  <option value="good">{t('Completed independently')}</option>
                  <option value="hard">{t('Completed with effort')}</option>
                  <option value="easy">{t('Easy')}</option>
                  <option value="again">{t('Needs another try')}</option>
                </select>
              </label>
              {subjective && (
                <p className="mt-2 text-xs leading-6 text-muted-foreground">
                  {t(
                    'This answer needs human review. Self-review is saved but cannot automatically prove mastery.'
                  )}
                </p>
              )}
              <div className="mt-5 flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  disabled={busy || conflict || !answer.trim()}
                  onClick={() => void submit()}
                  className="inline-flex items-center gap-2 rounded-xl bg-primary px-4 py-2.5 text-sm text-primary-foreground disabled:opacity-40"
                >
                  {busy && <Loader2 size={15} className="animate-spin" />}
                  {t(
                    busy
                      ? 'Saving review…'
                      : submission.current
                        ? 'Retry submission'
                        : exposed
                          ? 'Record corrected answer'
                          : 'Submit and save answer'
                  )}
                </button>
                {!exposed && !submission.current && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void showReference()}
                    className="rounded-xl border border-border px-4 py-2.5 text-sm hover:bg-muted"
                  >
                    {t('Show reference answer')}
                  </button>
                )}
                <button
                  type="button"
                  onClick={onSkip}
                  disabled={busy || !!submission.current}
                  className="text-sm text-muted-foreground hover:underline"
                >
                  {t('Skip for now')}
                </button>
              </div>
            </>
          ) : (
            <div
              className={`mt-6 rounded-xl p-4 ${result.closed ? 'bg-emerald-500/10' : result.correct === false ? 'bg-destructive/5' : 'bg-muted/50'}`}
              role="status"
            >
              <h3 className="flex items-center gap-2 text-sm font-semibold">
                {result.closed && <ShieldCheck size={17} />}
                {t(
                  result.closed
                    ? 'This question reached verified mastery'
                    : result.reopened
                      ? 'Reopened for review'
                      : result.correct === false
                        ? 'Needs another try'
                        : result.correct === true && !exposed && result.independent !== false
                          ? 'Correct attempt saved'
                          : 'Assisted or unverified review'
                )}
              </h3>
              <p className="mt-2 text-xs leading-6 text-muted-foreground">
                {t(
                  result.closed
                    ? 'This question leaves daily recommendations. Its learning history stays available for future spot checks.'
                    : result.reopened
                      ? 'A same-skill mistake reopened this question. Earlier learning and closure records are preserved.'
                      : 'A correct attempt is progress. Verified mastery also needs spaced practice, a different question and delayed verification.'
                )}
              </p>
            </div>
          )}
          {(exposed || result) && (
            <div className="mt-5 rounded-xl border border-border bg-muted/30 p-4">
              <h3 className="mb-3 text-sm font-semibold">{t('Reference answer')}</h3>
              <Markdown
                content={
                  entry.correct_answer ||
                  t('No reference answer was saved. Use the original source to check your work.')
                }
                allowHtml={false}
              />
              {entry.explanation && (
                <div className="mt-3 border-t border-border pt-3 text-sm">
                  <Markdown content={entry.explanation} allowHtml={false} />
                </div>
              )}
              {exposed && !result && (
                <p className="mt-3 text-xs leading-6 text-amber-800 dark:text-amber-300">
                  {t(
                    'Answer exposure is recorded. Retrying immediately cannot count as independent mastery.'
                  )}
                </p>
              )}
            </div>
          )}
          {result && (
            <>
              <div className="mt-5">
                <h3 className="mb-3 text-xs font-semibold">{t('Mastery evidence')}</h3>
                <MasteryEvidenceList evidence={result.parent_state?.evidence ?? result.evidence} />
              </div>
              {result.learning_status !== 'closed' && (
                <p className="mt-3 text-xs text-muted-foreground">
                  {t('Next review: {{date}}', {
                    date: new Date(result.due_at * 1000).toLocaleDateString(),
                  })}
                </p>
              )}
              <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap gap-3">
                  <button
                    type="button"
                    onClick={() => setDetails(true)}
                    className="text-xs text-muted-foreground hover:underline"
                  >
                    {t('Learning history and mastery')}
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setVariant({ ...reference, id: result.parent_entry_id ?? reference.id })
                    }
                    className="text-xs text-primary hover:underline"
                  >
                    {t('Practice a related question')}
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => onSaved(result)}
                  className="inline-flex items-center gap-2 rounded-xl bg-primary px-5 py-2.5 text-sm text-primary-foreground"
                >
                  {t('Next question')}
                  <ArrowRight size={15} />
                </button>
              </div>
            </>
          )}
          {details && (
            <MasteryDetails
              question={question ?? undefined}
              reference={reference}
              onClose={() => setDetails(false)}
              onPractice={() => {
                setDetails(false)
                reset()
              }}
            />
          )}
        </>
      )}
    </div>
  )
}
