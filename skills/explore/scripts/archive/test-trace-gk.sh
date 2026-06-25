#!/bin/sh
':' //; exec node --test "$0" "$@"

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const scriptDir = __dirname;
const fixture = path.join(scriptDir, "fixtures/search-mini-repo");
const replay = path.join(scriptDir, "fixtures/grok-trace-replay.json");

function runNode(args, options = {}) {
  const result = spawnSync(process.execPath, args, {
    cwd: scriptDir,
    env: { ...process.env, XAI_API_KEY: "test-key-offline-replay", ...(options.env || {}) },
    encoding: "utf8",
  });
  assert.equal(
    result.status,
    0,
    [result.stdout, result.stderr].filter(Boolean).join("\n")
  );
  return result;
}

test("grok-trace structured replay smoke", () => {
  const workDir = fs.mkdtempSync(path.join(os.tmpdir(), "explore-trace-gk-test."));
  try {
    const out = path.join(workDir, "out");
    const raw = path.join(workDir, "raw");
    const err = path.join(workDir, "err");
    const structured = path.join(workDir, "structured.json");
    const prompt = path.join(workDir, "prompt.txt");
    const submit = path.join(workDir, "submit.txt");

    fs.writeFileSync(prompt, "QUESTION: where is stop handled?\n");
    fs.writeFileSync(submit, "submit instructions\n");

    runNode([
      path.join(scriptDir, "grok-trace.mjs"),
      "--prompt-file", prompt,
      "--submit-prompt-file", submit,
      "--workspace", fixture,
      "--out", out,
      "--raw", raw,
      "--err", err,
      "--structured-out", structured,
      "--replay", replay,
    ]);

    const output = fs.readFileSync(out, "utf8");
    assert.match(output, /Stop gate/);
    assert.match(output, /## Flow/);
    assert.match(output, /gate_stop\.py/);
    assert.ok(fs.statSync(structured).size > 0);
  } finally {
    fs.rmSync(workDir, { recursive: true, force: true });
  }
});

test("grok-trace unit coverage", () => {
  runNode(["--test", path.join(scriptDir, "test/test-grok-trace.mjs")]);
});

test("trace schema unit coverage", () => {
  runNode(["--test", path.join(scriptDir, "test/test-trace-schema.mjs")]);
});
