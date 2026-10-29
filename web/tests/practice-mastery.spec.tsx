import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { DailyReviewPlan } from '@/components/learning/practice/DailyReviewPlan'
import { MasteryDetails } from '@/components/learning/practice/MasteryDetails'
import { VariantPractice } from '@/components/learning/practice/VariantPractice'
import { initI18n } from '@/i18n/init'
import * as api from '@/lib/practice-api'

vi.mock('next/link', () => ({
  default: ({ href, children }: { href: string; children: import('react').ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}))
vi.mock('@/lib/practice-api', async original => ({
  ...(await original<typeof api>()),
  getPracticeRecommendations: vi.fn(),
  getPracticeMastery: vi.fn(),
  updatePracticeLearning: vi.fn(),
  generatePracticeVariant: vi.fn(),
  confirmPracticeVariant: vi.fn(),
}))
initI18n('en')
const evidence = { spaced: true, transfer: false, delayed: false }
const row = (workspace: string): api.PracticeQuestion => ({
  entry: {
    id: 4,
    question: `Question in ${workspace}`,
    categories: [],
  } as unknown as api.PracticeQuestion['entry'],
  state: {
    version: 2,
    review_count: 1,
    is_mistake: true,
    due_at: 100,
    learning_status: 'verify',
    evidence,
  },
  content_workspace_id: workspace,
  recommendation: { reasons: ['Review is due'], score: 12, estimated_minutes: 3 },
})
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.getPracticeRecommendations).mockResolvedValue({
    items: [row('source-a'), row('source-b')],
    estimated_minutes: 6,
    budget_minutes: 15,
  })
  vi.mocked(api.getPracticeMastery).mockResolvedValue({
    state: row('source-a').state,
    evidence,
    history: [],
    decisions: [],
  })
  vi.mocked(api.updatePracticeLearning).mockResolvedValue({
    ...row('source-a').state,
    paused: true,
  })
})

it('starts a bounded recommendation group without losing duplicate IDs across workspaces', async () => {
  const start = vi.fn()
  render(<DailyReviewPlan summary={null} workspaceId="*" onStart={start} />)
  await screen.findByText('Question in source-a')
  await screen.findByText('Question in source-b')
  fireEvent.click(screen.getByRole('button', { name: "Start today's review" }))
  expect(start).toHaveBeenCalledWith([
    { id: 4, content_workspace_id: 'source-a', content_workspace_name: undefined },
    { id: 4, content_workspace_id: 'source-b', content_workspace_name: undefined },
  ])
  expect(api.getPracticeRecommendations).toHaveBeenCalledWith(
    '',
    '*',
    expect.objectContaining({ budget_minutes: 15, include_early: false })
  )
  fireEvent.click(screen.getByRole('button', { name: '10 min' }))
  await waitFor(() =>
    expect(api.getPracticeRecommendations).toHaveBeenLastCalledWith(
      '',
      '*',
      expect.objectContaining({ budget_minutes: 10 })
    )
  )
})

it('retains explicit query and subject constraints when opting into early practice', async () => {
  render(
    <DailyReviewPlan courseId="course-a" summary={null} workspaceId="source-a" onStart={vi.fn()} />
  )
  await screen.findByText('Question in source-a')
  fireEvent.change(screen.getByRole('textbox', { name: 'Search mistake topics or tags' }), {
    target: { value: 'fractions' },
  })
  fireEvent.change(screen.getByRole('combobox', { name: 'Subject filter' }), {
    target: { value: 'math' },
  })
  fireEvent.change(screen.getByRole('textbox', { name: 'Grade filter' }), {
    target: { value: '五年级' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Find questions' }))
  await waitFor(() =>
    expect(api.getPracticeRecommendations).toHaveBeenLastCalledWith(
      'course-a',
      'source-a',
      expect.objectContaining({
        query: 'fractions',
        subject: 'math',
        grade: '五年级',
        include_early: false,
      })
    )
  )
  fireEvent.click(
    screen.getByRole('checkbox', { name: 'Include not-yet-due questions for focused practice' })
  )
  await waitFor(() =>
    expect(api.getPracticeRecommendations).toHaveBeenLastCalledWith(
      'course-a',
      'source-a',
      expect.objectContaining({
        query: 'fractions',
        subject: 'math',
        grade: '五年级',
        include_early: true,
      })
    )
  )
})

it('ignores stale recommendation responses after the budget changes', async () => {
  let finish!: (value: api.PracticeRecommendations) => void
  vi.mocked(api.getPracticeRecommendations).mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve
      })
  )
  vi.mocked(api.getPracticeRecommendations).mockResolvedValue({
    items: [],
    budget_minutes: 10,
    estimated_minutes: 0,
  })
  render(<DailyReviewPlan summary={null} onStart={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '10 min' }))
  await screen.findByText(
    'No matching questions are due. Adjust the filters or choose focused early practice.'
  )
  await act(async () => finish({ items: [row('stale')], budget_minutes: 15, estimated_minutes: 3 }))
  expect(screen.queryByText('Question in stale')).not.toBeInTheDocument()
})

