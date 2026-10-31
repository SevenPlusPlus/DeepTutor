"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import { Check, FileUp, X } from "lucide-react";
import { QuestionBankSection } from "@/components/space/question-bank";
import SpaceSectionHeader from "@/components/space/SpaceSectionHeader";
import { practiceRoute, questionBankRoute } from "@/lib/learning-routes";
import { getPracticeSummary, type PracticeRef, type PracticeSummary } from "@/lib/practice-api";
import { LearningErrorState, LearningShell } from "../LearningShell";
import { ReviewHome } from "./ReviewHome";
import { WorkspaceLabel } from "@/components/workspaces/WorkspaceLabel";
import { activeWorkspaceId } from "@/lib/workspace-scope";
import { PracticeImport } from "./PracticeImport";
import { PracticeSession } from "./PracticeSession";
import { PracticeInsights } from "./PracticeInsights";
import { PhotoInbox } from "./PhotoInbox";
import { DailyReviewPlan } from "./DailyReviewPlan";

/** Shared collections and imports; the library surface omits practice-only tools. */
export function PracticePage({ mode = "practice" }: { mode?: "practice" | "library" }) {
  const search = useSearchParams();
  if (mode === "practice" && !search.has("course") && !search.has("question") && search.get("view") !== "photos") return <ReviewHome />;
  return <ScopedPracticePage mode={mode} initialImport={search.get("create") === "1"} />;
}

