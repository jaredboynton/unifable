import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  extractGeminiOutput,
  hasContent,
  runGeminiTrace,
} from "../gemini-trace.mjs";

test("hasContent rejects whitespace-only", () => {
  assert.equal(hasContent(""), false);
  assert.equal(hasContent("\n  \n"), false);
  assert.equal(hasContent("x"), true);
});

test("extractGeminiOutput parses json response field", () => {
  const raw = JSON.stringify({ session_id: "abc", response: "## Flow\nhello" });
  assert.equal(extractGeminiOutput(raw, "json"), "## Flow\nhello");
});

test("extractGeminiOutput falls back to raw text", () => {
  assert.equal(extractGeminiOutput("plain markdown", "json"), "plain markdown");
});

test("extractGeminiOutput text mode passes through", () => {
  assert.equal(extractGeminiOutput("  answer  ", "text"), "answer");
});

test("runGeminiTrace uses fake gemini binary", async () => {
  const dir = mkdtempSync(join(tmpdir(), "explore-gemini-trace-"));
  const fakeBin = join(dir, "gemini");
  writeFileSync(
    fakeBin,
    `#!/bin/sh
printf '%s\\n' '{"response":"## Summary\\ntrace ok"}'
`,
    { mode: 0o755 },
  );

  const { answer } = await runGeminiTrace({
    prompt: "QUESTION: test",
    workspace: dir,
    geminiBin: fakeBin,
    model: "gemini-3.1-flash-lite",
    timeoutSec: 10,
    outputFormat: "json",
  });

  assert.match(answer, /trace ok/);
});
