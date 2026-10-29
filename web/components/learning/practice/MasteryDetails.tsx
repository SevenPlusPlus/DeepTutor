'use client'

import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  ArrowRight,
  CheckCircle2,
  Circle,
  Loader2,
  Pause,
  RotateCcw,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import {
  getPracticeMastery,
  updatePracticeLearning,
  type MasteryEvidence,
  type PracticeMastery,
  type PracticeQuestion,
  type PracticeRef,
  type PracticeState,
} from '@/lib/practice-api'
import { WorkspaceLabel } from '../LibraryWorkspace'

export function LearningStatus({ state }: { state: PracticeState }) {
  const { t } = useTranslation()
  const label = state.paused
    ? 'Paused'
    : state.learning_status === 'closed'
      ? 'Verified mastery'
      : state.learning_status === 'verify'
        ? 'Awaiting verification'
        : 'Still learning'
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-1 text-xs ${state.learning_status === 'closed' ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400' : state.learning_status === 'verify' ? 'bg-amber-500/10 text-amber-800 dark:text-amber-300' : 'bg-muted text-muted-foreground'}`}
    >
      {state.learning_status === 'closed' && <ShieldCheck size={12} />}
      {t(label)}
    </span>
  )
}

export function MasteryEvidenceList({ evidence }: { evidence?: MasteryEvidence }) {
  const { t } = useTranslation()
  return (
    <div className="space-y-2.5" aria-label={t('Mastery evidence')}>
      {(
        [
          [evidence?.spaced, 'Independent success on separate days'],
          [evidence?.transfer, 'A different question on the same skill passed independently'],
          [
            evidence?.delayed,
            'Delayed verification at least seven days after the previous success',
          ],
        ] as const
      ).map(([done, label]) => (
        <p
          key={label}
          className={`flex items-start gap-2 text-xs leading-5 ${done ? 'text-emerald-700 dark:text-emerald-400' : 'text-muted-foreground'}`}
        >
          {done ? (
            <CheckCircle2 className="mt-0.5 shrink-0" size={15} />
          ) : (
            <Circle className="mt-0.5 shrink-0" size={15} />
          )}
          <span>
            {t(label)}
            <span className="sr-only"> · {t(done ? 'Complete' : 'Not yet')}</span>
          </span>
        </p>
      ))}
    </div>
  )
}

