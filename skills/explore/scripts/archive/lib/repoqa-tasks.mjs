// repoqa-tasks.mjs — stub adapter for future RepoQA SNF JSON tasks.

export function loadRepoQATasks() {
  // Future: load official RepoQA needle JSON from benchmarks/repoqa/
  return [];
}

export function repoqaAdapterStatus() {
  return {
    implemented: false,
    message: "RepoQA SNF adapter not implemented in v1; use fixtures/bench-needles/tasks.jsonl",
  };
}
