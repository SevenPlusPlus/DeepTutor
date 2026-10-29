"use client";

import { Camera } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";
import { getPartnerWorkspaces } from "@/lib/partners-api";
import { listWorkspaces, workspaceLabel, type ChatWorkspaceRegistration } from "@/lib/workspaces-api";

export function WeixinPhotoCollectionSettings({ value, onChange, partnerId }: {
  value: Record<string, unknown>; onChange: (value: Record<string, unknown>) => void; partnerId?: string;
}) {
  const { t, i18n } = useTranslation();
  const workspaceSelectId = useId();
  const [workspaces, setWorkspaces] = useState<ChatWorkspaceRegistration[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const enabled = value.photo_collection_enabled === true;
  const selectedWorkspace = typeof value.photo_workspace_id === "string" ? value.photo_workspace_id : "";
  useEffect(() => {
    if (!enabled) return;
    let active = true;
    void (partnerId ? getPartnerWorkspaces(partnerId) : listWorkspaces(true))
      .then(rows => {
        if (!active) return;
        setWorkspaces(rows.filter(row => row.kind !== "system" && !row.archived && row.status === "ready"));
        setFailed(false);
      })
      .catch(() => { if (active) setFailed(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [enabled, partnerId, attempt]);
  return <section aria-labelledby="weixin-photo-settings" className="space-y-3 rounded-xl border border-border bg-muted/20 p-4">
    <h4 id="weixin-photo-settings" className="flex items-center gap-2 text-sm font-medium"><Camera size={16} />{t("Photo collection")}</h4>
    <label className="flex items-start gap-2 text-sm"><input type="checkbox" checked={enabled} onChange={event => onChange({ ...value, photo_collection_enabled: event.target.checked })} className="mt-0.5" /><span>{t("Automatically save question photos from linked private chats")}</span></label>
    <p className="text-xs leading-relaxed text-muted-foreground">{t("Photos are saved to each linked sender's own question library, then organized by subject and first received time. They enter review only after the questions are checked.")}</p>
    {enabled && <div className="space-y-2">
      <label htmlFor={workspaceSelectId} className="block text-xs font-medium text-muted-foreground">{t("Save photos to")}</label>
      <select id={workspaceSelectId} value={selectedWorkspace} disabled={loading} onChange={event => onChange({ ...value, photo_workspace_id: event.target.value })} className="block w-full rounded-lg border border-border bg-background p-2 text-sm text-foreground disabled:opacity-60">
        <option value="">{t("Each sender's default learning workspace")}</option>
        {selectedWorkspace && !workspaces.some(row => row.workspace_id === selectedWorkspace) && <option value={selectedWorkspace} disabled>{loading ? t("Loading…") : t("Configured workspace unavailable")}</option>}
        {workspaces.map(row => <option key={row.workspace_id} value={row.workspace_id}>{workspaceLabel(row, i18n.language.startsWith("zh"))}</option>)}
      </select>
      <p className="text-xs leading-relaxed text-muted-foreground">{t("A chosen workspace must belong to the linked sender. Keep each sender's default when collecting from other people.")}</p>
      {failed && <p role="alert" className="text-xs text-destructive">{t("Failed to load workspaces")} <button type="button" onClick={() => { setLoading(true); setAttempt(current => current + 1); }} className="underline">{t("Retry")}</button></p>}
    </div>}
    <p className="text-xs text-muted-foreground">{t("With collection off, add /错题 or 加入错题本 to a photo message to collect it once.")}</p>
  </section>;
}
