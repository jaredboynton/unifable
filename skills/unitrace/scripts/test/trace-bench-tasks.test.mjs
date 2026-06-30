import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TASKS_FILE = path.resolve(HERE, "../bench/trace-repo-matrix.json");

function expandHome(p) {
  return p && p.startsWith("~") ? path.join(os.homedir(), p.slice(1)) : p;
}

test("trace benchmark task matrix points at real repos and files", () => {
  const doc = JSON.parse(readFileSync(TASKS_FILE, "utf8"));
  assert.ok(Array.isArray(doc.tasks));
  assert.ok(doc.tasks.length >= 9);
  for (const task of doc.tasks) {
    assert.ok(task.id);
    assert.ok(["quick", "medium", "deep"].includes(task.depth));
    const repo = expandHome(task.repo);
    assert.ok(existsSync(repo), `missing repo: ${repo}`);
    for (const rel of task.expected_paths || []) {
      const abs = path.join(repo, rel);
      assert.ok(existsSync(abs), `missing expected path: ${abs}`);
    }
  }
});
