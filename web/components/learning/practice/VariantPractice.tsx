'use client'

import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ArrowLeft, ArrowRight, Loader2, Sparkles } from 'lucide-react'
import { randomUuid } from '@/lib/random-uuid'
import {
  confirmPracticeVariant,
  generatePracticeVariant,
  type PracticeRef,
  type PracticeVariantDraft,
} from '@/lib/practice-api'

export function VariantPractice({
  reference,
  onClose,
  onPractice,
}: {
  reference: PracticeRef
  onClose: () => void
  onPractice: (reference: PracticeRef) => void
}) {
  const { t } = useTranslation()
  const [kind, setKind] = useState<'similar' | 'advanced'>('similar')
  const [draft, setDraft] = useState<PracticeVariantDraft | null>(null)
  const [options, setOptions] = useState<Record<string, string>>({})
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)
  const requestId = useRef<string | null>(null)
  async function generate() {
    if (pending.current) return
    pending.current = true
    setBusy(true)
    setError('')
    requestId.current ??= randomUuid()
    try {
      const result = await generatePracticeVariant(
        reference.id,
        requestId.current,
        kind,
        reference.content_workspace_id
      )
      setDraft(result)
      setOptions(result.options)
      setConfirmed(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      pending.current = false
      setBusy(false)
    }
  }
  async function commit() {
    if (!draft || pending.current || !confirmed) return
    const parsed = ['single_choice', 'multi_choice', 'true_false'].includes(draft.question_type)
      ? options
      : {}
    pending.current = true
    setBusy(true)
    setError('')
    try {
      const result = await confirmPracticeVariant(
        reference.id,
        { ...draft, options: parsed },
        reference.content_workspace_id
      )
      onPractice({
        id: result.entry.id,
        content_workspace_id: result.content_workspace_id ?? reference.content_workspace_id,
        content_workspace_name: result.content_workspace_name ?? reference.content_workspace_name,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      pending.current = false
      setBusy(false)
    }
  }
  function patch<K extends keyof PracticeVariantDraft>(key: K, value: PracticeVariantDraft[K]) {
    setDraft(current => (current ? { ...current, [key]: value } : current))
    setConfirmed(false)
  }
  const choice =
    draft && ['single_choice', 'multi_choice', 'true_false'].includes(draft.question_type)
  const questionTypes = {
    single_choice: 'Single choice',
    multi_choice: 'Multiple choice',
    true_false: 'True / false',
    fill_blank: 'Fill in the blank',
    short_answer: 'Short answer',
  }
  const difficulties = { easy: 'Easy', medium: 'Medium', hard: 'Hard' }
  function removeOption(key: string) {
    setOptions(current =>
      Object.fromEntries(Object.entries(current).filter(([name]) => name !== key))
    )
    if (draft)
      patch(
        'correct_answer',
        draft.correct_answer
          .split(/[,;\s]+/)
          .filter(name => name !== key)
          .join(',')
      )
  }
  return (
    <section className="mx-auto max-w-3xl rounded-2xl border border-border bg-card p-5 sm:p-7">
      <button
        type="button"
        disabled={busy}
        onClick={onClose}
        className="mb-5 inline-flex items-center gap-2 text-xs text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft size={14} />
        {t('Back to practice')}
      </button>
      <h2 className="flex items-center gap-2 text-lg font-semibold">
        <Sparkles size={19} />
        {t('Practice a related question')}
      </h2>
      <p className="mt-2 text-xs leading-6 text-muted-foreground">
        {t(
          'Similar questions verify the same skill. Advanced questions can introduce a separate skill without erasing earlier mastery.'
        )}
      </p>
      <div className="mt-5 flex gap-2" aria-label={t('Related question type')}>
        {(['similar', 'advanced'] as const).map(value => (
          <button
            type="button"
            key={value}
            disabled={busy}
            aria-pressed={kind === value}
            onClick={() => {
              setKind(value)
              setDraft(null)
              setError('')
              requestId.current = null
            }}
            className={`rounded-lg border px-4 py-2 text-sm ${kind === value ? 'border-primary bg-primary/5 text-primary' : 'border-border hover:bg-muted'}`}
          >
            {t(value === 'similar' ? 'Similar question' : 'Advanced question')}
          </button>
        ))}
      </div>
      {error && (
        <p role="alert" className="mt-4 rounded-lg bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </p>
      )}
      {!draft ? (
        <button
          type="button"
          disabled={busy}
          onClick={() => void generate()}
          className="mt-6 inline-flex items-center gap-2 rounded-xl bg-primary px-4 py-2.5 text-sm text-primary-foreground disabled:opacity-50"
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <Sparkles size={16} />}
          {t(busy ? 'Generating question…' : 'Generate a review draft')}
        </button>
      ) : (
        <div className="mt-6 space-y-4">
          <p className="rounded-lg bg-amber-500/10 p-3 text-xs leading-6 text-amber-900 dark:text-amber-200">
            {t(
              'Review the generated question and answer before saving. An unreviewed draft is not a test of mastery.'
            )}
          </p>
          <label className="block text-xs font-medium">
            {t('Question')}
            <textarea
              value={draft.question}
              disabled={busy}
              onChange={event => patch('question', event.target.value)}
              rows={4}
              className="mt-1.5 w-full rounded-lg border border-border bg-background p-3 text-sm"
            />
          </label>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-xs font-medium">
              {t('Question type')}
              <select
                value={draft.question_type}
                disabled={busy}
                onChange={event =>
                  patch(
                    'question_type',
                    event.target.value as PracticeVariantDraft['question_type']
                  )
                }
                className="mt-1.5 w-full rounded-lg border border-border bg-background p-2.5 text-sm"
              >
                {(
                  [
                    'single_choice',
                    'multi_choice',
                    'true_false',
                    'fill_blank',
                    'short_answer',
                  ] as const
                ).map(value => (
                  <option key={value} value={value}>
                    {t(questionTypes[value])}
                  </option>
                ))}
              </select>
            </label>
            <label className="block text-xs font-medium">
              {t('Difficulty')}
              <select
                value={draft.difficulty}
                disabled={busy}
                onChange={event =>
                  patch('difficulty', event.target.value as PracticeVariantDraft['difficulty'])
                }
                className="mt-1.5 w-full rounded-lg border border-border bg-background p-2.5 text-sm"
              >
                {(['easy', 'medium', 'hard'] as const).map(value => (
                  <option key={value} value={value}>
                    {t(difficulties[value])}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {choice ? (
            <fieldset className="space-y-2">
              <legend className="mb-2 text-xs font-medium">
                {t('Options and correct answer')}
              </legend>
              <p className="mb-3 text-xs text-muted-foreground">
                {t('Select the correct option, then check each option text.')}
              </p>
              {Object.entries(options).map(([key, value]) => (
                <div key={key} className="flex items-start gap-2">
                  <label className="flex items-center gap-2 py-2.5 text-xs font-medium">
                    <input
                      type={draft.question_type === 'multi_choice' ? 'checkbox' : 'radio'}
                      name="variant-correct-answer"
                      aria-label={t('Mark option {{key}} correct', { key })}
                      checked={draft.correct_answer.split(/[,;\s]+/).includes(key)}
                      disabled={busy}
                      onChange={event =>
                        patch(
                          'correct_answer',
                          draft.question_type === 'multi_choice'
                            ? (event.target.checked
                                ? [...draft.correct_answer.split(/[,;\s]+/).filter(Boolean), key]
                                : draft.correct_answer.split(/[,;\s]+/).filter(item => item !== key)
                              ).join(',')
                            : key
                        )
                      }
                      className="accent-primary"
                    />
                    {key}
                  </label>
                  <input
                    value={value}
                    aria-label={t('Option {{key}}', { key })}
                    disabled={busy}
                    onChange={event => {
                      setOptions(current => ({ ...current, [key]: event.target.value }))
                      setConfirmed(false)
                    }}
                    className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm"
                  />
                  <button
                    type="button"
                    disabled={busy}
                    aria-label={t('Remove option {{key}}', { key })}
                    onClick={() => removeOption(key)}
                    className="rounded-lg px-2 py-2 text-xs text-muted-foreground hover:bg-muted"
                  >
                    {t('Remove')}
                  </button>
                </div>
              ))}
              <button
                type="button"
                disabled={busy || Object.keys(options).length >= 26}
                onClick={() => {
                  const key = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
                    .split('')
                    .find(value => !(value in options))
                  if (key) setOptions(current => ({ ...current, [key]: '' }))
                  setConfirmed(false)
                }}
                className="rounded-lg border border-border px-3 py-2 text-xs hover:bg-muted"
              >
                {t('Add option')}
              </button>
            </fieldset>
          ) : (
            <label className="block text-xs font-medium">
              {t('Correct answer')}
              <input
                value={draft.correct_answer}
                disabled={busy}
                onChange={event => patch('correct_answer', event.target.value)}
                className="mt-1.5 w-full rounded-lg border border-border bg-background p-3 text-sm"
              />
            </label>
          )}
          <label className="block text-xs font-medium">
            {t('Explanation')}
            <textarea
              value={draft.explanation}
              disabled={busy}
              onChange={event => patch('explanation', event.target.value)}
              rows={3}
              className="mt-1.5 w-full rounded-lg border border-border bg-background p-3 text-sm"
            />
          </label>
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-xs font-medium">
              {t('Knowledge point')}
              <input
                value={draft.knowledge_point}
                disabled={busy}
                onChange={event => patch('knowledge_point', event.target.value)}
                className="mt-1.5 w-full rounded-lg border border-border bg-background p-3 text-sm"
              />
            </label>
            <label className="block text-xs font-medium">
              {t('Target skill')}
              <input
                value={draft.target_skill}
                disabled={busy || kind === 'similar'}
                onChange={event => patch('target_skill', event.target.value)}
                className="mt-1.5 w-full rounded-lg border border-border bg-background p-3 text-sm disabled:opacity-60"
              />
            </label>
          </div>
          {kind === 'advanced' && (
            <label className="flex items-center gap-2 text-xs">
              <input
                type="checkbox"
                disabled={busy}
                checked={draft.introduces_new_skill}
                onChange={event => patch('introduces_new_skill', event.target.checked)}
                className="accent-primary"
              />
              {t('This question introduces a new skill')}
            </label>
          )}
          <label className="flex items-start gap-2 text-xs leading-6">
            <input
              type="checkbox"
              disabled={busy}
              checked={confirmed}
              onChange={event => setConfirmed(event.target.checked)}
              className="mt-1.5 accent-primary"
            />
            <span>{t('I checked the question, answer, explanation and skill alignment.')}</span>
          </label>
          <p className="text-xs leading-6 text-muted-foreground">
            {t(
              'Because this review shows the answer, an immediate practice attempt will not count as independent mastery evidence.'
            )}
          </p>
          <button
            type="button"
            disabled={busy || !confirmed || !draft.question.trim() || !draft.correct_answer.trim()}
            onClick={() => void commit()}
            className="inline-flex items-center gap-2 rounded-xl bg-primary px-4 py-2.5 text-sm text-primary-foreground disabled:opacity-40"
          >
            {busy ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />}
            {t('Save reviewed question and practice')}
          </button>
        </div>
      )}
    </section>
  )
}
