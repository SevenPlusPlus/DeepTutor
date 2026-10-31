import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { PracticeSession } from "@/components/learning/practice/PracticeSession";
import { PracticeImport } from "@/components/learning/practice/PracticeImport";
import { initI18n } from "@/i18n/init";
import * as api from "@/lib/practice-api";

vi.mock("next/link", () => ({ default: ({ href, children }: { href: string; children: import("react").ReactNode }) => <a href={href}>{children}</a> }));
vi.mock("next/dynamic", () => ({ default: () => ({ content }: { content: string }) => <div>{content}</div> }));
vi.mock("@/lib/practice-api", async importOriginal => ({
  ...(await importOriginal<typeof api>()), getPracticeQuestion: vi.fn(), savePracticeReview: vi.fn(),
  recordPracticeExposure: vi.fn(), previewPracticeImport: vi.fn(), startPracticeRecognition: vi.fn(),
  getCurrentPracticeRecognition: vi.fn(), getPracticeRecognition: vi.fn(), stagePracticeRecognition: vi.fn(),
  commitPracticeImport: vi.fn(), downloadPracticeTemplate: vi.fn(),
}));
initI18n("en");
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.getCurrentPracticeRecognition).mockResolvedValue(null);
  vi.mocked(api.recordPracticeExposure).mockResolvedValue({ recorded: true });
  vi.mocked(api.getPracticeQuestion).mockResolvedValue({
    entry: {
      id: 8, session_id: "lesson", session_title: "Lesson", turn_id: "", question_id: "q1",
      difficulty: "", user_answer: "", source: "deep_question", material_id: "", material_title: "",
      section_id: "", section_title: "", score_trend: "new", is_correct: false, resolved: false,
      bookmarked: false, followup_session_id: "", created_at: 100, updated_at: 100,
      question: "Which ocean?", options: { A: "Pacific", B: "Atlantic" }, correct_answer: "B",
      explanation: "Reference explanation", question_type: "single_choice",
    },
    state: { version: 2, review_count: 1, is_mistake: true, due_at: 100, learning_status: "open" },
  });
  vi.mocked(api.savePracticeReview).mockResolvedValue({ due_at: 200000, is_mistake: true, correct: true, rating: "good", learning_status: "verify", closed: false });
});

it("persists the attempt before showing feedback and distinguishes correct from mastery", async () => {
  let finish!: (value: api.PracticeReviewResult) => void;
  vi.mocked(api.savePracticeReview).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<PracticeSession ids={[8]} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("radio", { name: "B Atlantic" }));
  expect(screen.queryByText("Reference explanation")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "I have mastered this" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Submit and save answer" }));
  expect(screen.queryByText("Reference explanation")).not.toBeInTheDocument();
  await act(async () => finish({ due_at: 200000, is_mistake: true, correct: true, rating: "good", learning_status: "verify", closed: false }));
  expect(await screen.findByText("Correct attempt saved")).toBeInTheDocument();
  expect(screen.getByText("Reference explanation")).toBeInTheDocument();
  expect(screen.queryByText("This question reached verified mastery")).not.toBeInTheDocument();
  expect(api.savePracticeReview).toHaveBeenCalledWith(8, expect.objectContaining({ version: 2, answer: "B", rating: "good", auto_grade: true, answer_revealed: false }), undefined);
  fireEvent.click(screen.getByRole("button", { name: "Next question" }));
  expect(await screen.findByText("Practice complete")).toBeInTheDocument();
  expect(screen.getByText("0 questions reached verified mastery.")).toBeInTheDocument();
});

it("keeps saved original photos in their owning workspace", async () => {
  const question = await api.getPracticeQuestion(8);
  vi.mocked(api.getPracticeQuestion).mockResolvedValue({ ...question, content_workspace_id: "geometry-source", entry: { ...question.entry, question_images: [{ id: "diagram", url: "/files/attachments/photo/diagram/geometry.png", filename: "geometry.png", mime_type: "image/png" }] } });
  render(<PracticeSession questions={[{ id: 8, content_workspace_id: "geometry-source" }]} onClose={vi.fn()} />);
  const original = await screen.findByAltText("Saved original photo 1");
  expect(original).toHaveAttribute("src", "/files/attachments/photo/diagram/geometry.png?dt_workspace=geometry-source");
  expect(original.closest("a")).toHaveAttribute("href", "/files/attachments/photo/diagram/geometry.png?dt_workspace=geometry-source");
  expect(screen.queryByText("Reference explanation")).not.toBeInTheDocument();
});

