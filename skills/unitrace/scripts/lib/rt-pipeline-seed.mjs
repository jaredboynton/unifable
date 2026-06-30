// Deterministic host reads for known trace pipelines (no model turn).
import { existsSync } from "node:fs";
import { confine, toolReadRange } from "./htools.mjs";
import { normalizeReadPath } from "./trace-schema.mjs";

const TRACE_PIPELINE = [
  { path: "scripts/realtime-trace.mjs", start_line: 1, end_line: 80 },
  { path: "scripts/trace-rt.sh", start_line: 280, end_line: 380 },
];

const TRACE_SUBMIT_RENDER = [
  { path: "scripts/lib/rt-rehydrate-submit.mjs", start_line: 1, end_line: 140 },
  { path: "scripts/lib/render-trace-structured.mjs", start_line: 1, end_line: 120 },
];

const TEMPLATES = [
  {
    re: /\btrace\.sh\b/i,
    reads: TRACE_PIPELINE,
  },
  {
    re: /\btrace-rt\b/i,
    reads: [
      { path: "scripts/realtime-trace.mjs", start_line: 190, end_line: 340 },
      ...TRACE_PIPELINE,
    ],
  },
  {
    re: /\b(trace-rt|submit|pointer|render(?:ed|ing)? trace)\b/i,
    reads: [
      { path: "scripts/realtime-trace.mjs", start_line: 906, end_line: 1055 },
      ...TRACE_SUBMIT_RENDER,
      ...TRACE_PIPELINE,
    ],
  },
];

function resolveRead(workspace, spec) {
  const tries = spec.path.includes("/")
    ? [spec.path, `skills/unitrace/${spec.path}`]
    : [`scripts/${spec.path}`, `skills/unitrace/scripts/${spec.path}`, spec.path];
  for (const candidate of tries) {
    const abs = confine(workspace, candidate);
    if (!abs || !existsSync(abs)) continue;
    const rel = normalizeReadPath(workspace, candidate);
    if (!rel) continue;
    return { rel, start_line: spec.start_line, end_line: spec.end_line };
  }
  return null;
}

export function pipelineSeedReads(question, workspace, filesRead, onRead) {
  const q = String(question || "");
  const added = [];
  for (const tpl of TEMPLATES) {
    if (!tpl.re.test(q)) continue;
    for (const spec of tpl.reads) {
      const resolved = resolveRead(workspace, spec);
      if (!resolved || filesRead.has(resolved.rel)) continue;
      const r = toolReadRange(workspace, resolved.rel, {
        start_line: resolved.start_line,
        end_line: resolved.end_line,
        stripPreamble: process.env.UNITRACE_RT_STRIP_COMMENTS !== "0",
      });
      if (!r.ok) continue;
      onRead(resolved.rel, r.content || "");
      added.push(resolved.rel);
    }
  }
  return added;
}
