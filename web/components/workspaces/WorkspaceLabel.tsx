"use client";

import { useTranslation } from "react-i18next";

/** Identify the source workspace when a review combines several collections. */
export function WorkspaceLabel({ row }: { row: { content_workspace_id?: string; content_workspace_name?: string } }) {
  const { t } = useTranslation();
  return <span className="text-xs text-muted-foreground">{row.content_workspace_name || row.content_workspace_id || t("Default workspace")}</span>;
}
