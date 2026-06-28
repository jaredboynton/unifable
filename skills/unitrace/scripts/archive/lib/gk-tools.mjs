// Native read-only explore tools for grok-trace (unchanged from pre-exec rt-tools).
import { toolRead, toolGrep, toolLs, toolCodebaseSearch, toolBatchRead } from "./htools.mjs";

const BATCH_READ_TOOL = {
  type: "function",
  name: "batch_read",
  description:
    "Read MULTIPLE files in ONE call. Pass every path you need as paths; returns each file under a ===== path ===== header. Prefer this over repeated read_file when you know several paths.",
  parameters: {
    type: "object",
    properties: {
      paths: {
        type: "array",
        items: { type: "string" },
        description: "Workspace-relative file paths to read together (max 60)",
      },
    },
    required: ["paths"],
    additionalProperties: false,
  },
};

const BASE_TOOL_SCHEMAS = [
  {
    type: "function",
    name: "read_file",
    description: "Read a file from the workspace. Returns content with line numbers implied by newlines.",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Workspace-relative or absolute file path" },
      },
      required: ["path"],
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "grep",
    description: "Search file contents with ripgrep (rg). Returns matching lines with file paths and line numbers.",
    parameters: {
      type: "object",
      properties: {
        pattern: { type: "string", description: "Regex pattern to search for" },
        path: { type: "string", description: "Optional subdirectory or file to search within" },
        glob: { type: "string", description: "Optional glob filter (e.g. '*.py')" },
        case_insensitive: { type: "boolean", description: "Case insensitive search" },
      },
      required: ["pattern"],
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "list_dir",
    description: "List files and subdirectories in a workspace directory (non-recursive).",
    parameters: {
      type: "object",
      properties: {
        path: { type: "string", description: "Directory path; defaults to workspace root" },
      },
      additionalProperties: false,
    },
  },
  {
    type: "function",
    name: "codebase_search",
    description: "Semantic codebase search via Cerebras + ripgrep. Requires CEREBRAS_API_KEY.",
    parameters: {
      type: "object",
      properties: {
        query: { type: "string", description: "Natural language search query" },
        target_directories: {
          type: "array",
          items: { type: "string" },
          description: "Optional directories to scope the search",
        },
      },
      required: ["query"],
      additionalProperties: false,
    },
  },
];

export function buildGrokToolSchemas() {
  if (process.env.UNITRACE_GK_BATCH_READ === "0") return [...BASE_TOOL_SCHEMAS];
  return [BATCH_READ_TOOL, ...BASE_TOOL_SCHEMAS];
}

export const TOOL_SCHEMAS = buildGrokToolSchemas();

export function dispatchTool(name, args, workspace) {
  const a = args && typeof args === "object" ? args : {};
  switch (name) {
    case "batch_read": {
      const paths = Array.isArray(a.paths) ? a.paths : [];
      const r = toolBatchRead(workspace, paths);
      return r.ok ? { ok: true, text: r.text, count: r.count } : { ok: false, error: r.reason };
    }
    case "read_file": {
      const r = toolRead(workspace, a.path);
      return r.ok
        ? { ok: true, path: r.path, content: r.content, total_lines: r.totalLines, file_size: r.fileSize, truncated: r.truncated }
        : { ok: false, error: r.reason };
    }
    case "grep": {
      const r = toolGrep(workspace, a);
      return r.ok
        ? { ok: true, pattern: r.pattern, path: r.path, file_matches: r.fileMatches, client_truncated: r.clientTruncated }
        : { ok: false, error: r.reason };
    }
    case "list_dir": {
      const r = toolLs(workspace, a.path);
      return r.ok ? { ok: true, path: r.absPath, dirs: r.dirs, files: r.files } : { ok: false, error: r.reason };
    }
    case "codebase_search": {
      if (!process.env.CEREBRAS_API_KEY) {
        return { ok: false, error: "codebase_search unavailable: CEREBRAS_API_KEY not set" };
      }
      const dirs = Array.isArray(a.target_directories) ? a.target_directories : [];
      const r = toolCodebaseSearch(workspace, String(a.query || ""), dirs);
      return r.ok ? { ok: true, text: r.text } : { ok: false, error: r.reason };
    }
    default:
      return { ok: false, error: `unknown tool: ${name}` };
  }
}

export function parseArguments(raw) {
  if (raw == null || raw === "") return {};
  if (typeof raw === "object") return raw;
  try {
    return JSON.parse(String(raw));
  } catch {
    return {};
  }
}
