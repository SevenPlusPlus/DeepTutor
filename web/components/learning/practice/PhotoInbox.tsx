"use client";
/* eslint-disable @next/next/no-img-element -- Originals use authenticated workspace URLs. */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Camera, ChevronLeft, ChevronRight, ImageIcon, Loader2, RefreshCw, Search } from "lucide-react";
import { useTranslation } from "react-i18next";
import { activeWorkspaceId, scopedUrl } from "@/lib/workspace-scope";
import {
  getPhotoInboxItem, listPhotoInbox, PHOTO_SUBJECTS, PHOTO_IN_FLIGHT, PHOTO_STATUS_LABELS, photoSubjectLabel,
  type PhotoInboxFilters, type PhotoInboxItem, type PhotoInboxList,
} from "@/lib/photo-inbox-api";
import { PhotoInboxDetail } from "./PhotoInboxDetail";

const PAGE_SIZE = 24;

/** Original photos remain accessible even when question recognition fails. */
export function PhotoInbox({ onConfirmed }: { onConfirmed: () => void }) {
  const { t, i18n } = useTranslation();
  const [data, setData] = useState<PhotoInboxList | null>(null);
  const [subject, setSubject] = useState("");
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<PhotoInboxItem | null>(null);
  const [opening, setOpening] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const sequence = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const workspaceId = activeWorkspaceId();
  const pageQuery = new URLSearchParams(typeof window === "undefined" ? "" : window.location.search);
  const allWorkspaces = !pageQuery.has("dt_workspace") && !pageQuery.has("workspace");
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
  const filters = useMemo<PhotoInboxFilters>(() => {
    const until = to ? new Date(`${to}T00:00:00`) : null;
    if (until) until.setDate(until.getDate() + 1);
    return {
      subject, status, q: query.trim(), offset, limit: PAGE_SIZE,
      ...(allWorkspaces ? { all_workspaces: true } : {}),
      since: from ? new Date(`${from}T00:00:00`).getTime() / 1000 : undefined,
      until: until ? until.getTime() / 1000 : undefined,
    };
  }, [subject, status, query, from, to, offset, allWorkspaces]);
  const refresh = useCallback(async () => {
    const current = ++sequence.current;
    try {
      const next = await listPhotoInbox(filters);
      if (current !== sequence.current) return;
      setData({ ...next, items: next.items.map(item => ({ ...item, content_workspace_id: item.content_workspace_id ?? workspaceId })) });
      setError("");
    } catch (err) {
      if (current === sequence.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  }, [filters, workspaceId]);
  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 200);
    return () => {
      clearTimeout(timer);
      // Invalidate requests from the previous filter or workspace scope.
      // eslint-disable-next-line react-hooks/exhaustive-deps
      sequence.current++;
    };
  }, [refresh]);
  useEffect(() => {
    if (selected) return;
    const onFocus = () => void refresh();
    const timer = setInterval(onFocus, data?.items.some(item => PHOTO_IN_FLIGHT.has(item.status)) ? 3000 : 15000);
    window.addEventListener("focus", onFocus);
    return () => { clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, [refresh, data, selected]);
  useEffect(() => () => { sequence.current++; }, []);

  async function open(item: PhotoInboxItem) {
    setOpening(photoKey(item));
    setError("");
    try {
      const next = await getPhotoInboxItem(item.id, item.content_workspace_id ?? workspaceId);
      setSelected({ ...next, content_workspace_id: item.content_workspace_id ?? workspaceId, content_workspace_name: item.content_workspace_name });
    }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setOpening(""); }
  }
  function photoKey(item: PhotoInboxItem) { return JSON.stringify([item.content_workspace_id ?? workspaceId, item.id]); }
  function resetPage(change: () => void) { change(); setOffset(0); }
  const dayFormat = new Intl.DateTimeFormat(i18n.language, { dateStyle: "long", timeZone: timezone });
  const timeFormat = new Intl.DateTimeFormat(i18n.language, { timeStyle: "short", timeZone: timezone });
  const groups = new Map<string, PhotoInboxItem[]>();
  for (const item of data?.items ?? []) {
    const day = dayFormat.format(new Date(item.received_at * 1000));
    groups.set(day, [...(groups.get(day) ?? []), item]);
  }

  if (selected) return <PhotoInboxDetail key={photoKey(selected)} initial={selected} onConfirmed={onConfirmed} onBack={() => {
    setSelected(null); void refresh(); requestAnimationFrame(() => heading.current?.focus());
  }} />;

  return <section aria-labelledby="photo-inbox-heading" className="space-y-5">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 id="photo-inbox-heading" ref={heading} tabIndex={-1} className="flex items-center gap-2 text-lg font-semibold outline-none"><Camera size={20} />{t("WeChat photo inbox")}</h2>
        <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{t("Send a question photo to your linked WeChat partner. Originals are saved first; check the questions before adding them to review.")}</p>
      </div>
      <button type="button" onClick={() => void refresh()} className="inline-flex items-center gap-2 rounded-lg border border-border px-3 py-2 text-sm"><RefreshCw size={15} />{t("Refresh")}</button>
    </div>
    {data && allWorkspaces && <p className="text-xs text-muted-foreground">{t("Showing photos from all your learning workspaces.")}</p>}
    {!!data?.unavailable_workspaces?.length && <p role="status" className="rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm">{t("Some workspaces could not be loaded. Available content is shown.")}</p>}
    {data && <dl className="grid grid-cols-3 divide-x divide-border rounded-xl border border-border bg-card p-4">
      {[["Saved photos", data.counts.photos], ["Needs attention", data.counts.pending], ["In review", data.counts.confirmed]].map(([label, count]) => <div key={label} className="px-2 sm:px-4"><dd className="text-2xl font-semibold tabular-nums">{count}</dd><dt className="mt-1 text-xs text-muted-foreground">{t(String(label))}</dt></div>)}
    </dl>}
    <div className="rounded-xl border border-border bg-muted/20 p-4 text-sm">
      <p className="font-medium">{t("Enable photo collection in your partner's WeChat channel settings.")}</p>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{t("Only linked private chats are collected. With collection off, send a photo with /错题 or 加入错题本 to save it once.")}</p>
    </div>
    <div className="space-y-3">
      <div className="flex gap-2 overflow-x-auto pb-1" role="group" aria-label={t("Filter photos by subject")}>
        <button type="button" aria-pressed={!subject} onClick={() => resetPage(() => setSubject(""))} className={`shrink-0 rounded-full border px-3 py-1.5 text-sm ${!subject ? "border-primary bg-primary text-primary-foreground" : "border-border"}`}>{t("All subjects")}</button>
        {Object.entries(PHOTO_SUBJECTS).map(([id, label]) => <button type="button" key={id} aria-pressed={subject === id} onClick={() => resetPage(() => setSubject(id))} className={`shrink-0 rounded-full border px-3 py-1.5 text-sm ${subject === id ? "border-primary bg-primary text-primary-foreground" : "border-border"}`}>{t(label)}</button>)}
      </div>
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="relative block sm:col-span-2 xl:col-span-1"><span className="sr-only">{t("Search saved photos")}</span><Search size={16} className="absolute left-3 top-3 text-muted-foreground" /><input value={query} onChange={event => resetPage(() => setQuery(event.target.value))} placeholder={t("Search question or note")} className="w-full rounded-lg border border-border bg-background py-2.5 pl-9 pr-3 text-sm" /></label>
        <label className="text-xs text-muted-foreground">{t("Photo status")}<select value={status} onChange={event => resetPage(() => setStatus(event.target.value))} className="mt-1 block w-full rounded-lg border border-border bg-background p-2 text-sm text-foreground"><option value="">{t("All statuses")}</option><option value="pending">{t("Needs attention")}</option><option value="ready">{t("Archived")}</option><option value="confirmed">{t("In review")}</option><option value="removed">{t("Removed photos")}</option></select></label>
        <label className="text-xs text-muted-foreground">{t("Received from")}<input type="date" value={from} max={to || undefined} onChange={event => resetPage(() => setFrom(event.target.value))} className="mt-1 block w-full rounded-lg border border-border bg-background p-2 text-sm text-foreground" /></label>
        <label className="text-xs text-muted-foreground">{t("Received through")}<input type="date" value={to} min={from || undefined} onChange={event => resetPage(() => setTo(event.target.value))} className="mt-1 block w-full rounded-lg border border-border bg-background p-2 text-sm text-foreground" /></label>
      </div>
      <p className="text-xs text-muted-foreground">{t("Grouped by first received time · {{timezone}}", { timezone })}</p>
    </div>
    {error && <div role="alert" className="rounded-lg border border-destructive/30 p-3 text-sm text-destructive">{error}</div>}
    {loading ? <p role="status" className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 size={17} className="animate-spin" />{t("Loading…")}</p> : data && !data.items.length ? <div className="rounded-xl border border-dashed border-border px-5 py-12 text-center"><ImageIcon size={30} className="mx-auto mb-3 text-muted-foreground" /><p className="font-medium">{t("No photos found")}</p><p className="mt-1 text-sm text-muted-foreground">{t("New WeChat photos appear here after the original is saved. Try adjusting your filters.")}</p></div> : [...groups].map(([day, items]) => <section key={day} aria-label={day} className="space-y-3"><h3 className="text-sm font-medium">{day}</h3><div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">{items.map(item => <button key={photoKey(item)} type="button" disabled={!!opening} onClick={() => void open(item)} className="overflow-hidden rounded-xl border border-border bg-card text-left transition-colors hover:border-primary/50 focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary disabled:opacity-60">
      <div className="flex h-40 items-center justify-center overflow-hidden bg-muted/40 p-3">{item.asset?.url ? <img src={scopedUrl(item.asset.url, item.content_workspace_id ?? workspaceId)} alt={t("Saved original photo")} className="h-full w-full object-contain" /> : <ImageIcon size={28} className="text-muted-foreground" />}</div>
      <div className="space-y-3 p-4"><div className="flex items-center justify-between gap-2 text-xs"><span className="font-medium text-primary">{t(photoSubjectLabel(item))}</span><span className={`rounded-md px-2 py-1 ${item.status === "confirmed" ? "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300" : "bg-muted text-muted-foreground"}`}>{opening === photoKey(item) ? t("Loading…") : t(PHOTO_STATUS_LABELS[item.status])}</span></div><p className="line-clamp-2 min-h-10 text-sm font-medium">{item.drafts?.[0]?.question || item.asset?.filename || t("Saved original photo")}</p>{item.content_workspace_id !== undefined && <p className="text-xs text-muted-foreground">{t("Saved in {{workspace}}", { workspace: item.content_workspace_name || t(item.content_workspace_id ? "Workspace" : "Default workspace") })}</p>}<div className="flex flex-wrap justify-between gap-1 border-t border-border pt-3 text-xs text-muted-foreground"><span>{timeFormat.format(new Date(item.received_at * 1000))}</span><span>{item.upload_count > 1 ? t("{{count}} uploads · same original", { count: item.upload_count }) : t("Original preserved")}{item.drafts?.length > 1 && ` · ${t("{{count}} questions", { count: item.drafts.length })}`}</span></div></div>
    </button>)}</div></section>)}
    {data && data.total > PAGE_SIZE && <nav aria-label={t("Photo pages")} className="flex items-center justify-between gap-3 border-t border-border pt-4 text-sm"><button type="button" disabled={!offset} onClick={() => setOffset(value => Math.max(0, value - PAGE_SIZE))} className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-2 disabled:opacity-40"><ChevronLeft size={15} />{t("Previous")}</button><span className="text-xs text-muted-foreground">{t("{{start}}–{{end}} of {{total}} photos", { start: offset + 1, end: Math.min(offset + PAGE_SIZE, data.total), total: data.total })}</span><button type="button" disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(value => value + PAGE_SIZE)} className="inline-flex items-center gap-1 rounded-lg border border-border px-3 py-2 disabled:opacity-40">{t("Next")}<ChevronRight size={15} /></button></nav>}
  </section>;
}