export function MasteryDetails({
  question,
  questionText,
  reference,
  onClose,
  onChanged,
  onPractice,
  onVariant,
}: {
  question?: PracticeQuestion
  questionText?: string
  reference: PracticeRef
  onClose: () => void
  onChanged?: () => void
  onPractice: (reference: PracticeRef) => void
  onVariant?: (reference: PracticeRef) => void
}) {
  const { t } = useTranslation()
  const [details, setDetails] = useState<PracticeMastery | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const panel = useRef<HTMLDivElement>(null)
  const close = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    const prior = document.activeElement as HTMLElement | null
    close.current?.focus()
    return () => {
      if (prior?.isConnected) prior.focus()
    }
  }, [])
  useEffect(() => {
    let active = true
    setError('')
    void getPracticeMastery(reference.id, reference.content_workspace_id)
      .then(value => {
        if (active) setDetails(value)
      })
      .catch(err => {
        if (active) setError(err instanceof Error ? err.message : String(err))
      })
    return () => {
      active = false
    }
  }, [reference.id, reference.content_workspace_id, revision])
  async function togglePause() {
    if (!details || busy) return
    setBusy(true)
    setError('')
    try {
      await updatePracticeLearning(
        reference.id,
        details.state.version,
        !details.state.paused,
        reference.content_workspace_id
      )
      setRevision(value => value + 1)
      onChanged?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }
  const state = details?.state ?? question?.state
  const kinds: Record<string, string> = {
    original: 'Original question',
    similar: 'Similar question',
    advanced: 'Advanced question',
    spotcheck: 'Mastery spot check',
  }
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={event => {
        if (event.target === event.currentTarget && !busy) onClose()
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="mastery-detail-title"
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-border bg-background shadow-xl"
        onKeyDown={event => {
          if (event.key === 'Escape' && !busy) onClose()
          if (event.key !== 'Tab') return
          const nodes = panel.current?.querySelectorAll<HTMLElement>(
            'button:not(:disabled),a[href],input,select,textarea'
          )
          if (!nodes?.length) return
          const first = nodes[0],
            last = nodes[nodes.length - 1]
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault()
            last.focus()
          }
          if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault()
            first.focus()
          }
        }}
      >
        <header className="flex items-center justify-between gap-4 border-b border-border p-5">
          <h2 id="mastery-detail-title" className="font-semibold">
            {t('Learning history and mastery')}
          </h2>
          <button
            ref={close}
            type="button"
            disabled={busy}
            onClick={onClose}
            aria-label={t('Close')}
            className="rounded-lg p-2 hover:bg-muted"
          >
            <X size={18} />
          </button>
        </header>
        <div className="space-y-6 overflow-y-auto p-5 sm:p-6">
          {error && (
            <div role="alert" className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
              {error}
              <button
                type="button"
                onClick={() => setRevision(value => value + 1)}
                className="ml-3 underline"
              >
                {t('Retry')}
              </button>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            {state && <LearningStatus state={state} />}
            <WorkspaceLabel row={reference} />
          </div>
          {(question?.entry.question || questionText) && (
            <p className="text-sm leading-7 [overflow-wrap:anywhere]">{question?.entry.question || questionText}</p>
          )}
          {!details ? (
            <p role="status" className="text-sm text-muted-foreground">
              {t('Loading…')}
            </p>
          ) : (
            <>
              <section>
                <h3 className="mb-3 text-sm font-semibold">{t('Mastery evidence')}</h3>
                <MasteryEvidenceList evidence={details.evidence} />
                <p className="mt-3 text-xs leading-5 text-muted-foreground">
                  {t(
                    'Hints, answer exposure and self-review do not count as verified independent mastery. These thresholds are a configurable initial policy.'
                  )}
                </p>
              </section>
              {state?.closed_at && (
                <p className="rounded-xl bg-emerald-500/10 p-3 text-xs text-emerald-700 dark:text-emerald-400">
                  {t('Verified on {{date}}', {
                    date: new Date(state.closed_at * 1000).toLocaleDateString(),
                  })}
                </p>
              )}
              <section>
                <h3 className="mb-3 text-sm font-semibold">{t('Review history')}</h3>
                {details.history.length ? (
                  <ol className="space-y-3 border-l border-border pl-4">
                    {details.history
                      .slice()
                      .reverse()
                      .map((attempt, index) => (
                        <li key={`${attempt.reviewed_at}:${index}`} className="text-xs leading-5">
                          <p className="font-medium">
                            {new Date(attempt.reviewed_at * 1000).toLocaleString()} ·{' '}
                            {t(kinds[attempt.kind] ?? 'Original question')}
                          </p>
                          <p className="text-muted-foreground">
                            {t(
                              attempt.correct === false
                                ? 'Needs another try'
                                : attempt.independent
                                  ? 'Independent correct answer'
                                  : 'Assisted or unverified review'
                            )}
                          </p>
                        </li>
                      ))}
                  </ol>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    {t('No reviews yet. Try this question independently first.')}
                  </p>
                )}
              </section>
              {details.decisions.length > 0 && (
                <section>
                  <h3 className="mb-3 text-sm font-semibold">{t('Mastery decisions')}</h3>
                  <ul className="space-y-2">
                    {details.decisions
                      .slice()
                      .reverse()
                      .map((decision, index) => (
                        <li key={`${decision.decided_at}:${index}`} className="text-xs leading-5">
                          <span className="font-medium">
                            {t(
                              decision.action === 'closed' || decision.action === 'close'
                                ? 'Verified mastery'
                                : 'Reopened for review'
                            )}
                          </span>
                          <span className="ml-2 text-muted-foreground">
                            {new Date(decision.decided_at * 1000).toLocaleString()}
                          </span>
                        </li>
                      ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </div>
        <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-border p-5">
          {details && state?.learning_status !== 'closed' ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => void togglePause()}
              className="inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
            >
              {busy ? (
                <Loader2 size={14} className="animate-spin" />
              ) : state?.paused ? (
                <RotateCcw size={14} />
              ) : (
                <Pause size={14} />
              )}
              {t(state?.paused ? 'Resume review' : 'Pause review')}
            </button>
          ) : (
            <span />
          )}
          <div className="flex flex-wrap gap-2">
            {onVariant && (
              <button
                type="button"
                disabled={busy || !details}
                onClick={() => onVariant(reference)}
                className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-xs hover:bg-muted"
              >
                <Sparkles size={14} />
                {t('Practice a related question')}
              </button>
            )}
            <button
              type="button"
              disabled={busy || !details || state?.paused}
              onClick={() =>
                onPractice({
                  ...reference,
                  attempt_kind: state?.learning_status === 'closed' ? 'spotcheck' : 'original',
                })
              }
              className="inline-flex items-center gap-2 rounded-lg bg-primary px-3 py-2 text-xs text-primary-foreground"
            >
              {t(
                state?.learning_status === 'closed'
                  ? 'Start a spot check'
                  : 'Practice this question'
              )}
              <ArrowRight size={14} />
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}
