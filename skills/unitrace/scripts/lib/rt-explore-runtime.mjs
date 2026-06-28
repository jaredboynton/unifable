// explore_exec sandbox: code_mode-style JS with nested read-only tools in parallel.
import { relative, resolve } from "node:path";
import {
  toolGrep,
  toolReadRange,
  toolLs,
  toolShell,
  confine,
} from "./htools.mjs";
import { preflightExploreExecCode } from "./rt-map-seed.mjs";

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const DEFAULT_EXEC_TIMEOUT_MS = Number(process.env.UNITRACE_RT_EXEC_TIMEOUT_MS) || 25_000;
const GREP_HIT_CAP = 30;
const LINE_PREVIEW_MAX = 120;
const READ_PREVIEW_MAX = 400;
const SHELL_PREVIEW_MAX = 2048;
// Hide comments / imports / blanks from the model's read view so it can only
// cite real code (grounded citations). Disable with UNITRACE_RT_STRIP_COMMENTS=0.
const STRIP_PREAMBLE = process.env.UNITRACE_RT_STRIP_COMMENTS !== "0";

function workspaceRel(root, path) {
  const abs = confine(root, path);
  if (!abs) return null;
  const rel = relative(resolve(root), abs);
  return rel && !rel.startsWith("..") ? rel : null;
}

function trimLine(s, max = LINE_PREVIEW_MAX) {
  const t = String(s || "");
  return t.length <= max ? t : `${t.slice(0, max)}…`;
}

function readPreview(content) {
  const lines = String(content || "").split("\n").filter(Boolean);
  if (!lines.length) return "";
  const head = lines.slice(0, 3);
  const tail = lines.length > 6 ? lines.slice(-3) : [];
  let preview = head.join("\n");
  if (tail.length) preview += `\n...\n${tail.join("\n")}`;
  return preview.length > READ_PREVIEW_MAX ? `${preview.slice(0, READ_PREVIEW_MAX)}…` : preview;
}

