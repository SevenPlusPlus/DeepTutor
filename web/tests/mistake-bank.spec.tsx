import { useState, type ReactNode } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import QuestionCard from '@/components/space/question-bank/QuestionCard'
import BankToolbar from '@/components/space/question-bank/BankToolbar'
import {
  buildQuestionBankFilter,
  type ReviewFilters,
} from '@/components/space/question-bank/useQuestionBank'
import { initI18n } from '@/i18n/init'
import type { NotebookEntry } from '@/lib/notebook-api'
import * as practice from '@/lib/practice-api'

vi.mock('next/link', () => ({
  default: ({ href, children }: { href: string; children: ReactNode }) => (
    <a href={href}>{children}</a>
  ),
}))
vi.mock('@/components/common/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div>{content}</div>,
}))
vi.mock('@/components/space/question-bank/CategoryMenu', () => ({ default: () => <span /> }))
vi.mock('@/shared/ui/Tooltip', () => ({
  default: ({ children }: { children: ReactNode }) => <>{children}</>,
}))
vi.mock('@/lib/workspace-scope', async original => ({
  ...(await original<typeof import('@/lib/workspace-scope')>()),
  activeWorkspaceId: () => 'active-elsewhere',
}))
vi.mock('@/lib/practice-api', async original => ({
  ...(await original<typeof practice>()),
  recordPracticeExposure: vi.fn(),
}))
initI18n('en')

const entry = (changes: Partial<NotebookEntry> = {}): NotebookEntry => ({
  id: 12,
  session_id: '',
  session_title: '',
  turn_id: '',
  question_id: 'bank-12',
  question: 'Solve the equation.',
  question_type: 'fill_blank',
  options: {},
  correct_answer: 'answer-hidden-until-recorded',
  explanation: 'Worked explanation hidden by default',
  difficulty: 'easy',
  user_answer: 'old attempt',
  source: 'import',
  material_id: '',
  material_title: '',
  section_id: '',
  section_title: '',
  score_trend: 'new',
  is_correct: false,
  resolved: false,
  bookmarked: false,
  followup_session_id: '',
  created_at: 100,
  updated_at: 100,
  practice: {
    is_mistake: true,
    due_at: 100,
    review_count: 1,
    last_answer: 'latest saved attempt',
    last_rating: 'again',
    version: 2,
    learning_status: 'open',
    paused: false,
  },
  ...changes,
})

function Card({
  item = entry(),
  onToggleResolved = vi.fn(),
}: {
  item?: NotebookEntry
  onToggleResolved?: () => void
}) {
  return (
    <QuestionCard
      entry={item}
      mistakesOnly
      categories={[]}
      selected={false}
      disabled={false}
      onToggleSelected={vi.fn()}
      onToggleBookmark={vi.fn()}
      onToggleResolved={onToggleResolved}
      onInspect={vi.fn()}
      onDelete={vi.fn()}
      onFile={vi.fn(async () => true)}
      onUnfile={vi.fn(async () => true)}
      onCreateAndFile={vi.fn(async () => true)}
    />
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(practice.recordPracticeExposure).mockResolvedValue({ recorded: true })
})

it('hides original and latest answers until answer exposure has been durably recorded', async () => {
  let finish!: (value: { recorded: boolean }) => void
  vi.mocked(practice.recordPracticeExposure).mockImplementation(
    () =>
      new Promise(resolve => {
        finish = resolve
      })
  )
  render(
    <ul>
      <Card />
    </ul>
  )
  expect(screen.getByText('answer-hidden-until-recorded')).not.toBeVisible()
  expect(screen.getByText('Worked explanation hidden by default')).not.toBeVisible()
  expect(screen.getByText('latest saved attempt')).not.toBeVisible()
  fireEvent.click(screen.getByText('Answer and explanation'))
  expect(screen.getByText('answer-hidden-until-recorded')).not.toBeVisible()
  expect(screen.getByText('Worked explanation hidden by default')).not.toBeVisible()
  await act(async () => finish({ recorded: true }))
  expect(screen.getByText('answer-hidden-until-recorded')).toBeVisible()
  expect(screen.getByText('Worked explanation hidden by default')).toBeVisible()
  expect(practice.recordPracticeExposure).toHaveBeenCalledWith(
    12,
    expect.any(String),
    'answer',
    'active-elsewhere'
  )
})

it('keeps answers hidden after an exposure error and retries the same request', async () => {
  vi.mocked(practice.recordPracticeExposure).mockRejectedValueOnce(
    new Error('Could not save exposure')
  )
  render(
    <ul>
      <Card />
    </ul>
  )
  fireEvent.click(screen.getByText('Answer and explanation'))
  await screen.findByRole('alert')
  expect(screen.getByText('answer-hidden-until-recorded')).not.toBeVisible()
  fireEvent.click(screen.getByText('Answer and explanation'))
  await waitFor(() => expect(screen.getByText('answer-hidden-until-recorded')).toBeVisible())
  expect(vi.mocked(practice.recordPracticeExposure).mock.calls[0]).toEqual(
    vi.mocked(practice.recordPracticeExposure).mock.calls[1]
  )
})