it('loads evidence and pauses only the owning workspace using its saved version', async () => {
  const changed = vi.fn()
  render(
    <MasteryDetails
      reference={{ id: 4, content_workspace_id: 'source-a' }}
      onClose={vi.fn()}
      onChanged={changed}
      onPractice={vi.fn()}
    />
  )
  await screen.findByText('No reviews yet. Try this question independently first.')
  fireEvent.click(screen.getByRole('button', { name: 'Pause review' }))
  await waitFor(() =>
    expect(api.updatePracticeLearning).toHaveBeenCalledWith(4, 2, true, 'source-a')
  )
  expect(changed).toHaveBeenCalledOnce()
  expect(api.getPracticeMastery).toHaveBeenCalledWith(4, 'source-a')
})

it('starts a closed question as a spot check rather than allowing manual mastery', async () => {
  const start = vi.fn()
  vi.mocked(api.getPracticeMastery).mockResolvedValue({
    state: { ...row('source-a').state, learning_status: 'closed', closed_at: 100 },
    evidence: { spaced: true, transfer: true, delayed: true },
    history: [],
    decisions: [],
  })
  render(
    <MasteryDetails
      reference={{ id: 4, content_workspace_id: 'source-a' }}
      onClose={vi.fn()}
      onPractice={start}
    />
  )
  fireEvent.click(await screen.findByRole('button', { name: 'Start a spot check' }))
  expect(start).toHaveBeenCalledWith({
    id: 4,
    content_workspace_id: 'source-a',
    attempt_kind: 'spotcheck',
  })
  expect(screen.queryByRole('button', { name: 'Pause review' })).not.toBeInTheDocument()
})

it('keeps generation failures retryable and requires reviewed confirmation before a variant is practiced', async () => {
  const start = vi.fn()
  const draft: api.PracticeVariantDraft = {
    draft_id: 'draft-1',
    version: 3,
    parent_entry_id: 4,
    kind: 'similar',
    status: 'ready',
    question: 'New equation',
    question_type: 'single_choice',
    options: { A: '4', B: '5' },
    correct_answer: 'A',
    explanation: 'Subtract first.',
    difficulty: 'easy',
    knowledge_point: 'Equations',
    target_skill: 'linear-equations',
    introduces_new_skill: false,
  }
  vi.mocked(api.generatePracticeVariant)
    .mockRejectedValueOnce(new Error('Model unavailable'))
    .mockResolvedValueOnce(draft)
  vi.mocked(api.confirmPracticeVariant).mockResolvedValue({
    ...row('source-a'),
    entry: { ...row('source-a').entry, id: 9 },
  })
  render(
    <VariantPractice
      reference={{ id: 4, content_workspace_id: 'source-a' }}
      onClose={vi.fn()}
      onPractice={start}
    />
  )
  fireEvent.click(screen.getByRole('button', { name: 'Generate a review draft' }))
  await screen.findByText('Model unavailable')
  expect(screen.queryByLabelText('Question')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Generate a review draft' }))
  await screen.findByDisplayValue('New equation')
  expect(vi.mocked(api.generatePracticeVariant).mock.calls[0]).toEqual(
    vi.mocked(api.generatePracticeVariant).mock.calls[1]
  )
  expect(screen.getByRole('button', { name: 'Save reviewed question and practice' })).toBeDisabled()
  fireEvent.click(
    screen.getByRole('checkbox', {
      name: 'I checked the question, answer, explanation and skill alignment.',
    })
  )
  fireEvent.click(screen.getByRole('button', { name: 'Save reviewed question and practice' }))
  await waitFor(() =>
    expect(start).toHaveBeenCalledWith({
      id: 9,
      content_workspace_id: 'source-a',
      content_workspace_name: undefined,
    })
  )
  expect(api.confirmPracticeVariant).toHaveBeenCalledWith(4, draft, 'source-a')
})
