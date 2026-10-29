"use client";
/* eslint-disable @next/next/no-img-element -- Originals use authenticated workspace URLs. */

import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, Loader2, RefreshCw, Save, Upload } from "lucide-react";
import { useTranslation } from "react-i18next";
import { ApiError } from "@/shared/api/errors";
import { practiceRoute } from "@/lib/learning-routes";
import { activeWorkspaceId, scopedUrl } from "@/lib/workspace-scope";
import { recordPracticeExposure } from "@/lib/practice-api";
import { randomUuid } from "@/lib/random-uuid";
import {
  confirmPhotoInboxItem, getPhotoInboxItem, PHOTO_IN_FLIGHT, PHOTO_STATUS_LABELS,
  PHOTO_SUBJECTS, retryPhotoInboxItem, supplementPhotoInboxItem, updatePhotoInboxItem,
  removePhotoInboxItem, restorePhotoInboxItem,
  photoSubjectLabel, type PhotoDraft, type PhotoInboxItem, type PhotoSubject,
} from "@/lib/photo-inbox-api";

const QUESTION_TYPES = {
  single_choice: "Single choice", multi_choice: "Multiple choice", true_false: "True or false",
  fill_blank: "Fill in the blank", short_answer: "Short answer",
} as const;
const inputClass = "mt-1 block w-full rounded-lg border border-border bg-background p-2.5 text-sm text-foreground disabled:opacity-60";