function flattenGrep(root, r) {
  const hits = [];
  const fileMatches = [];
  for (const fm of r.fileMatches || []) {
    const file = String(fm.file || "").replace(/^\.\//, "");
    const rel = workspaceRel(root, file) || file;
    const matches = [];
    for (const m of fm.matches || []) {
      if (hits.length >= GREP_HIT_CAP) break;
      const hit = {
        path: rel,
        lineNumber: m.lineNumber,
        content: trimLine(m.content),
      };
      hits.push(hit);
      matches.push({ lineNumber: m.lineNumber, content: hit.content });
    }
    if (matches.length) fileMatches.push({ file: rel, matches });
  }
  return {
    ok: true,
    hits,
    hitCount: hits.length,
    truncated: Boolean(r.clientTruncated) || hits.length >= GREP_HIT_CAP,
    fileMatches,
  };
}

function slimRead(r, rel) {
  const lines = String(r.content || "").split("\n").filter(Boolean);
  return {
    path: rel || r.path,
    start_line: r.start_line,
    end_line: r.end_line,
    line_count: lines.length,
    preview: readPreview(r.content),
    truncated: r.truncated,
  };
}

function summarizeForModel(value, depth = 0) {
  if (value == null) return value;
  if (typeof value === "string") {
    return value.length > READ_PREVIEW_MAX ? `${value.slice(0, READ_PREVIEW_MAX)}…` : value;
  }
  if (typeof value !== "object") return value;
  if (Array.isArray(value)) {
    const cap = depth === 0 ? 40 : 20;
    return value.slice(0, cap).map((v) => summarizeForModel(v, depth + 1));
  }
  const out = {};
  for (const [k, v] of Object.entries(value)) {
    out[k] = summarizeForModel(v, depth + 1);
  }
  return out;
}

function execResultMax() {
  const v = Number(process.env.UNITRACE_RT_EXEC_RESULT_MAX);
  return Number.isFinite(v) && v > 0 ? v : 32_000;
}

function capResult(value) {
  const summarized = summarizeForModel(value);
  let text;
  try {
    text = JSON.stringify(summarized);
  } catch {
    text = String(summarized);
  }
  const max = execResultMax();
  if (text.length <= max) return summarized;

  const pathsSeen = new Set();
  const walk = (v) => {
    if (!v || typeof v !== "object") return;
    if (Array.isArray(v)) return v.forEach(walk);
    if (typeof v.path === "string") pathsSeen.add(v.path);
    Object.values(v).forEach(walk);
  };
  walk(summarized);

  return {
    truncated: true,
    message: "explore_exec result exceeded size cap",
    hitCount: Array.isArray(summarized?.hits) ? summarized.hits.length : undefined,
    pathsSeen: [...pathsSeen].slice(0, 20),
    summary: text.slice(0, Math.min(max, 8000)),
  };
}

function execErrorHint(message) {
  if (/is not a function/i.test(String(message))) {
    return "tools.grep returns { hits: [{ path, lineNumber, content }] }; use hits.find(...) not grep(...).find(...)";
  }
  return undefined;
}

function buildTools(root, { onRead }) {
  const trackRead = (r) => {
    if (!r?.ok || !r.path) return r;
    const rel = workspaceRel(root, r.path);
    if (rel && onRead) onRead(rel, r.content || "");
    return r;
  };

  return Object.freeze({
    grep: async (args = {}) => {
      const r = toolGrep(root, {
        pattern: args.pattern,
        path: args.path,
        glob: args.glob,
        type: args.type,
        caseInsensitive: args.case_insensitive ?? args.caseInsensitive,
      });
      if (!r.ok) throw new Error(r.reason || "grep failed");
      return flattenGrep(root, r);
    },
    read: async (args = {}) => {
      if (!args.path) throw new Error("read: path required");
      const r = trackRead(toolReadRange(root, args.path, { ...args, stripPreamble: STRIP_PREAMBLE }));
      if (!r.ok) throw new Error(r.reason || "read failed");
      const rel = workspaceRel(root, r.path);
      return slimRead(r, rel || r.path);
    },
    batch_read: async (args = {}) => {
      const reads = Array.isArray(args.reads) ? args.reads : args.paths?.map((path) => ({ path }));
      if (!reads?.length) throw new Error("batch_read: reads array required");
      const paths = [];
      for (const entry of reads.slice(0, 60)) {
        const r = trackRead(toolReadRange(root, String(entry.path), { ...entry, stripPreamble: STRIP_PREAMBLE }));
        if (!r.ok) continue;
        const rel = workspaceRel(root, r.path) || r.path;
        const lines = String(r.content || "").split("\n").filter(Boolean);
        paths.push({
          path: rel,
          start_line: r.start_line,
          end_line: r.end_line,
          line_count: lines.length,
        });
      }
      return { count: paths.length, paths };
    },
    list_dir: async (args = {}) => {
      const r = toolLs(root, args.path);
      if (!r.ok) throw new Error(r.reason || "list_dir failed");
      return { path: args.path || ".", dirs: r.dirs, files: r.files };
    },
    shell: async (args = {}) => {
      if (!args.command?.trim()) throw new Error("shell: command required");
      const r = toolShell(root, {
        command: args.command,
        workingDirectory: args.working_directory ?? args.workingDirectory,
      });
      if (!r.ok) throw new Error(r.reason || r.rejected ? "shell rejected" : "shell failed");
      return {
        exitCode: r.exitCode,
        stdout_preview: trimLine(r.stdout, SHELL_PREVIEW_MAX),
        stderr_preview: trimLine(r.stderr, SHELL_PREVIEW_MAX),
      };
    },
  });
}

export async function runExploreExec(workspace, code, { deadlineMs, onRead } = {}) {
  if (!code || !String(code).trim()) return { ok: false, error: "explore_exec: empty code" };
  const preflight = preflightExploreExecCode(code);
  if (!preflight.ok) return preflight;
  const timeoutMs = deadlineMs && deadlineMs > 0
    ? Math.min(deadlineMs - Date.now(), DEFAULT_EXEC_TIMEOUT_MS)
    : DEFAULT_EXEC_TIMEOUT_MS;
  if (timeoutMs <= 0) return { ok: false, error: "explore_exec: deadline exceeded" };

  const tools = buildTools(workspace, { onRead });
  let fn;
  try {
    fn = new AsyncFunction("tools", `"use strict";\n${code}`);
  } catch (e) {
    return { ok: false, error: `explore_exec compile error: ${e.message}` };
  }

  let timer;
  try {
    const result = await Promise.race([
      fn(tools),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("explore_exec timed out")), timeoutMs);
      }),
    ]);
    return { ok: true, result: capResult(result) };
  } catch (e) {
    const error = e.message || String(e);
    const hint = execErrorHint(error);
    return hint ? { ok: false, error, hint } : { ok: false, error };
  } finally {
    if (timer) clearTimeout(timer);
  }
}