function ScopedPracticePage({ mode, initialImport }: { mode: "practice" | "library"; initialImport: boolean }) {
  const { t } = useTranslation();
  const libraryOnly = mode === "library";
  const pageRoute = libraryOnly ? questionBankRoute : practiceRoute;
  const router = useRouter();
  const search = useSearchParams();
  const courseId = search.get("course")?.trim() || "";
  const tab = search.get("view") === "photos" ? "photos" : search.get("view") === "mistakes" ? "mistakes" : "bank";
  const [summary, setSummary] = useState<PracticeSummary | null>(null);
  const [error, setError] = useState("");
  const [importOpen, setImportOpen] = useState(initialImport);
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);
  const [session, setSession] = useState<PracticeRef[] | null>(() => {
    const id = Number(search.get('question'));
    return Number.isSafeInteger(id) && id > 0 ? [{ id }] : null;
  });
  const summarySequence = useRef(0);
  const refresh = useCallback(async () => {
    const sequence = ++summarySequence.current;
    try {
      const result = await getPracticeSummary(courseId);
      if (sequence === summarySequence.current) {
        setSummary(result);
        setError("");
      }
    } catch (err) {
      if (sequence === summarySequence.current)
        setError(err instanceof Error ? err.message : String(err));
    }
  }, [courseId]);
  useEffect(() => {
    void refresh();
    const onFocus = () => {
      void refresh();
    };
    window.addEventListener("focus", onFocus);
    const timer = window.setInterval(onFocus, 60000);
    return () => {
      // Invalidate pending requests after this scope is replaced.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      summarySequence.current++;
      window.removeEventListener("focus", onFocus);
      window.clearInterval(timer);
    };
  }, [refresh]);
  function closeSession() {
    setSession(null);
    setRevision(value => value + 1);
    void refresh();
  }
  function switchTab(view: "bank" | "mistakes" | "photos") {
    const next = new URLSearchParams(search.toString());
    next.set("view", view);
    if (view === "photos") next.delete("course");
    router.replace(pageRoute(next), { scroll: false });
  }
  return (
    <PracticePageShell
      libraryOnly={libraryOnly}
      action={
        !session && (
          <button
            type="button"
            onClick={() => setImportOpen(open => !open)}
            aria-expanded={importOpen}
            className="inline-flex items-center gap-2 rounded-xl border border-border bg-card px-4 py-2.5 text-sm font-medium hover:bg-muted"
          >
            <FileUp size={16} />
            {t("Import questions")}
          </button>
        )
      }
      scopeChip={
        courseId && (
          <span className="inline-flex items-center gap-2 rounded-full border border-border px-3 py-1 text-xs text-muted-foreground">
            {t("This course")}
            <button
              type="button"
              aria-label={t("Show every course")}
              onClick={() => {
                const next = new URLSearchParams(search.toString());
                next.delete("course");
                router.replace(pageRoute(next));
              }}
            >
              <X size={13} />
            </button>
          </span>
        )
      }
    >
      {!libraryOnly && <div className="mb-4 flex items-center gap-3 text-xs"><a href="/learning/practice" className="underline">{t("All workspaces")}</a><WorkspaceLabel row={{ content_workspace_id: activeWorkspaceId() }} /></div>}
      {session ? (
        <PracticeSession questions={session} onClose={closeSession} backLabel={libraryOnly ? t("Back to library") : undefined} />
      ) : (
        <>
          {notice && (
            <div
              role="status"
              className="mb-4 flex items-start gap-2 rounded-xl bg-emerald-500/10 p-3 text-sm"
            >
              <Check size={16} className="mt-0.5 shrink-0 text-emerald-600" />
              {notice}
            </div>
          )}
          {importOpen && (
            <PracticeImport
              key={tab}
              initialTarget={tab === "photos" ? "mistakes" : tab}
              courseId={courseId}
              onClose={() => setImportOpen(false)}
              onImported={message => {
                setNotice(message);
                setImportOpen(false);
                setRevision(value => value + 1);
                void refresh();
              }}
            />
          )}
          {error && <LearningErrorState message={error} onRetry={() => void refresh()} />}
          {!libraryOnly && <PracticeInsights courseId={courseId} revision={revision} />}
          {!libraryOnly && <div className="mb-7"><DailyReviewPlan courseId={courseId} summary={summary} revision={revision} onStart={setSession} onChanged={() => void refresh()} /></div>}
          <div
            role="tablist"
            aria-label={t("Practice collections")}
            className="mb-5 flex gap-5 border-b border-border"
          >
            {(["bank", "mistakes", "photos"] as const).map(view => (
              <button
                key={view}
                id={`practice-tab-${view}`}
                type="button"
                role="tab"
                aria-selected={tab === view}
                tabIndex={tab === view ? 0 : -1}
                onKeyDown={event => {
                  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                    event.preventDefault();
                    const views = ["bank", "mistakes", "photos"] as const;
                    const next = views[(views.indexOf(view) + (event.key === "ArrowRight" ? 1 : 2)) % views.length];
                    switchTab(next);
                    document.getElementById(`practice-tab-${next}`)?.focus();
                  }
                }}
                aria-controls="practice-collection"
                onClick={() => switchTab(view)}
                className={`border-b-2 px-1 pb-3 text-sm ${tab === view ? "border-primary font-semibold text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}
              >
                {view === "bank" ? t("Question Bank") : view === "photos" ? t("WeChat photos") : t("Mistakes")}
                {view !== "photos" && <span className="ml-2 text-xs tabular-nums text-muted-foreground">
                  {summary ? (view === "bank" ? summary.total : summary.mistakes) : "—"}
                </span>}
              </button>
            ))}
          </div>
          <div id="practice-collection" role="tabpanel" aria-labelledby={`practice-tab-${tab}`}>
            {tab === "photos" ? <PhotoInbox key={activeWorkspaceId()} onConfirmed={() => { setRevision(value => value + 1); void refresh(); }} /> : <QuestionBankSection
              key={`${tab}:${courseId}:${revision}`}
              embedded
              courseId={courseId}
              onChanged={refresh}
              mistakesOnly={tab === "mistakes"}
              onPractice={
                ids => {
                      setSession(ids.map(id => ({ id })));
                      setNotice("");
                    }
              }
            />}
          </div>
        </>
      )}
    </PracticePageShell>
  );
}

function PracticePageShell({
  libraryOnly,
  action,
  scopeChip,
  children,
}: {
  libraryOnly: boolean;
  action: ReactNode;
  scopeChip: ReactNode;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  if (libraryOnly) {
    // SpaceMain supplies the Learning Space breadcrumb and page scroll container.
    return (
      <>
        <SpaceSectionHeader
          title={t("Question Bank")}
          description={t("Questions from every source, together with your own imports.")}
          action={action}
          meta={scopeChip}
        />
        {children}
      </>
    );
  }
  return (
    <LearningShell
      title={t("Practice")}
      subtitle={t("Collect what you learn. Practice what matters. Remember it for longer.")}
      action={action}
    >
      {scopeChip && <div className="mb-4">{scopeChip}</div>}
      {children}
    </LearningShell>
  );
}