export function PhotoInboxDetail({ initial, onBack, onConfirmed }: {
  initial: PhotoInboxItem; onBack: () => void; onConfirmed: () => void;
}) {
  const { t, i18n } = useTranslation();
  const origin = useRef({ workspaceId: initial.content_workspace_id ?? activeWorkspaceId(), name: initial.content_workspace_name });
  const workspaceId = origin.current.workspaceId;
  const [item, setItem] = useState(initial);
  const [drafts, setDrafts] = useState(initial.drafts);
  const [subject, setSubject] = useState(initial.subject);
  const [subjectChanged, setSubjectChanged] = useState(false);
  const [note, setNote] = useState(initial.note || "");
  const [reviewed, setReviewed] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const [notice, setNotice] = useState("");
  const [sourceReady, setSourceReady] = useState(!initial.entry_ids?.length);
  const [sourceRevision, setSourceRevision] = useState(0);
  const exposureIds = useRef(new Map<number, string>());
  const pending = useRef(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const alive = useRef(true);
  const removed = !!item.archived_at;
  const locked = removed || !sourceReady || PHOTO_IN_FLIGHT.has(item.status) || item.status === "confirmed";
  const canRetry = !removed && ["failed", "needs_retake", "non_question"].includes(item.status);
  useEffect(() => {
    alive.current = true;
    heading.current?.focus();
    return () => { alive.current = false; };
  }, []);

  const adopt = useCallback((next: PhotoInboxItem) => {
    setItem({ ...next, content_workspace_id: workspaceId, content_workspace_name: origin.current.name }); setDrafts(next.drafts); setSubject(next.subject); setNote(next.note || "");
    setDirty(false); setReviewed(false); setStale(false); setSubjectChanged(false);
  }, [workspaceId]);
  useEffect(() => {
    if (!PHOTO_IN_FLIGHT.has(item.status)) return;
    let cancelled = false;
    let failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await getPhotoInboxItem(item.id, workspaceId);
        if (cancelled) return;
        failures = 0;
        setError("");
        adopt(next);
        if (PHOTO_IN_FLIGHT.has(next.status)) timer = setTimeout(() => void poll(), 3000);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
        if (!(err instanceof ApiError) || err.retryable) {
          failures++;
          timer = setTimeout(() => void poll(), Math.min(30000, 2000 * 2 ** Math.min(failures, 4)));
        }
      }
    };
    timer = setTimeout(() => void poll(), 2000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [item.id, item.status, workspaceId, adopt]);

  function editDraft(index: number, changes: Partial<PhotoDraft>) {
    setDrafts(current => current.map((draft, i) => i === index ? { ...draft, ...changes } : draft));
    setDirty(true); setReviewed(false); setNotice("");
  }
  async function run(action: () => Promise<PhotoInboxItem>, message: string) {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const next = await action();
      if (!alive.current) return;
      adopt(next); setNotice(message);
      if (next.status === "confirmed") onConfirmed();
    } catch (err) {
      if (!alive.current) return;
      setError(err instanceof Error ? err.message : String(err));
      if (err instanceof ApiError && err.status === 409) setStale(true);
    } finally {
      pending.current = false;
      if (alive.current) setBusy(false);
    }
  }
  async function supplement(file?: File) {
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || !file.size || file.size > 20 * 1024 * 1024) {
      setError(t("Choose a JPG, PNG or WebP photo up to 20 MB."));
      return;
    }
    await run(() => supplementPhotoInboxItem(item.id, item.version, file, workspaceId), t("New photo saved. Both originals are preserved; recognition is queued."));
  }
  const selectedDrafts = drafts.filter(draft => draft.selected !== false);
  const linkedIds = (item.entry_ids || []).join(",");
  useEffect(() => {
    let active = true;
    if (!linkedIds) { setSourceReady(true); return; }
    setSourceReady(false);
    void Promise.all(linkedIds.split(",").map(Number).map(entryId => {
      if (!exposureIds.current.has(entryId)) exposureIds.current.set(entryId, randomUuid());
      return recordPracticeExposure(entryId, exposureIds.current.get(entryId)!, "answer", workspaceId);
    })).then(() => { if (active) setSourceReady(true); }).catch(err => {
      if (active) setError(err instanceof Error ? err.message : String(err));
    });
    return () => { active = false; };
  }, [linkedIds, workspaceId, sourceRevision]);
  const canConfirm = !locked && selectedDrafts.length > 0 && selectedDrafts.every(draft => draft.question.trim() && draft.subject !== "unclassified" && !(draft.correct_answer && draft.answer_origin === "model_suggested"));
  const archiveSubject = drafts.length ? new Set(drafts.map(draft => draft.subject)).size === 1 ? drafts[0].subject : "unclassified" : subject;
  const archiveSubjectLabel = photoSubjectLabel({ subject: archiveSubject, drafts });
  // Draft subjects are authoritative when present; a top-level subject would
  // overwrite later per-question changes on a mixed-subject photo.
  const changes = { expected_version: item.version, drafts, note, ...(subjectChanged && !drafts.length ? { subject } : {}) };
  const assets = item.assets?.length ? item.assets : [item.asset];
  return <section aria-labelledby="photo-detail-heading" className="space-y-5">
    <button type="button" onClick={onBack} disabled={busy} className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground disabled:opacity-40"><ArrowLeft size={16} />{t("Back to saved photos")}</button>
    {item.content_workspace_id !== undefined && <p className="text-xs text-muted-foreground">{t("Saved in {{workspace}}", { workspace: item.content_workspace_name || t(item.content_workspace_id ? "Workspace" : "Default workspace") })}</p>}
    <div className="flex flex-wrap items-start justify-between gap-3"><div><h2 id="photo-detail-heading" ref={heading} tabIndex={-1} className="text-lg font-semibold outline-none">{t("Check your saved photo")}</h2><p className="mt-1 text-sm text-muted-foreground">{t("First received: {{date}}", { date: new Date(item.received_at * 1000).toLocaleString(i18n.language) })}</p>{item.upload_count > 1 && <p className="mt-1 text-xs text-muted-foreground">{t("{{count}} uploads · last received {{date}}", { count: item.upload_count, date: new Date(item.last_received_at * 1000).toLocaleString(i18n.language) })}</p>}</div><span className="rounded-full bg-muted px-3 py-1.5 text-xs font-medium">{t(PHOTO_STATUS_LABELS[item.status])}</span></div>
    {item.source?.channel === "weixin" && <p className="rounded-lg bg-muted/40 px-3 py-2 text-xs text-muted-foreground">{t("WeChat question photo")}{item.source.partner_name ? ` · ${item.source.partner_name}` : ""} · {t("The original source and first received time stay with the review question.")}</p>}
    {error && <div role="alert" className="rounded-xl border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive"><p>{error}</p>{stale && <button type="button" disabled={busy} onClick={() => void run(() => getPhotoInboxItem(item.id, workspaceId), t("Latest photo loaded. Check it again before confirming."))} className="mt-2 underline">{t("Reload saved photo")}</button>}</div>}
    {notice && <p role="status" className="flex items-center gap-2 rounded-xl bg-emerald-500/10 p-3 text-sm"><Check size={16} className="shrink-0 text-emerald-600" />{notice}</p>}
    {removed && <p className="rounded-xl border border-border bg-muted/20 p-3 text-sm">{t("This photo is removed from the inbox. Its originals and any questions already added to review are preserved.")}</p>}
    {!!item.uploads?.length && <details className="rounded-xl border border-border p-3 text-xs"><summary className="cursor-pointer font-medium">{t("Upload history")}</summary><ol className="mt-2 space-y-2 text-muted-foreground">{item.uploads.map((upload, index) => <li key={`${upload.received_at}:${index}`}>{t("Received: {{date}}", { date: new Date(upload.received_at * 1000).toLocaleString(i18n.language) })}{upload.source_sent_at && <span className="ml-2">{t("Sent in WeChat: {{date}}", { date: new Date(upload.source_sent_at * 1000).toLocaleString(i18n.language) })}</span>}</li>)}</ol></details>}
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div className="space-y-3">
        <h3 className="text-sm font-semibold">{t("Original photos")}</h3>
        {!sourceReady && <p role="status" className="rounded-lg border border-border p-3 text-xs text-muted-foreground">{t("Saving answer exposure…")}<button type="button" onClick={() => { setError(""); setSourceRevision(value => value + 1); }} className="ml-3 underline">{t("Retry")}</button></p>}
        {sourceReady && assets.filter(Boolean).map((asset, index) => <figure key={asset.id} className="overflow-hidden rounded-xl border border-border bg-muted/20"><a href={scopedUrl(asset.url, workspaceId)} target="_blank" rel="noreferrer" className="block p-3" aria-label={t("Open original photo {{number}}", { number: index + 1 })}><img src={scopedUrl(asset.url, workspaceId)} alt={t("Saved original photo {{number}}", { number: index + 1 })} className="max-h-[70vh] w-full object-contain" /></a><figcaption className="border-t border-border px-3 py-2 text-xs text-muted-foreground">{index === 0 ? t("First original") : t("Supplemental photo")} · {asset.filename}</figcaption></figure>)}
        <p className="text-xs leading-relaxed text-muted-foreground">{t("Retrying recognition or adding a clearer photo keeps the first original and its received time.")}</p>
        {canRetry && <div className="flex flex-wrap gap-2"><button type="button" disabled={busy || stale} onClick={() => void run(() => retryPhotoInboxItem(item.id, item.version, workspaceId), t("Recognition queued again. The original is preserved."))} className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm disabled:opacity-40"><RefreshCw size={15} />{t("Retry recognition")}</button><label className={`inline-flex cursor-pointer items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm ${busy || stale ? "pointer-events-none opacity-40" : ""}`}><Upload size={15} />{t("Add clearer photo")}<input type="file" accept="image/jpeg,image/png,image/webp" disabled={busy || stale} className="sr-only" onChange={event => { void supplement(event.target.files?.[0]); event.target.value = ""; }} /></label></div>}
        <div className="border-t border-border pt-3"><button type="button" disabled={busy || stale || PHOTO_IN_FLIGHT.has(item.status)} onClick={() => void run(() => removed ? restorePhotoInboxItem(item.id, item.version, workspaceId) : removePhotoInboxItem(item.id, item.version, workspaceId), removed ? t("Photo restored to the inbox.") : t("Photo removed from the inbox. Originals and review questions are preserved."))} className="text-sm text-muted-foreground underline disabled:opacity-40">{removed ? t("Restore to inbox") : t("Remove from inbox")}</button></div>
      </div>
      <div className="min-w-0 space-y-4">
        {PHOTO_IN_FLIGHT.has(item.status) && <p role="status" className="flex items-start gap-2 rounded-xl border border-border p-4 text-sm"><Loader2 size={17} className="mt-0.5 shrink-0 animate-spin" />{t("Your original is saved. Recognition continues in the background; you can leave this page.")}</p>}
        {item.error && <p className="rounded-xl bg-amber-500/10 p-3 text-sm text-amber-800 dark:text-amber-200">{item.error}</p>}
        {!drafts.length && !PHOTO_IN_FLIGHT.has(item.status) && <div className="rounded-xl border border-dashed border-border p-5 text-sm text-muted-foreground">{t("No editable question was recognized. Your original is safe. Retry recognition or add a clearer photo.")}</div>}
        <label className="block text-xs font-medium text-muted-foreground">{t("Archive subject")}<select value={archiveSubject} disabled={locked || busy || stale} onChange={event => { const next = event.target.value as PhotoSubject; setSubject(next); setDrafts(current => current.map(draft => ({ ...draft, subject: next }))); setSubjectChanged(true); setDirty(true); setReviewed(false); }} className={inputClass}>{Object.entries(PHOTO_SUBJECTS).map(([id, label]) => <option key={id} value={id}>{t(id === "unclassified" && archiveSubjectLabel === "photoSubject.mixed" ? archiveSubjectLabel : label)}</option>)}</select><span className="mt-1 block font-normal">{t("Changing the archive subject applies it to every question below.")}</span></label>
        {drafts.length > 1 && <p className="text-xs text-muted-foreground">{t("Each question has its own subject. All questions below share this original photo.")}</p>}
        <div className="space-y-4">{drafts.map((draft, index) => <fieldset key={draft.draft_id} disabled={locked || busy || stale} className="min-w-0 space-y-3 rounded-xl border border-border bg-card p-4"><legend className="px-1 text-sm font-semibold">{t("Question {{number}}", { number: index + 1 })}</legend>
          <label className="flex items-center gap-2 text-sm font-medium"><input type="checkbox" checked={draft.selected !== false} onChange={event => editDraft(index, { selected: event.target.checked })} />{t("Include question {{number}} in review", { number: index + 1 })}</label>
          <div className="grid gap-3 sm:grid-cols-2"><label className="text-xs font-medium text-muted-foreground">{t("Question subject")}<select value={draft.subject} onChange={event => editDraft(index, { subject: event.target.value as PhotoSubject })} className={inputClass}>{Object.entries(PHOTO_SUBJECTS).map(([id, label]) => <option key={id} value={id}>{t(label)}</option>)}</select></label><label className="text-xs font-medium text-muted-foreground">{t("Question type")}<select value={draft.question_type} onChange={event => editDraft(index, { question_type: event.target.value as PhotoDraft['question_type'] })} className={inputClass}>{Object.entries(QUESTION_TYPES).map(([id, label]) => <option key={id} value={id}>{t(label)}</option>)}</select></label></div>
          <label className="block text-xs font-medium text-muted-foreground">{t("Question")}<textarea value={draft.question} onChange={event => editDraft(index, { question: event.target.value })} rows={5} className={inputClass} /></label>
          <div className="grid gap-3 sm:grid-cols-2"><label className="text-xs font-medium text-muted-foreground">{t("Grade (optional)")}<input value={draft.grade || ""} maxLength={100} placeholder={t("For example: Grade 5")} onChange={event => editDraft(index, { grade: event.target.value })} className={inputClass} /></label><label className="text-xs font-medium text-muted-foreground">{t("Knowledge point")}<input value={draft.knowledge_point || ""} maxLength={200} onChange={event => editDraft(index, { knowledge_point: event.target.value })} className={inputClass} /></label></div>
          <label className="block text-xs font-medium text-muted-foreground">{t("Mistake tags (comma separated)")}<input value={(draft.tags || []).join(", ")} onChange={event => editDraft(index, { tags: event.target.value.split(/[,，]/).map(value => value.trim()).filter(Boolean) })} className={inputClass} /></label>
          {Object.entries(draft.options || {}).map(([key, value]) => <label key={key} className="block text-xs font-medium text-muted-foreground">{t("Option {{label}}", { label: key })}<input value={value} onChange={event => editDraft(index, { options: { ...draft.options, [key]: event.target.value } })} className={inputClass} /></label>)}
          <label className="block text-xs font-medium text-muted-foreground">{t("Correct answer (optional)")}<input value={sourceReady ? draft.correct_answer || "" : ""} onChange={event => editDraft(index, { correct_answer: event.target.value, answer_origin: event.target.value.trim() ? "user" : "missing" })} className={inputClass} /></label>
          {draft.answer_origin === "model_suggested" && draft.correct_answer && <label className="flex items-start gap-2 rounded-lg bg-amber-500/10 p-3 text-xs leading-relaxed"><input type="checkbox" className="mt-0.5" checked={false} onChange={() => editDraft(index, { answer_origin: "user" })} /><span>{t("This answer is an AI suggestion. I checked it and want to use it.")}</span></label>}
          <p className="text-xs text-muted-foreground">{t("An answer is optional. Leave it blank if the photo does not contain one.")}</p>
          {!!draft.warnings?.length && <ul className="list-inside list-disc rounded-lg bg-amber-500/10 p-3 text-xs leading-relaxed text-amber-800 dark:text-amber-200">{draft.warnings.map((warning, i) => <li key={i}>{t(warning)}</li>)}</ul>}
          <details className="text-xs"><summary className="cursor-pointer text-muted-foreground">{t("Recognized explanation (optional)")}</summary><textarea aria-label={t("Explanation for question {{number}}", { number: index + 1 })} value={sourceReady ? draft.explanation || "" : ""} onChange={event => editDraft(index, { explanation: event.target.value })} rows={3} className={inputClass} /></details>
        </fieldset>)}</div>
        <label className="block text-xs font-medium text-muted-foreground">{t("My note / reason for the mistake")}<textarea value={note} disabled={locked || busy || stale} onChange={event => { setNote(event.target.value); setDirty(true); setReviewed(false); }} rows={3} maxLength={20000} className={inputClass} /></label>
        {item.status === "confirmed" ? <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4"><p className="flex items-center gap-2 text-sm font-medium"><Check size={17} />{t("These questions are linked to your question bank. Existing mastery and pause states are preserved.")}</p><div className="mt-3 flex flex-wrap gap-3">{item.entry_ids?.map((entryId, index) => <a key={entryId} href={scopedUrl(practiceRoute(new URLSearchParams({ question: String(entryId) }), ""), workspaceId)} className="inline-flex items-center gap-2 text-sm text-primary underline">{item.entry_ids.length === 1 ? t("Review this question") : t("Review question {{number}}", { number: index + 1 })}<ArrowRight size={15} /></a>)}</div></div> : <div className="space-y-3 rounded-xl border border-border p-4">
          <label className="flex items-start gap-2 text-sm leading-relaxed"><input type="checkbox" className="mt-1" checked={reviewed} disabled={!canConfirm || busy || stale} onChange={event => setReviewed(event.target.checked)} /><span>{t("I checked every selected question and subject against the original photo before adding them to review.")}</span></label>
          {selectedDrafts.some(draft => draft.subject === "unclassified") && <p className="text-xs text-amber-700 dark:text-amber-300">{t("Choose a subject for every question first.")}</p>}
          <div className="flex flex-wrap gap-2"><button type="button" disabled={!dirty || busy || locked || stale} onClick={() => void run(() => updatePhotoInboxItem(item.id, changes, workspaceId), t("Photo changes saved."))} className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-2.5 text-sm disabled:opacity-40"><Save size={15} />{t("Save changes")}</button><button type="button" disabled={!reviewed || !canConfirm || busy || stale} onClick={() => void run(async () => {
            const saved = dirty ? await updatePhotoInboxItem(item.id, changes, workspaceId) : item;
            return confirmPhotoInboxItem(item.id, saved.version, drafts, workspaceId);
          }, t("Questions added to review."))} className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground disabled:opacity-40">{busy ? <Loader2 size={16} className="animate-spin" /> : <Check size={16} />}{t("Confirm and add to review")}</button></div>
          <p className="text-xs leading-relaxed text-muted-foreground">{t("Collected questions are self-reported mistakes. This does not count as an automatically graded wrong answer.")}</p>
        </div>}
      </div>
    </div>
  </section>;
}
