import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { PhotoInbox } from "@/components/learning/practice/PhotoInbox";
import { PhotoInboxDetail } from "@/components/learning/practice/PhotoInboxDetail";
import { WeixinPhotoCollectionSettings } from "@/components/partners/WeixinPhotoCollectionSettings";
import { ApiError } from "@/shared/api/errors";
import { initI18n } from "@/i18n/init";
import { ensureLanguage } from "@/i18n/init";
import * as api from "@/lib/photo-inbox-api";
import { getPartnerWorkspaces } from "@/lib/partners-api";
import { recordPracticeExposure } from "@/lib/practice-api";

vi.mock("@/lib/photo-inbox-api", async original => ({
  ...(await original<typeof api>()),
  listPhotoInbox: vi.fn(), getPhotoInboxItem: vi.fn(), updatePhotoInboxItem: vi.fn(),
  retryPhotoInboxItem: vi.fn(), supplementPhotoInboxItem: vi.fn(), confirmPhotoInboxItem: vi.fn(),
  removePhotoInboxItem: vi.fn(), restorePhotoInboxItem: vi.fn(),
}));
vi.mock("@/lib/practice-api", async original => ({ ...(await original<typeof import("@/lib/practice-api")>()), recordPracticeExposure: vi.fn() }));
vi.mock("@/lib/partners-api", async original => ({
  ...(await original<object>()), getPartnerWorkspaces: vi.fn(),
}));
const i18n = initI18n("en");
const asset = { id: "original", url: "/api/attachments/receipt/original", filename: "question.jpg", mime_type: "image/jpeg" };
const draft: api.PhotoDraft = {
  draft_id: "draft-1", ordinal: 1, question: "Find x when 2x = 8", question_type: "short_answer",
  options: {}, correct_answer: "", answer_origin: "missing", explanation: "", difficulty: "",
  tags: [], question_images: [asset], source_locator: {}, review_level: "normal", warnings: [], subject: "math",
};
const photo: api.PhotoInboxItem = {
  id: "photo-1", status: "ready", subject: "math", received_at: 1793232000,
  source_sent_at: null, last_received_at: 1793232000, upload_count: 2, asset, assets: [asset],
  drafts: [draft], version: 4, error: "", entry_ids: [], note: "",
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(recordPracticeExposure).mockResolvedValue({ recorded: true });
  window.history.replaceState({}, "", "/space/questions?view=photos");
  vi.mocked(api.listPhotoInbox).mockResolvedValue({ items: [photo], total: 1, subjects: [{ id: "math", label: "数学" }], counts: { photos: 1, pending: 0, confirmed: 0 } });
  vi.mocked(api.getPhotoInboxItem).mockResolvedValue(photo);
  vi.mocked(api.updatePhotoInboxItem).mockResolvedValue({ ...photo, version: 5 });
  vi.mocked(api.confirmPhotoInboxItem).mockResolvedValue({ ...photo, version: 6, status: "confirmed", entry_ids: [42] });
  vi.mocked(api.removePhotoInboxItem).mockResolvedValue({ ...photo, archived_at: 1793233000, version: 5 });
  vi.mocked(api.restorePhotoInboxItem).mockResolvedValue({ ...photo, archived_at: null, version: 6 });
  vi.mocked(getPartnerWorkspaces).mockResolvedValue([]);
});

