import { describe, expect, it } from "vitest";

import { workspaceKnowledgeDefaults } from "@/lib/workspace-knowledge-defaults";
import type { ChatWorkspaceRegistration } from "@/lib/workspaces-api";

function workspace(
  knowledgeBases: string[] | null,
): ChatWorkspaceRegistration {
  return {
    workspace_id: "ws_math",
    kind: "workspace",
    follows_root: true,
    display_name: "Math",
    path: "/materials/math",
    archived: false,
    status: "ready",
    error: "",
    created_at: "",
    resources: { skills: null, mcp: null, knowledge_bases: knowledgeBases },
  };
}

describe("workspace knowledge defaults", () => {
  it("attaches explicitly assigned knowledge bases to a new conversation", () => {
    expect(
      workspaceKnowledgeDefaults(workspace(["account:kb:algebra"]), [
        { id: "account:kb:algebra", name: "Algebra" },
        { id: "account:kb:geometry", name: "Geometry" },
      ]),
    ).toEqual(["account:kb:algebra"]);
  });

  it("does not turn inherited catalog access into an implicit selection", () => {
    expect(
      workspaceKnowledgeDefaults(workspace(null), [
        { id: "account:kb:algebra", name: "Algebra" },
      ]),
    ).toEqual([]);
  });

  it("drops stale and subagent references", () => {
    expect(
      workspaceKnowledgeDefaults(
        workspace(["missing", "account:kb:tutor", "account:kb:algebra"]),
        [
          {
            id: "account:kb:tutor",
            name: "Tutor",
            metadata: { type: "subagent" },
          },
          { id: "account:kb:algebra", name: "Algebra" },
        ],
      ),
    ).toEqual(["account:kb:algebra"]);
  });

  it("keeps only the last assigned PageIndex OSS base", () => {
    expect(
      workspaceKnowledgeDefaults(
        workspace([
          "account:kb:oss-a",
          "account:kb:vectors",
          "account:kb:oss-b",
        ]),
        [
          {
            id: "account:kb:oss-a",
            name: "OSS A",
            metadata: { rag_provider: "pageindex-oss" },
          },
          {
            id: "account:kb:vectors",
            name: "Vectors",
            statistics: { rag_provider: "lightrag" },
          },
          {
            id: "account:kb:oss-b",
            name: "OSS B",
            statistics: { rag_provider: "pageindex-oss" },
          },
        ],
      ),
    ).toEqual(["account:kb:vectors", "account:kb:oss-b"]);
  });
});