it("waits for durable exposure recording before showing reference answers", async () => {
  let finish!: (value: { recorded: boolean }) => void;
  vi.mocked(api.recordPracticeExposure).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<PracticeSession questions={[{ id: 8, content_workspace_id: "source-a" }]} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Show reference answer" }));
  expect(screen.queryByText("Reference explanation")).not.toBeInTheDocument();
  await act(async () => finish({ recorded: true }));
  expect(await screen.findByText("Reference explanation")).toBeInTheDocument();
  expect(api.recordPracticeExposure).toHaveBeenCalledWith(8, expect.any(String), "answer", "source-a");
  fireEvent.click(screen.getByRole("radio", { name: "B Atlantic" }));
  fireEvent.click(screen.getByRole("button", { name: "Record corrected answer" }));
  await screen.findByRole("heading", { name: "Assisted or unverified review" });
  expect(api.savePracticeReview).toHaveBeenCalledWith(8, expect.objectContaining({ answer_revealed: true }), "source-a");
});

it("does not reveal answers when exposure recording fails", async () => {
  vi.mocked(api.recordPracticeExposure).mockRejectedValueOnce(new Error("Exposure unavailable"));
  render(<PracticeSession ids={[8]} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Show reference answer" }));
  await screen.findByText("Exposure unavailable");
  expect(screen.queryByText("Reference explanation")).not.toBeInTheDocument();
});

it("retries a lost submission using the same payload and request ID", async () => {
  vi.mocked(api.savePracticeReview).mockRejectedValueOnce(new Error("Network interrupted"));
  render(<PracticeSession ids={[8]} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("radio", { name: "B Atlantic" }));
  fireEvent.click(screen.getByRole("button", { name: "Submit and save answer" }));
  await screen.findByText("Network interrupted");
  expect(screen.queryByText("Reference explanation")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry submission" }));
  await screen.findByText("Correct attempt saved");
  expect(vi.mocked(api.savePracticeReview).mock.calls[0]).toEqual(vi.mocked(api.savePracticeReview).mock.calls[1]);
});

it("saves failure without an unbounded same-round retry loop", async () => {
  vi.mocked(api.savePracticeReview).mockResolvedValue({ due_at: 200, is_mistake: true, correct: false, rating: "again", learning_status: "open" });
  render(<PracticeSession ids={[8]} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("radio", { name: "A Pacific" }));
  fireEvent.click(screen.getByRole("button", { name: "Submit and save answer" }));
  expect(await screen.findByRole("heading", { name: "Needs another try" })).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Next question" }));
  expect(await screen.findByText("Practice complete")).toBeInTheDocument();
  expect(api.getPracticeQuestion).toHaveBeenCalledTimes(1);
});

it("reloads conflicts instead of retrying the stale version", async () => {
  vi.mocked(api.savePracticeReview).mockRejectedValueOnce(new api.PracticeRequestError("Question changed", 409));
  render(<PracticeSession ids={[8]} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("radio", { name: "B Atlantic" }));
  fireEvent.click(screen.getByRole("button", { name: "Submit and save answer" }));
  fireEvent.click(await screen.findByRole("button", { name: "Reload question" }));
  await waitFor(() => expect(api.getPracticeQuestion).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("Reference explanation")).not.toBeInTheDocument();
});

it("preserves workspace ownership for equal IDs in separate workspaces", async () => {
  render(<PracticeSession questions={[{ id: 8, content_workspace_id: "" }, { id: 8, content_workspace_id: "other", attempt_kind: "spotcheck" }]} onClose={vi.fn()} />);
  for (const workspace of ["", "other"]) {
    fireEvent.click(await screen.findByRole("radio", { name: "B Atlantic" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit and save answer" }));
    await screen.findByText("Correct attempt saved");
    fireEvent.click(screen.getByRole("button", { name: "Next question" }));
    if (workspace === "") await waitFor(() => expect(api.getPracticeQuestion).toHaveBeenLastCalledWith(8, "other"));
  }
  await screen.findByText("Practice complete");
  expect(vi.mocked(api.savePracticeReview).mock.calls.map(call => call[2])).toEqual(["", "other"]);
  expect(api.savePracticeReview).toHaveBeenLastCalledWith(8, expect.objectContaining({ attempt_kind: "spotcheck" }), "other");
});

it("lets the server grade numeric fill blanks without forcing self-report", async () => {
  const question = await api.getPracticeQuestion(8);
  vi.mocked(api.getPracticeQuestion).mockResolvedValue({ ...question, entry: { ...question.entry, options: {}, question_type: "fill_blank", question: "Half plus a quarter?", correct_answer: "3/4" } });
  render(<PracticeSession ids={[8]} onClose={vi.fn()} />);
  fireEvent.change(await screen.findByRole("textbox", { name: "Your answer" }), { target: { value: "3/4" } });
  fireEvent.click(screen.getByRole("button", { name: "Submit and save answer" }));
  await screen.findByText("Correct attempt saved");
  expect(api.savePracticeReview).toHaveBeenCalledWith(8, expect.objectContaining({ self_report: false, auto_grade: true, answer: "3/4" }), undefined);
});

it("counts verified mastery only when the server returns a closure decision", async () => {
  vi.mocked(api.savePracticeReview).mockResolvedValue({ due_at: 200000, is_mistake: true, correct: true, rating: "good", learning_status: "closed", closed: true, evidence: { spaced: true, transfer: true, delayed: true } });
  render(<PracticeSession ids={[8]} onClose={vi.fn()} />);
  fireEvent.click(await screen.findByRole("radio", { name: "B Atlantic" }));
  fireEvent.click(screen.getByRole("button", { name: "Submit and save answer" }));
  await screen.findByText("This question reached verified mastery");
  fireEvent.click(screen.getByRole("button", { name: "Next question" }));
  await screen.findByText("Practice complete");
  expect(screen.getByText("1 questions reached verified mastery.")).toBeInTheDocument();
});

it("previews import errors without committing any rows", async () => {
  vi.mocked(api.previewPracticeImport).mockResolvedValue({
    token: null,
    total: 2,
    valid: 1,
    errors: [{ row: 3, message: "Answer is required" }],
    samples: [],
  });
  render(<PracticeImport onClose={vi.fn()} onImported={vi.fn()} initialTarget="bank" />);
  fireEvent.change(screen.getByLabelText(/Choose a question file/), {
    target: { files: [new File(["q,a"], "questions.csv")] },
  });
  expect(await screen.findByText(/Answer is required/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Confirm import" })).toBeDisabled();
  expect(api.commitPracticeImport).not.toHaveBeenCalled();
});

it("keeps a successful preview available when import saving fails", async () => {
  vi.mocked(api.previewPracticeImport).mockResolvedValue({
    token: "a".repeat(32),
    total: 1,
    valid: 1,
    errors: [],
    samples: [],
  });
  vi.mocked(api.commitPracticeImport)
    .mockRejectedValueOnce(new Error("Save unavailable"))
    .mockResolvedValueOnce({ created: 1, duplicates: 0 });
  const onImported = vi.fn();
  render(<PracticeImport onClose={vi.fn()} onImported={onImported} initialTarget="mistakes" />);
  fireEvent.change(screen.getByLabelText(/Choose a question file/), {
    target: { files: [new File(["q,a"], "questions.csv")] },
  });
  fireEvent.click(await screen.findByRole("button", { name: "Confirm import" }));
  await screen.findByText("Save unavailable");
  expect(onImported).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm import" }));
  await waitFor(() => expect(onImported).toHaveBeenCalledOnce());
  expect(api.previewPracticeImport).toHaveBeenCalledWith(expect.any(File), "mistakes");
});

it("reviews recognized image questions before staging and committing", async () => {
  vi.mocked(api.startPracticeRecognition).mockResolvedValue({
    job_id: "rec_1",
    status: "queued",
    created_at: 100,
  });
  vi.mocked(api.getPracticeRecognition).mockResolvedValue({
    job_id: "rec_1",
    status: "ready",
    stage: "recognizing",
    progress_message: "Recognized 1 question",
    completed_units: 1,
    total_units: 1,
    version: 3,
    drafts: [{
      draft_id: "q_001",
      ordinal: 1,
      question: "2 + 2 = ?",
      question_type: "single_choice",
      options: { A: "3", B: "4" },
      correct_answer: "B",
      explanation: "",
      difficulty: "easy",
      tags: ["Math"],
      question_images: [{
        id: "source",
        url: "/files/attachments/rec_1/source/question.png",
        filename: "question.png",
        mime_type: "image/png",
      }],
      source_locator: { page_numbers: [1] },
      answer_origin: "document",
      review_level: "normal",
      warnings: [],
    }],
    summary: { total: 1, normal: 1, review: 0, required: 0 },
    error_code: "",
    error_message: "",
    expires_at: 200,
  });
  vi.mocked(api.stagePracticeRecognition).mockResolvedValue({
    token: "b".repeat(32),
    total: 1,
    valid: 1,
    errors: [],
    samples: [{
      question: "2 + 2 = ?",
      question_type: "single_choice",
      correct_answer: "B",
      tags: ["Math"],
    }],
  });
  vi.mocked(api.commitPracticeImport).mockResolvedValue({ created: 1, duplicates: 0 });
  const onImported = vi.fn();
  render(<PracticeImport onClose={vi.fn()} onImported={onImported} initialTarget="bank" />);

  fireEvent.click(screen.getByRole("tab", { name: "Photo / PDF recognition" }));
  const recognitionInput = await screen.findByLabelText(/Choose a photo, screenshot, or PDF/);
  expect(recognitionInput).toHaveAttribute("accept", expect.stringContaining("application/pdf"));
  fireEvent.change(recognitionInput, {
    target: { files: [new File(["image"], "question.png", { type: "image/png" })] },
  });
  expect(await screen.findByDisplayValue("2 + 2 = ?")).toBeInTheDocument();
  expect(screen.getByRole("img", { name: "Source image for question 1" })).toHaveAttribute(
    "src",
    "/files/attachments/rec_1/source/question.png",
  );
  expect(screen.getByText("Correct answer (optional)")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Continue to import preview" }));
  expect(await screen.findByRole("button", { name: "Confirm import" })).toBeEnabled();
  fireEvent.click(screen.getByRole("button", { name: "Confirm import" }));

  await waitFor(() => expect(onImported).toHaveBeenCalledOnce());
  expect(api.stagePracticeRecognition).toHaveBeenCalledWith(
    "rec_1",
    3,
    [expect.objectContaining({ draft_id: "q_001", selected: true })],
  );
});

it("recovers and displays the current recognition task after reopening import", async () => {
  const current: api.RecognitionSnapshot = {
    job_id: "rec_pdf",
    filename: "worksheet.pdf",
    target: "bank",
    status: "recognizing",
    stage: "recognizing",
    progress_message: "Recognizing PDF page 8 of 12",
    completed_units: 7,
    total_units: 12,
    version: 8,
    drafts: [],
    summary: { total: 0, normal: 0, review: 0, required: 0 },
    error_code: "",
    error_message: "",
    expires_at: 200,
  };
  vi.mocked(api.getCurrentPracticeRecognition).mockResolvedValue(current);
  vi.mocked(api.getPracticeRecognition).mockResolvedValue(current);

  render(<PracticeImport onClose={vi.fn()} onImported={vi.fn()} initialTarget="mistakes" />);
  fireEvent.click(screen.getByRole("tab", { name: "Photo / PDF recognition" }));

  expect(await screen.findByText("worksheet.pdf")).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Import into" })).toHaveValue("bank");
  expect(screen.getByText("Recognizing PDF page 8 of 12")).toBeInTheDocument();
  expect(screen.getByText("7 of 12 pages processed")).toBeInTheDocument();
  expect(screen.queryByLabelText(/Choose a photo, screenshot, or PDF/)).not.toBeInTheDocument();
  expect(api.startPracticeRecognition).not.toHaveBeenCalled();
});
