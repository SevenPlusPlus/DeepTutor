import { afterEach, expect, it, vi } from "vitest";
import {
  confirmPhotoInboxItem, getPhotoInboxItem, listPhotoInbox, removePhotoInboxItem, restorePhotoInboxItem,
  retryPhotoInboxItem, supplementPhotoInboxItem, updatePhotoInboxItem,
} from "@/lib/photo-inbox-api";

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState({}, "", "/"); });

it("keeps photo reads and writes in the active workspace and uses stable item paths", async () => {
  window.history.replaceState({}, "", "/space/questions?workspace=math-space&view=photos");
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify({}), { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  await listPhotoInbox({ status: "removed", q: "x + y", since: 123, until: 456 });
  await updatePhotoInboxItem("photo id", { expected_version: 2, note: "Check sign" });
  await retryPhotoInboxItem("photo id", 2);
  await confirmPhotoInboxItem("photo id", 2, []);
  await removePhotoInboxItem("photo id", 2);
  await restorePhotoInboxItem("photo id", 3);
  const calls = fetch.mock.calls;
  const list = new URL(calls[0][0], "http://localhost");
  expect(list.pathname).toBe("/api/question-notebook/practice/photo-inbox");
  expect(Object.fromEntries(list.searchParams)).toEqual({ status: "removed", q: "x + y", since: "123", until: "456", dt_workspace: "math-space" });
  expect(calls.slice(1).map(call => new URL(call[0], "http://localhost").pathname)).toEqual([
    "/api/question-notebook/practice/photo-inbox/photo%20id",
    "/api/question-notebook/practice/photo-inbox/photo%20id/retry",
    "/api/question-notebook/practice/photo-inbox/photo%20id/confirm",
    "/api/question-notebook/practice/photo-inbox/photo%20id/remove",
    "/api/question-notebook/practice/photo-inbox/photo%20id/restore",
  ]);
  expect(calls.every(call => new URL(call[0], "http://localhost").searchParams.get("dt_workspace") === "math-space")).toBe(true);
  expect(calls[1][1]).toEqual(expect.objectContaining({ method: "PATCH", credentials: "include", body: JSON.stringify({ expected_version: 2, note: "Check sign" }) }));
  expect(JSON.parse(calls[3][1].body)).toEqual({ expected_version: 2, drafts: [], confirmed: true });
});

it("sends a supplemental photo as multipart data without overriding the browser boundary", async () => {
  const fetch = vi.fn().mockImplementation(async () => new Response("{}", { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  const photo = new File(["original"], "clear.png", { type: "image/png" });
  await supplementPhotoInboxItem("photo-1", 7, photo);
  const init = fetch.mock.calls[0][1];
  expect(init.method).toBe("POST");
  expect(init.headers).toBeUndefined();
  expect(init.body.get("file")).toBe(photo);
  expect(init.body.get("expected_version")).toBe("7");
});

it("pins every action to the saved photo's workspace instead of the page workspace", async () => {
  window.history.replaceState({}, "", "/space/questions?workspace=wrong-page-space&view=photos");
  const fetch = vi.fn().mockImplementation(async () => new Response("{}", { headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  const image = new File(["image"], "supplement.png", { type: "image/png" });
  for (const workspace of ["saved-space", ""]) {
    await getPhotoInboxItem("shared-id", workspace);
    await updatePhotoInboxItem("shared-id", { expected_version: 1, note: "Save here" }, workspace);
    await retryPhotoInboxItem("shared-id", 1, workspace);
    await confirmPhotoInboxItem("shared-id", 1, [], workspace);
    await removePhotoInboxItem("shared-id", 1, workspace);
    await restorePhotoInboxItem("shared-id", 1, workspace);
    await supplementPhotoInboxItem("shared-id", 1, image, workspace);
  }
  expect(fetch.mock.calls.slice(0, 7).every(([url]) => new URL(url, "http://localhost").searchParams.get("dt_workspace") === "saved-space")).toBe(true);
  expect(fetch.mock.calls.slice(7).every(([url]) => new URL(url, "http://localhost").searchParams.get("dt_workspace") === "")).toBe(true);
});
