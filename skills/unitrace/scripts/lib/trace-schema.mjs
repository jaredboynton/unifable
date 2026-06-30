// Structured trace schema, validation, and strict grounding for trace-rt.
import { readFileSync, existsSync } from "node:fs";
import { join, normalize, isAbsolute } from "node:path";

export const MAX_SPAN = 400;
// Citation/key-file cap. Raised 5->8: at 5 the cap was binding on every
// medium/deep trace (each maxed exactly 5 citations) while cursor cited 6-9,
// so it was a pure throttle on grounded coverage. Small-file slim runs stay
// tighter (few files = little to cite).
export const MAX_CODE_PASSAGES = 8;
export const SUBMIT_SCHEMA_NAME = "submit_trace";
export const SUBMIT_PROSE_SCHEMA_NAME = "submit_trace_prose";
export const SUBMIT_POINTER_SCHEMA_NAME = "submit_trace_pointer";

function needsComparison(question) {
  return /\b(vs|versus|compare|comparison|difference|contrast|differ)\b/i.test(String(question || ""));
}

function baseTraceProperties({ allowedPaths = [], maxPassages = 5, slim = false } = {}) {
  const filePathSchema = { type: "string" };
  if (allowedPaths.length) filePathSchema.enum = allowedPaths;
  if (!slim && allowedPaths.length) {
    filePathSchema.description = "Must be one of files read during explore.";
  }

  return {
    opening_summary: {
      type: "string",
      description: "Direct 3-5 sentence answer to the question: what the system does end to end and the specific mechanism that makes it work. Lead with the answer, not background.",
    },
    flow_steps: {
      type: "array",
      items: {
        type: "string",
        description: "One ordered pipeline step naming a concrete script/function/module and what it does, so the steps read as a real call path (not a generic stage label).",
      },
    },
    comparison_tables: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["title", "columns", "rows"],
        properties: {
          title: { type: "string" },
          columns: { type: "array", items: { type: "string" } },
          rows: { type: "array", items: { type: "array", items: { type: "string" } } },
        },
      },
    },
    sections: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["heading", "body"],
        properties: {
          heading: { type: "string", description: "The component or mechanism this section explains (a real script/module/function)." },
          body: { type: "string", description: "How this component works and why it matters to the answer — mechanism, control/data flow, key decision — grounded in the cited lines." },
        },
      },
    },
    key_files: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["path", "role"],
        properties: {
          path: { type: "string" },
          role: { type: "string", description: "What this file specifically contributes to the answer (not a generic one-liner)." },
        },
      },
    },
    code_passages: {
      type: "array",
      minItems: 1,
      maxItems: maxPassages,
      items: {
        type: "object",
        additionalProperties: false,
        required: ["file_path", "start_line", "end_line", "rationale"],
        properties: {
          file_path: filePathSchema,
          start_line: { type: "integer" },
          end_line: { type: "integer" },
          rationale: { type: "string" },
        },
      },
    },
    grounding_manifest: {
      type: "object",
      additionalProperties: false,
      required: ["files_read", "tool_turns"],
      properties: {
        files_read: { type: "array", items: { type: "string" } },
        tool_turns: { type: "integer" },
      },
    },
  };
}

export function traceProviderSchema({
  allowedCodePassagePaths = [],
  question = "",
  slim = false,
  filesReadCount = 0,
} = {}) {
  const allowedPaths = [...new Set(
    allowedCodePassagePaths.filter((p) => typeof p === "string" && p.trim().length > 0)
  )].sort();
  const maxPassages = slim && filesReadCount <= 4 ? 3 : MAX_CODE_PASSAGES;
  const required = [
    "opening_summary",
    "flow_steps",
    "comparison_tables",
    "sections",
    "key_files",
    "code_passages",
    "grounding_manifest",
  ];

  return {
    type: "object",
    additionalProperties: false,
    required,
    properties: baseTraceProperties({ allowedPaths, maxPassages, slim }),
  };
}

export function traceProseSchema({ question = "", slim = false } = {}) {
  const required = [
    "opening_summary",
    "flow_steps",
    "comparison_tables",
    "sections",
    "key_files",
  ];

  const props = baseTraceProperties({ slim });
  delete props.code_passages;
  delete props.grounding_manifest;

  return {
    type: "object",
    additionalProperties: false,
    required,
    properties: props,
  };
}

export function tracePointerSchema({
  question = "",
  slim = false,
  orderedPaths = [],
  maxCitations = MAX_CODE_PASSAGES,
} = {}) {
  const required = [
    "opening_summary",
    "flow_steps",
    "comparison_tables",
    "sections",
    "key_files",
    "citation_spans",
  ];

  const props = baseTraceProperties({ slim });
  delete props.code_passages;
  delete props.grounding_manifest;

  const maxIndex = Math.max(0, orderedPaths.length - 1);
  const indexSchema = { type: "integer", minimum: 0 };
  if (orderedPaths.length) indexSchema.maximum = maxIndex;

  props.citation_spans = {
    type: "array",
    minItems: 1,
    maxItems: slim && orderedPaths.length <= 4 ? Math.min(3, maxCitations) : maxCitations,
    items: {
      type: "object",
      additionalProperties: false,
      required: ["excerpt_index", "start_line", "end_line", "rationale"],
      properties: {
        excerpt_index: indexSchema,
        start_line: { type: "integer", minimum: 1 },
        end_line: { type: "integer", minimum: 1 },
        rationale: { type: "string", description: "What this span proves about the answer (the behavior it implements), not a restatement of the file name." },
      },
    },
  };

  return {
    type: "object",
    additionalProperties: false,
    required,
    properties: props,
  };
}

