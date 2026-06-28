import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { buildRepoContext } from "../repo-context.mjs";

test("buildRepoContext returns empty when no docs present", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "repo-ctx-"));
  try {
    assert.equal(buildRepoContext(tmp), "");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("buildRepoContext extracts bullets from AGENTS.md", () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "repo-ctx-"));
  try {
    fs.writeFileSync(
      path.join(tmp, "AGENTS.md"),
      `# My App

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Auth | src/auth/ |
| API | src/api/ |

## Notes
- Uses PostgreSQL
`,
    );
    const ctx = buildRepoContext(tmp);
    assert.match(ctx, /Caller context \(from AGENTS\.md/);
    assert.match(ctx, /Auth: src\/auth\//);
    assert.match(ctx, /Do NOT re-recommend capabilities already documented/);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
