import { knowledgeBaseRef } from "@/lib/knowledge-helpers";
import type { ChatWorkspaceRegistration } from "@/lib/workspaces-api";

interface KnowledgeBaseOption {
  id?: string;
  name: string;
  metadata?: { type?: string; rag_provider?: string };
  statistics?: { rag_provider?: string };
}

/**
 * Resolve the knowledge bases a new conversation should inherit from its
 * workspace.
 *
 * A null workspace assignment means "keep the account's normal catalog", not
 * "attach every knowledge base". Only an explicit assignment is a default.
 * Stale references are ignored, and connected agents are never mistaken for
 * retrieval knowledge bases. PageIndex OSS can open only one index per turn,
 * so the last assigned OSS base wins while ordinary bases remain selected.
 */
export function workspaceKnowledgeDefaults(
  workspace: ChatWorkspaceRegistration | null,
  catalog: KnowledgeBaseOption[],
): string[] {
  const assigned = workspace?.resources?.knowledge_bases;
  if (!assigned?.length) return [];

  const options = new Map(
    catalog
      .filter((item) => item.metadata?.type !== "subagent")
      .map((item) => [knowledgeBaseRef(item), item]),
  );
  const selected = Array.from(new Set(assigned)).filter((ref) => options.has(ref));
  const lastOss = selected.findLastIndex((ref) => {
    const option = options.get(ref);
    return (
      option?.metadata?.rag_provider || option?.statistics?.rag_provider || ""
    ) === "pageindex-oss";
  });

  return selected.filter((ref, index) => {
    const option = options.get(ref);
    const provider =
      option?.metadata?.rag_provider || option?.statistics?.rag_provider || "";
    return provider !== "pageindex-oss" || index === lastOss;
  });
}