it('records a new exposure when an answer is hidden and opened again', async () => {
  render(
    <ul>
      <Card />
    </ul>
  )
  fireEvent.click(screen.getByText('Answer and explanation'))
  await waitFor(() => expect(screen.getByText('answer-hidden-until-recorded')).toBeVisible())
  const first = vi.mocked(practice.recordPracticeExposure).mock.calls[0][1]
  fireEvent.click(screen.getByText('Answer and explanation'))
  expect(screen.getByText('answer-hidden-until-recorded')).not.toBeVisible()
  fireEvent.click(screen.getByText('Answer and explanation'))
  await waitFor(() => expect(screen.getByText('answer-hidden-until-recorded')).toBeVisible())
  expect(practice.recordPracticeExposure).toHaveBeenCalledTimes(2)
  expect(vi.mocked(practice.recordPracticeExposure).mock.calls[1][1]).not.toBe(first)
})

it('records equal question IDs in their captured source workspaces, including the default workspace', async () => {
  const items = [entry({ content_workspace_id: 'source-a' }), entry({ content_workspace_id: '' })]
  render(
    <ul>
      {items.map((item, index) => (
        <Card key={index} item={item} />
      ))}
    </ul>
  )
  const cards = screen.getAllByRole('listitem')
  for (const card of cards) {
    fireEvent.click(within(card).getByText('Answer and explanation'))
    await waitFor(() =>
      expect(within(card).getByText('answer-hidden-until-recorded')).toBeVisible()
    )
  }
  expect(vi.mocked(practice.recordPracticeExposure).mock.calls.map(call => call[3])).toEqual([
    'source-a',
    '',
  ])
})

it('keeps verified mastery evidence separate from pause and manual mastery controls', () => {
  const item = entry({
    practice: { ...entry().practice!, learning_status: 'closed', closed_at: 100 },
  })
  render(
    <ul>
      <Card item={item} />
    </ul>
  )
  expect(screen.getByText('Verified mastery')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Pause review' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Resume review' })).not.toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: /mark.*master|i have mastered|mark.*resolved/i })
  ).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Learning history and mastery' })).toBeEnabled()
})

it('offers resume for a paused mistake without claiming that it is mastered', () => {
  const toggle = vi.fn()
  const item = entry({ practice: { ...entry().practice!, paused: true } })
  render(
    <ul>
      <Card item={item} onToggleResolved={toggle} />
    </ul>
  )
  expect(screen.getByText('Paused')).toBeVisible()
  expect(screen.queryByText('Verified mastery')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Resume review' }))
  expect(toggle).toHaveBeenCalledOnce()
})

it('keeps WeChat source, first receipt time and the reviewed grade visible', () => {
  const received = 1793271600
  const item = entry({
    recognition_meta: {
      photo_receipt_id: 'receipt-7',
      photo_source_channel: 'weixin',
      subject: 'math',
      grade: '五年级',
      first_uploaded_at: received,
    },
  })
  render(
    <ul>
      <Card item={item} />
    </ul>
  )
  const source = screen.getByText(/WeChat question photo/)
  expect(source).toHaveTextContent('Math')
  expect(source).toHaveTextContent('五年级')
  expect(source).toHaveTextContent(`First received: ${new Date(received * 1000).toLocaleString()}`)
  expect(screen.getByText('Worked explanation hidden by default')).not.toBeVisible()
})

it('preserves canonical subject and learning state alongside course, tags and search constraints', () => {
  const changed = vi.fn()
  function Filters() {
    const [filters, setFilters] = useState<ReviewFilters>({
      source: '',
      materialId: '',
      scoreTrend: '',
    })
    return (
      <BankToolbar
        search="fractions"
        sort="recent"
        refreshing={false}
        managerOpen={false}
        filters={filters}
        materials={[]}
        onSearchChange={vi.fn()}
        onSortChange={vi.fn()}
        onToggleManager={vi.fn()}
        onFiltersChange={next => {
          changed(next)
          setFilters(next)
        }}
      />
    )
  }
  render(<Filters />)
  fireEvent.change(screen.getByRole('combobox', { name: 'Subject' }), { target: { value: 'math' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Learning status' }), {
    target: { value: 'verify' },
  })
  const filters = changed.mock.lastCall?.[0] as ReviewFilters
  expect(filters).toMatchObject({ subject: 'math', learningStatus: 'verify' })
  expect(
    buildQuestionBankFilter(
      { kind: 'category', categoryId: 8 },
      { ...filters, grade: '五年级' },
      'fractions',
      'recent',
      'course-a'
    )
  ).toMatchObject({
    category_id: 8,
    learning_status: 'verify',
    subject: 'math',
    grade: '五年级',
    search: 'fractions',
    course_id: 'course-a',
  })
  expect(
    buildQuestionBankFilter(
      { kind: 'unresolved' },
      { source: '', materialId: '', scoreTrend: '' },
      '',
      'recent'
    ).learning_status
  ).toBe('active')
})
