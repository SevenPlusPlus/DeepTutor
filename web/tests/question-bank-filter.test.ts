import assert from "node:assert/strict";
import test from "node:test";

import { buildQuestionBankFilter } from "@/components/space/question-bank/useQuestionBank";

test("the unresolved view selects active mistakes including reviewed photo imports", () => {
  const filter = buildQuestionBankFilter(
    { kind: "unresolved" },
    { source: "mastery_path", materialId: "path-1", scoreTrend: "declined" },
    "equation",
    "oldest",
  );

  assert.deepEqual(filter, {
    category_id: undefined,
    uncategorized: undefined,
    bookmarked: undefined,
    is_correct: undefined,
    source: "mastery_path",
    material_id: "path-1",
    learning_status: "active",
    subject: undefined,
    grade: undefined,
    score_trend: "declined",
    search: "equation",
    sort: "oldest",
    limit: 60,
  });
});

test("review filters preserve the selected source and material", () => {
  const filter = buildQuestionBankFilter(
    { kind: "all" },
    { source: "book", materialId: "book-1", scoreTrend: "" },
    "",
    "recent",
  );

  assert.equal(filter.source, "book");
  assert.equal(filter.material_id, "book-1");
});
