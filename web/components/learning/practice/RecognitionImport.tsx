"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AlertTriangle, Camera, Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import {
  getPracticeRecognition,
  getCurrentPracticeRecognition,
  stagePracticeRecognition,
  startPracticeRecognition,
  type ImportPreview,
  type RecognitionDraft,
  type RecognitionSnapshot,
} from "@/lib/practice-api";

const TERMINAL = new Set(["ready", "failed", "expired", "staged", "committed"]);

export function RecognitionImport({
  target,
  courseId,
  disabled,
  onTargetRecovered,
  onStaged,
}: {
  target: "bank" | "mistakes";
  courseId: string;
  disabled: boolean;
  onTargetRecovered: (target: "bank" | "mistakes") => void;
  onStaged: (preview: ImportPreview, filename: string) => void;
}) {
  const { t } = useTranslation();
  const [jobId, setJobId] = useState("");
  const [filename, setFilename] = useState("");
  const [snapshot, setSnapshot] = useState<RecognitionSnapshot | null>(null);
  const [drafts, setDrafts] = useState<RecognitionDraft[]>([]);
  const [busy, setBusy] = useState(false);
  const [recovering, setRecovering] = useState(true);
  const [error, setError] = useState("");
  const staged = useRef(false);

  const adoptSnapshot = useCallback((next: RecognitionSnapshot) => {
    setJobId(next.job_id);
    setFilename(next.filename || "");
    setSnapshot(next);
    if (next.target) onTargetRecovered(next.target);
    if (next.status === "ready") {
      setDrafts(next.drafts.map(draft => ({ ...draft, selected: true })));
    }
  }, [onTargetRecovered]);

  useEffect(() => {
    let cancelled = false;
    void getCurrentPracticeRecognition()
      .then(current => {
        if (!cancelled && current) adoptSnapshot(current);
      })
      .catch(err => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setRecovering(false);
      });
    return () => {
      cancelled = true;
    };
  }, [adoptSnapshot]);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const next = await getPracticeRecognition(jobId);
        if (cancelled) return;
        adoptSnapshot(next);
        if (next.status === "ready") {
          return;
        }
        if (next.status === "failed" || next.status === "expired") {
          setError(next.error_message || t("Question recognition failed."));
          return;
        }
        timer = setTimeout(() => void poll(), 2000);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    }
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [adoptSnapshot, jobId, t]);

  async function select(file?: File) {
    if (!file || busy) return;
    setBusy(true);
    setError("");
    setSnapshot(null);
    setDrafts([]);
    staged.current = false;
    setFilename(file.name);
    try {
      const started = await startPracticeRecognition(file, target, courseId);
      setJobId(started.job_id);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (message.includes("already running")) {
        try {
          const current = await getCurrentPracticeRecognition();
          if (current) {
            adoptSnapshot(current);
            return;
          }
        } catch {
          // Preserve the original, more actionable conflict below.
        }
      }
      setError(message);
    } finally {
      setBusy(false);
    }
  }

  function updateDraft(index: number, changes: Partial<RecognitionDraft>) {
    setDrafts(current =>
      current.map((draft, itemIndex) => itemIndex === index ? { ...draft, ...changes } : draft)
    );
  }

  async function stage() {
    if (!snapshot || snapshot.status !== "ready" || busy || staged.current) return;
    setBusy(true);
    setError("");
    try {
      const preview = await stagePracticeRecognition(snapshot.job_id, snapshot.version, drafts);
      staged.current = true;
      onStaged(preview, filename);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const selectedCount = drafts.filter(draft => draft.selected).length;
  const inFlight = Boolean(jobId && (!snapshot || !TERMINAL.has(snapshot.status)));
  return (
    <div className="mt-4 space-y-4">
      {!recovering && (!jobId || snapshot?.status === "failed" || snapshot?.status === "expired") ? (
        <label className="block rounded-xl border border-dashed border-border bg-muted/20 p-5 text-sm">
          <span className="mb-2 flex items-center gap-2 font-medium">
            <Camera size={17} />
            {t("Choose a photo, screenshot, or PDF")}
          </span>
          <input
            type="file"
            accept="image/jpeg,image/png,image/webp,application/pdf,.pdf"
            disabled={disabled || busy || Boolean(jobId && !snapshot)}
            onChange={event => {
              void select(event.target.files?.[0]);
              event.target.value = "";
            }}
            className="block w-full text-sm file:mr-3 file:rounded-lg file:border-0 file:bg-primary/10 file:px-3 file:py-2 file:text-primary"
          />
          <span className="mt-2 block text-xs text-muted-foreground">
            {t("Supports JPG, PNG, WebP and PDF up to 20 MB. PDFs may contain up to 20 pages. You will review every question before importing.")}
          </span>
        </label>
      ) : null}

      {recovering && !jobId && (
        <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 size={16} className="animate-spin" />
          {t("Checking current import task…")}
        </p>
      )}
      {(busy || inFlight) && (
        <div role="status" className="flex items-start gap-3 rounded-xl border border-border bg-muted/20 p-4 text-sm">
          <Loader2 size={17} className="mt-0.5 shrink-0 animate-spin" />
          <div className="min-w-0">
            <p className="truncate font-medium">{filename || t("Current import task")}</p>
            <p className="mt-0.5 text-muted-foreground">
              {snapshot?.progress_message || t("Starting question recognition…")}
            </p>
            {snapshot && snapshot.total_units > 1 && (
              <p className="mt-1 text-xs text-muted-foreground">
                {t("{{completed}} of {{total}} pages processed", {
                  completed: snapshot.completed_units,
                  total: snapshot.total_units,
                })}
              </p>
            )}
          </div>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}

      {snapshot?.status === "ready" && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
            <p className="font-medium">
              {filename} · {t("{{count}} recognized questions", { count: drafts.length })}
            </p>
            <p className="text-xs text-muted-foreground">
              {t("{{count}} selected", { count: selectedCount })}
            </p>
          </div>
          <ol className="space-y-3">
            {drafts.map((draft, index) => (
              <li key={draft.draft_id} className="rounded-xl border border-border p-4">
                <label className="flex items-center gap-2 text-sm font-medium">
                  <input
                    type="checkbox"
                    checked={Boolean(draft.selected)}
                    onChange={event => updateDraft(index, { selected: event.target.checked })}
                  />
                  {t("Use question {{number}}", { number: index + 1 })}
                </label>
                {draft.question_images[0]?.url && (
                  // Dynamic workspace-scoped backend URL; Next Image cannot optimize it.
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={draft.question_images[0].url}
                    alt={t("Source image for question {{number}}", { number: index + 1 })}
                    className="mt-3 max-h-64 w-auto max-w-full rounded-lg border border-border object-contain"
                  />
                )}
                <label className="mt-3 block text-xs font-medium text-muted-foreground">
                  {t("Question")}
                  <textarea
                    value={draft.question}
                    onChange={event => updateDraft(index, { question: event.target.value })}
                    rows={3}
                    className="mt-1 block w-full rounded-lg border border-border bg-background p-2 text-sm text-foreground"
                  />
                </label>
                <label className="mt-3 block text-xs font-medium text-muted-foreground">
                  {t("Correct answer (optional)")}
                  <input
                    value={draft.correct_answer}
                    onChange={event => updateDraft(index, {
                      correct_answer: event.target.value,
                      answer_origin: event.target.value.trim() ? "user" : "missing",
                    })}
                    className="mt-1 block w-full rounded-lg border border-border bg-background p-2 text-sm text-foreground"
                  />
                </label>
                {(draft.review_level === "required" || draft.warnings.length > 0) && (
                  <div className="mt-3 flex gap-2 rounded-lg bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
                    <AlertTriangle size={15} className="shrink-0" />
                    <span>
                      {draft.warnings.map(warning => t(warning)).join(" · ")
                        || t("Confirm or enter the answer before importing.")}
                    </span>
                  </div>
                )}
              </li>
            ))}
          </ol>
          <button
            type="button"
            disabled={disabled || busy || selectedCount === 0}
            onClick={() => void stage()}
            className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-40"
          >
            {t("Continue to import preview")}
          </button>
        </div>
      )}
    </div>
  );
}