it("hides linked originals and answer keys when exposure recording fails, then retries in their source workspace", async () => {
  window.history.replaceState({}, "", "/space/questions?view=photos&dt_workspace=other-space");
  vi.mocked(recordPracticeExposure).mockResolvedValueOnce({ recorded: true }).mockRejectedValueOnce(new Error("Could not save original exposure"));
  render(<PhotoInboxDetail initial={{ ...photo, status: "confirmed", content_workspace_id: "photo-source", entry_ids: [42, 43], drafts: [{ ...draft, correct_answer: "4", explanation: "Worked answer" }] }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  expect(screen.queryByAltText("Saved original photo 1")).not.toBeInTheDocument();
  expect(screen.getByLabelText("Correct answer (optional)")).toHaveValue("");
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not save original exposure");
  expect(screen.queryByAltText("Saved original photo 1")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(await screen.findByAltText("Saved original photo 1")).toBeVisible();
  expect(screen.getByAltText("Saved original photo 1")).toHaveAttribute("src", asset.url + "?dt_workspace=photo-source");
  expect(screen.getByLabelText("Correct answer (optional)")).toHaveValue("4");
  expect(vi.mocked(recordPracticeExposure).mock.calls.map(call => [call[0], call[2], call[3]])).toEqual([[42, "answer", "photo-source"], [43, "answer", "photo-source"], [42, "answer", "photo-source"], [43, "answer", "photo-source"]]);
  expect(vi.mocked(recordPracticeExposure).mock.calls[0][1]).toBe(vi.mocked(recordPracticeExposure).mock.calls[2][1]);
  expect(vi.mocked(recordPracticeExposure).mock.calls[1][1]).toBe(vi.mocked(recordPracticeExposure).mock.calls[3][1]);
});

it("loads all workspaces in the account overview so photos saved in a named workspace are visible", async () => {
  const saved = { ...photo, content_workspace_id: "mida-learning", content_workspace_name: "米达的学习" };
  vi.mocked(api.listPhotoInbox).mockResolvedValue({ items: [saved], total: 1, subjects: [], counts: { photos: 1, pending: 0, confirmed: 0 } });
  render(<PhotoInbox onConfirmed={vi.fn()} />);
  expect(await screen.findByText("Find x when 2x = 8")).toBeVisible();
  expect(api.listPhotoInbox).toHaveBeenCalledWith(expect.objectContaining({ all_workspaces: true }));
  expect(screen.getByText(/米达的学习/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: /Find x when/ }));
  await screen.findByRole("heading", { name: "Check your saved photo" });
  expect(api.getPhotoInboxItem).toHaveBeenCalledWith("photo-1", "mida-learning");
});

it("keeps an explicitly selected workspace scoped and reports unavailable spaces", async () => {
  window.history.replaceState({}, "", "/space/questions?view=photos&dt_workspace=one-space");
  vi.mocked(api.listPhotoInbox).mockResolvedValue({ items: [photo], total: 1, subjects: [], counts: { photos: 1, pending: 0, confirmed: 0 }, unavailable_workspaces: ["offline-space"] });
  render(<PhotoInbox onConfirmed={vi.fn()} />);
  await screen.findByText("Find x when 2x = 8");
  expect(vi.mocked(api.listPhotoInbox).mock.calls[0][0]).not.toHaveProperty("all_workspaces", true);
  expect(screen.getByText("Some workspaces could not be loaded. Available content is shown.")).toBeVisible();
});

it("does not treat an explicit default workspace as an account overview", async () => {
  window.history.replaceState({}, "", "/space/questions?view=photos&dt_workspace=");
  render(<PhotoInbox onConfirmed={vi.fn()} />);
  await screen.findByText("Find x when 2x = 8");
  expect(vi.mocked(api.listPhotoInbox).mock.calls[0][0]).not.toHaveProperty("all_workspaces", true);
  expect(screen.queryByText("Showing photos from all your learning workspaces.")).not.toBeInTheDocument();
});

it("retains the saved workspace after detail responses omit provenance", async () => {
  const saved = { ...photo, content_workspace_id: "mida-learning", content_workspace_name: "米达的学习" };
  render(<PhotoInboxDetail initial={saved} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("My note / reason for the mistake"), { target: { value: "Check sign" } });
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await screen.findByText("Photo changes saved.");
  expect(api.updatePhotoInboxItem).toHaveBeenCalledWith("photo-1", expect.anything(), "mida-learning");
  fireEvent.click(screen.getByRole("checkbox", { name: /I checked every selected question/ }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm and add to review" }));
  await screen.findByText("Questions added to review.");
  expect(api.confirmPhotoInboxItem).toHaveBeenCalledWith("photo-1", 5, expect.anything(), "mida-learning");
  expect(screen.getByRole("link", { name: "Review this question" })).toHaveAttribute("href", "/learning/practice?question=42&dt_workspace=mida-learning");
  expect(screen.getByText(/米达的学习/)).toBeVisible();
});

it("retains an explicit default origin through polling while the page has another workspace", async () => {
  window.history.replaceState({}, "", "/space/questions?view=photos&dt_workspace=other-space");
  vi.useFakeTimers();
  try {
    render(<PhotoInboxDetail initial={{ ...photo, content_workspace_id: "", status: "queued", drafts: [] }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(api.getPhotoInboxItem).toHaveBeenCalledWith("photo-1", "");
    expect(screen.getByAltText("Saved original photo 1")).toHaveAttribute("src", asset.url + "?dt_workspace=");
  } finally { vi.useRealTimers(); }
  fireEvent.click(screen.getByRole("checkbox", { name: /I checked every selected question/ }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm and add to review" }));
  await screen.findByText("Questions added to review.");
  expect(api.confirmPhotoInboxItem).toHaveBeenCalledWith("photo-1", 4, expect.anything(), "");
  expect(screen.getByRole("link", { name: "Review this question" })).toHaveAttribute("href", "/learning/practice?question=42&dt_workspace=");
});

it("opens same-id photos from different workspaces at their own origins", async () => {
  const rows = [
    { ...photo, content_workspace_id: "", content_workspace_name: "Default learning" },
    { ...photo, content_workspace_id: "mida-learning", content_workspace_name: "米达的学习" },
  ];
  vi.mocked(api.listPhotoInbox).mockResolvedValue({ items: rows, total: 2, subjects: [], counts: { photos: 2, pending: 0, confirmed: 0 } });
  render(<PhotoInbox onConfirmed={vi.fn()} />);
  const named = await screen.findByText("Saved in 米达的学习");
  fireEvent.click(named.closest("button")!);
  await screen.findByRole("heading", { name: "Check your saved photo" });
  expect(api.getPhotoInboxItem).toHaveBeenLastCalledWith("photo-1", "mida-learning");
  fireEvent.click(screen.getByRole("button", { name: "Back to saved photos" }));
  const general = await screen.findByText("Saved in Default learning");
  fireEvent.click(general.closest("button")!);
  await screen.findByRole("heading", { name: "Check your saved photo" });
  expect(api.getPhotoInboxItem).toHaveBeenLastCalledWith("photo-1", "");
});

it("shows one preserved original for repeated uploads and sends subject, state and date filters", async () => {
  render(<PhotoInbox onConfirmed={vi.fn()} />);
  expect(await screen.findByText("Find x when 2x = 8")).toBeVisible();
  expect(screen.getByText("2 uploads · same original")).toBeVisible();
  expect(screen.getAllByAltText("Saved original photo")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Mathematics" }));
  fireEvent.change(screen.getByRole("combobox", { name: "Photo status" }), { target: { value: "pending" } });
  fireEvent.change(screen.getByLabelText("Received from"), { target: { value: "2026-10-28" } });
  fireEvent.change(screen.getByLabelText("Received through"), { target: { value: "2026-10-29" } });
  await waitFor(() => expect(api.listPhotoInbox).toHaveBeenLastCalledWith(expect.objectContaining({
    subject: "math", status: "pending", since: new Date("2026-10-28T00:00:00").getTime() / 1000,
    until: new Date("2026-10-30T00:00:00").getTime() / 1000,
  })));
});

it("uses school-subject translations and identifies known mixed-subject photos", async () => {
  await ensureLanguage("zh");
  await i18n.changeLanguage("zh");
  try {
    expect(i18n.t(api.PHOTO_SUBJECTS.chinese)).toBe("语文");
    expect(i18n.t(api.PHOTO_SUBJECTS.history)).toBe("历史");
    expect(i18n.t(api.photoSubjectLabel({ subject: "unclassified", drafts: [draft, { ...draft, subject: "physics" }] }))).toBe("多学科");
  } finally { await i18n.changeLanguage("en"); }
  const mixed = { ...photo, subject: "unclassified" as const, drafts: [draft, { ...draft, draft_id: "draft-2", subject: "physics" as const }] };
  vi.mocked(api.listPhotoInbox).mockResolvedValue({ items: [mixed], total: 1, subjects: [], counts: { photos: 1, pending: 0, confirmed: 0 } });
  render(<PhotoInbox onConfirmed={vi.fn()} />);
  expect(await screen.findByText("Mixed subjects")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "Physics" }));
  await waitFor(() => expect(api.listPhotoInbox).toHaveBeenLastCalledWith(expect.objectContaining({ subject: "physics" })));
});

it("requires a subject and explicit review, invalidates it on edits, and saves notes before committing", async () => {
  const onConfirmed = vi.fn();
  render(<PhotoInboxDetail initial={{ ...photo, status: "needs_subject", subject: "unclassified", drafts: [{ ...draft, subject: "unclassified" }] }} onBack={vi.fn()} onConfirmed={onConfirmed} />);
  const review = screen.getByRole("checkbox", { name: /I checked every selected question/ });
  const confirm = screen.getByRole("button", { name: "Confirm and add to review" });
  expect(review).toBeDisabled();
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByRole("combobox", { name: "Question subject" }), { target: { value: "math" } });
  fireEvent.click(review);
  expect(confirm).toBeEnabled();
  fireEvent.change(screen.getByLabelText("My note / reason for the mistake"), { target: { value: "Missed a step" } });
  expect(review).not.toBeChecked();
  expect(confirm).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Grade (optional)"), { target: { value: "五年级" } });
  fireEvent.change(screen.getByLabelText("Knowledge point"), { target: { value: "有理数" } });
  fireEvent.change(screen.getByLabelText("Mistake tags (comma separated)"), { target: { value: "漏看条件, 移项变号" } });
  fireEvent.click(review);
  fireEvent.click(confirm);
  await screen.findByText("Questions added to review.");
  expect(api.updatePhotoInboxItem).toHaveBeenCalledWith("photo-1", expect.objectContaining({ expected_version: 4, note: "Missed a step", drafts: [expect.objectContaining({ grade: "五年级", knowledge_point: "有理数", tags: ["漏看条件", "移项变号"] })] }), "");
  expect(api.confirmPhotoInboxItem).toHaveBeenCalledWith("photo-1", 5, [expect.objectContaining({ subject: "math", correct_answer: "", answer_origin: "missing" })], "");
  expect(onConfirmed).toHaveBeenCalledOnce();
  expect(screen.getByRole("link", { name: "Review this question" })).toHaveAttribute("href", "/learning/practice?question=42&dt_workspace=");
});

it("requires a suggested answer to be checked separately before confirming", () => {
  render(<PhotoInboxDetail initial={{ ...photo, drafts: [{ ...draft, correct_answer: "4", answer_origin: "model_suggested" }] }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  const review = screen.getByRole("checkbox", { name: /I checked every selected question/ });
  expect(review).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "This answer is an AI suggestion. I checked it and want to use it." }));
  expect(review).toBeEnabled();
  fireEvent.click(review);
  expect(screen.getByRole("button", { name: "Confirm and add to review" })).toBeEnabled();
});

it("can exclude an incorrect split and keeps mixed subjects when saving only a note", async () => {
  const second: api.PhotoDraft = { ...draft, draft_id: "draft-2", question: "", subject: "unclassified" };
  render(<PhotoInboxDetail initial={{ ...photo, subject: "unclassified", drafts: [draft, second] }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  const review = screen.getByRole("checkbox", { name: /I checked every selected question/ });
  expect(review).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "Include question 2 in review" }));
  fireEvent.change(screen.getByLabelText("My note / reason for the mistake"), { target: { value: "Only use the first question" } });
  fireEvent.click(review);
  fireEvent.click(screen.getByRole("button", { name: "Confirm and add to review" }));
  await screen.findByText("Questions added to review.");
  const patch = vi.mocked(api.updatePhotoInboxItem).mock.calls[0][1];
  expect(patch).not.toHaveProperty("subject");
  expect(patch.drafts).toEqual([expect.objectContaining({ subject: "math" }), expect.objectContaining({ subject: "unclassified", selected: false })]);
  expect(api.confirmPhotoInboxItem).toHaveBeenCalledWith("photo-1", 5, expect.arrayContaining([expect.objectContaining({ draft_id: "draft-2", selected: false })]), "");
});

it("updates every draft when the user deliberately changes the archive subject", async () => {
  render(<PhotoInboxDetail initial={{ ...photo, subject: "unclassified", drafts: [draft, { ...draft, draft_id: "draft-2", subject: "physics" }] }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  fireEvent.change(screen.getByRole("combobox", { name: /Archive subject/ }), { target: { value: "chemistry" } });
  expect(screen.getAllByRole("combobox", { name: "Question subject" }).every(select => (select as HTMLSelectElement).value === "chemistry")).toBe(true);
  fireEvent.click(screen.getByRole("checkbox", { name: /I checked every selected question/ }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm and add to review" }));
  await screen.findByText("Questions added to review.");
  expect(api.updatePhotoInboxItem).toHaveBeenCalledWith("photo-1", expect.objectContaining({ drafts: [expect.objectContaining({ subject: "chemistry" }), expect.objectContaining({ subject: "chemistry" })] }), "");
  expect(api.confirmPhotoInboxItem).toHaveBeenCalledWith("photo-1", 5, [expect.objectContaining({ subject: "chemistry" }), expect.objectContaining({ subject: "chemistry" })], "");
  expect(screen.queryByRole("button", { name: "Retry recognition" })).not.toBeInTheDocument();
});

it("keeps a later per-question subject change after changing the whole photo subject", async () => {
  render(<PhotoInboxDetail initial={{ ...photo, drafts: [draft, { ...draft, draft_id: "draft-2" }] }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  fireEvent.change(screen.getByRole("combobox", { name: /Archive subject/ }), { target: { value: "chemistry" } });
  fireEvent.change(screen.getAllByRole("combobox", { name: "Question subject" })[1], { target: { value: "physics" } });
  expect(screen.getByRole("combobox", { name: /Archive subject/ })).toHaveValue("unclassified");
  fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
  await screen.findByText("Photo changes saved.");
  const patch = vi.mocked(api.updatePhotoInboxItem).mock.calls[0][1];
  expect(patch).not.toHaveProperty("subject");
  expect(patch.drafts).toEqual([expect.objectContaining({ subject: "chemistry" }), expect.objectContaining({ subject: "physics" })]);
});

it("keeps the original visible on failure and shows success only after a supplemental photo is saved", async () => {
  let finish!: (item: api.PhotoInboxItem) => void;
  vi.mocked(api.supplementPhotoInboxItem).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  render(<PhotoInboxDetail initial={{ ...photo, status: "needs_retake", drafts: [], error: "Photo is blurry" }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  expect(screen.getByAltText("Saved original photo 1")).toBeVisible();
  expect(screen.getByText("Photo is blurry")).toBeVisible();
  const file = new File(["test-image"], "clear.jpg", { type: "image/jpeg" });
  fireEvent.change(screen.getByLabelText("Add clearer photo"), { target: { files: [file] } });
  expect(screen.queryByText("New photo saved. Both originals are preserved; recognition is queued.")).not.toBeInTheDocument();
  finish({ ...photo, status: "queued", version: 5, assets: [asset, { ...asset, id: "supplement", filename: "clear.jpg" }] });
  await screen.findByText("New photo saved. Both originals are preserved; recognition is queued.");
  expect(screen.getByAltText("Saved original photo 1")).toBeVisible();
  expect(screen.getByAltText("Saved original photo 2")).toBeVisible();
  expect(screen.getByText(/First received:/)).toHaveTextContent(new Date(photo.received_at * 1000).toLocaleString("en"));
});

it("recovers polling after a temporary failure and clears the old error", async () => {
  vi.useFakeTimers();
  try {
    vi.mocked(api.getPhotoInboxItem).mockRejectedValueOnce(new ApiError({ code: "temporarily_unavailable", message: "Try again soon", scope: "network", retryable: true, status: 503 })).mockResolvedValueOnce(photo);
    render(<PhotoInboxDetail initial={{ ...photo, status: "queued", drafts: [] }} onBack={vi.fn()} onConfirmed={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole("alert")).toHaveTextContent("Try again soon");
    expect(api.getPhotoInboxItem).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(api.getPhotoInboxItem).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Question")).toHaveValue("Find x when 2x = 8");
    expect(screen.getByText("Archived")).toBeVisible();
  } finally { vi.useRealTimers(); }
});

it("requires a reload after a version conflict and never commits stale edits", async () => {
  vi.mocked(api.updatePhotoInboxItem).mockRejectedValueOnce(new ApiError({ code: "conflict", message: "Photo changed", scope: "network", retryable: false, status: 409 }));
  render(<PhotoInboxDetail initial={photo} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("Question"), { target: { value: "Edited question" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /I checked every selected question/ }));
  fireEvent.click(screen.getByRole("button", { name: "Confirm and add to review" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Photo changed");
  expect(api.confirmPhotoInboxItem).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Confirm and add to review" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reload saved photo" }));
  await screen.findByText("Latest photo loaded. Check it again before confirming.");
  expect(screen.getByRole("checkbox", { name: /I checked every selected question/ })).not.toBeChecked();
});

it("removes and restores a photo without deleting its originals", async () => {
  render(<PhotoInboxDetail initial={photo} onBack={vi.fn()} onConfirmed={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Retry recognition" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Remove from inbox" }));
  await screen.findByText("This photo is removed from the inbox. Its originals and any questions already added to review are preserved.");
  expect(screen.getByAltText("Saved original photo 1")).toBeVisible();
  expect(screen.getByRole("button", { name: "Confirm and add to review" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Restore to inbox" }));
  await screen.findByText("Photo restored to the inbox.");
  expect(screen.getByRole("button", { name: "Remove from inbox" })).toBeEnabled();
  expect(api.restorePhotoInboxItem).toHaveBeenCalledWith("photo-1", 5, "");
});

it("configures photo collection without dropping existing channel values", () => {
  const onChange = vi.fn();
  render(<WeixinPhotoCollectionSettings value={{ enabled: true, bot_token: "retained", photo_collection_enabled: false }} onChange={onChange} />);
  fireEvent.click(screen.getByRole("checkbox", { name: "Automatically save question photos from linked private chats" }));
  expect(onChange).toHaveBeenCalledWith({ enabled: true, bot_token: "retained", photo_collection_enabled: true });
  expect(screen.queryByLabelText("Target workspace ID (optional)")).not.toBeInTheDocument();
});

it("chooses a named workspace while clearly keeping the sender's default as the default option", async () => {
  const row = { workspace_id: "math-space", display_name: "My mathematics", kind: "workspace" as const, status: "ready" as const, archived: false, follows_root: false, path: "", created_at: "", error: "" };
  vi.mocked(getPartnerWorkspaces).mockResolvedValue([row, { ...row, workspace_id: "system", kind: "system" }, { ...row, workspace_id: "old", display_name: "Old space", archived: true }]);
  const onChange = vi.fn();
  render(<WeixinPhotoCollectionSettings partnerId="my-partner" value={{ photo_collection_enabled: true, photo_workspace_id: "", bot_token: "retained" }} onChange={onChange} />);
  expect(await screen.findByRole("option", { name: "My mathematics" })).toBeVisible();
  expect(getPartnerWorkspaces).toHaveBeenCalledWith("my-partner");
  const select = screen.getByRole("combobox", { name: "Save photos to" });
  expect(select).toHaveValue("");
  expect(screen.getByRole("option", { name: "Each sender's default learning workspace" })).toBeVisible();
  expect(screen.queryByRole("option", { name: "Old space" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "System workspace" })).not.toBeInTheDocument();
  fireEvent.change(select, { target: { value: "math-space" } });
  expect(onChange).toHaveBeenCalledWith({ photo_collection_enabled: true, photo_workspace_id: "math-space", bot_token: "retained" });
  expect(screen.getByText("A chosen workspace must belong to the linked sender. Keep each sender's default when collecting from other people.")).toBeVisible();
});

it("keeps an unavailable configured workspace without exposing its raw ID", async () => {
  render(<WeixinPhotoCollectionSettings partnerId="my-partner" value={{ photo_collection_enabled: true, photo_workspace_id: "unavailable-raw-id" }} onChange={vi.fn()} />);
  const missing = await screen.findByRole("option", { name: "Configured workspace unavailable" });
  expect(missing).toBeDisabled();
  expect(screen.getByRole("combobox", { name: "Save photos to" })).toHaveValue("unavailable-raw-id");
  expect(screen.queryByText("unavailable-raw-id")).not.toBeInTheDocument();
});