export function safeRelPath(repo, rel) {
  if (typeof rel !== "string") return null;
  const trimmed = rel.trim();
  if (!trimmed || isAbsolute(trimmed)) return null;
  const norm = normalize(trimmed);
  if (norm === "." || norm.startsWith("..")) return null;
  const root = normalize(repo);
  const abs = normalize(join(root, norm));
  if (!abs.startsWith(root + "/") && abs !== root) return null;
  return norm;
}

export function normalizeReadPath(repo, pathValue) {
  return safeRelPath(repo, pathValue);
}

function isNonEmptyString(v) {
  return typeof v === "string" && v.trim().length > 0;
}

function isInt(v) {
  return Number.isInteger(v) && !Number.isNaN(v);
}

export function validateTraceObject(obj, { workspace, filesRead, toolTurns, question = "" }) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return "result is not an object";
  if (!isNonEmptyString(obj.opening_summary)) return "opening_summary missing";
  if (!Array.isArray(obj.flow_steps)) return "flow_steps is not an array";
  if (obj.flow_steps.length === 0) return "flow_steps is empty";
  if (!Array.isArray(obj.comparison_tables)) obj.comparison_tables = [];
  if (!Array.isArray(obj.sections)) return "sections is not an array";
  if (!Array.isArray(obj.key_files)) return "key_files is not an array";
  if (!Array.isArray(obj.code_passages)) return "code_passages is not an array";
  if (!obj.grounding_manifest || typeof obj.grounding_manifest !== "object") {
    return "grounding_manifest missing";
  }

  for (const [i, step] of obj.flow_steps.entries()) {
    if (!isNonEmptyString(step)) return `flow_steps[${i}] invalid`;
  }

  for (const [i, table] of obj.comparison_tables.entries()) {
    if (!table || typeof table !== "object") return `comparison_tables[${i}] invalid`;
    if (!isNonEmptyString(table.title)) return `comparison_tables[${i}].title missing`;
    if (!Array.isArray(table.columns) || table.columns.length === 0) {
      return `comparison_tables[${i}].columns missing`;
    }
    if (!Array.isArray(table.rows)) return `comparison_tables[${i}].rows missing`;
  }

  for (const [i, sec] of obj.sections.entries()) {
    if (!sec || typeof sec !== "object") return `sections[${i}] invalid`;
    if (!isNonEmptyString(sec.heading)) return `sections[${i}].heading missing`;
    if (!isNonEmptyString(sec.body)) return `sections[${i}].body missing`;
  }

  for (const [i, kf] of obj.key_files.entries()) {
    if (!kf || typeof kf !== "object") return `key_files[${i}] invalid`;
    if (!isNonEmptyString(kf.path)) return `key_files[${i}].path missing`;
    if (!isNonEmptyString(kf.role)) return `key_files[${i}].role missing`;
  }

  const readSet = new Set([...(filesRead || [])].map((p) => normalizeReadPath(workspace, p)).filter(Boolean));

  for (const [i, p] of obj.code_passages.entries()) {
    if (!p || typeof p !== "object") return `code_passages[${i}] invalid`;
    const rel = safeRelPath(workspace, p.file_path);
    if (!rel) return `code_passages[${i}].file_path invalid: ${p.file_path}`;
    if (!isInt(p.start_line) || !isInt(p.end_line)) return `code_passages[${i}] line range invalid`;
    if (p.start_line < 1 || p.end_line < p.start_line) return `code_passages[${i}] line order invalid`;
    if (p.end_line - p.start_line + 1 > MAX_SPAN) return `code_passages[${i}] span too large`;
    if (!readSet.has(rel)) return `code_passages[${i}] file not read during explore: ${rel}`;
    const filePath = join(workspace, rel);
    if (!existsSync(filePath)) return `code_passages[${i}] file missing on disk: ${rel}`;
    const lines = readFileSync(filePath, "utf8").split("\n");
    if (p.end_line > lines.length) return `code_passages[${i}] end_line past EOF: ${rel}`;
    if (!isNonEmptyString(p.rationale)) return `code_passages[${i}].rationale missing`;
  }

  if (obj.code_passages.length === 0) return "code_passages is empty after grounding";

  const manifestFiles = obj.grounding_manifest.files_read;
  if (!Array.isArray(manifestFiles)) return "grounding_manifest.files_read missing";
  if (!isInt(obj.grounding_manifest.tool_turns)) return "grounding_manifest.tool_turns invalid";
  if (typeof toolTurns === "number" && obj.grounding_manifest.tool_turns !== toolTurns) {
    return "grounding_manifest.tool_turns mismatch";
  }

  return null;
}

export function applyGroundingManifest(obj, filesRead, toolTurns) {
  const out = { ...obj };
  out.grounding_manifest = {
    files_read: [...filesRead].sort(),
    tool_turns: toolTurns,
  };
  return out;
}
