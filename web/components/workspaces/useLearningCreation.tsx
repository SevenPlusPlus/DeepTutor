"use client";

import { useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslation } from "react-i18next";
import Modal from "@/components/common/Modal";
import { useChatWorkspaces } from "@/hooks/useChatWorkspaces";
import { activeWorkspaceId, scopedUrl } from "@/lib/workspace-scope";
import { useCreateRequest } from "@/components/learning/create-request";

/** Pick a destination only when creating; URL scopes all ensuing uploads and writes. */
export function useLearningCreation(open: () => void) {
  const router = useRouter()
  const pathname = usePathname()
  const query = useSearchParams()
  useCreateRequest(open)
  const { t } = useTranslation()
  const { workspaces, error } = useChatWorkspaces()
  const [choosing, setChoosing] = useState(false)
  const [destination, setDestination] = useState(activeWorkspaceId)
  const dialog = <Modal isOpen={choosing} onClose={() => setChoosing(false)} title={t('Save new content to')} width="sm">
    <div className="p-5">
      <p className="text-[12.5px] leading-relaxed text-muted-foreground">{t('Only new content is saved here. Your existing materials remain visible in all workspaces.')}</p>
      <select aria-label={t('Save to workspace')} value={destination} onChange={event => setDestination(event.target.value)} className="mt-3 h-9 w-full rounded-lg border border-border bg-background px-2.5 text-[13px] outline-none focus:border-ring">
        <option value="">{t('Default workspace')}</option>
        {workspaces.filter(row => !row.archived && row.status === 'ready').map(row => <option key={row.workspace_id} value={row.workspace_id}>{row.display_name}</option>)}
      </select>
      {error && <p role="alert" className="mt-3 text-[11.5px] text-destructive">{error}</p>}
      <div className="mt-5 flex justify-end gap-2">
        <button type="button" className="h-8 rounded-lg px-3 text-[12px] text-muted-foreground hover:bg-muted" onClick={() => setChoosing(false)}>{t('Cancel')}</button>
        <button type="button" className="h-8 rounded-lg bg-primary px-3.5 text-[12px] font-semibold text-primary-foreground hover:opacity-90" onClick={() => {
          setChoosing(false)
          if (destination === activeWorkspaceId()) open()
          else {
            const next = new URLSearchParams(query.toString())
            next.delete('dt_workspace'); next.delete('workspace'); next.set('create', '1')
            router.push(scopedUrl(`${pathname}?${next}`, destination))
          }
        }}>{t('Continue')}</button>
      </div>
    </div>
  </Modal>
  // Legacy course links retain their source workspace for imported questions.
  return { begin: () => query.has("course") ? open() : setChoosing(true), dialog }
}
