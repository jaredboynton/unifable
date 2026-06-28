// map-ast-extract.mjs — ast-grep signature extraction for map prefetch (AST-only langs).
//
// Env:
//   UNITRACE_MAP_AST=auto|1|0   auto = ast for langs without regex extractors (default: auto)
//   UNITRACE_MAP_AST_MAX_FILES  cap for UNITRACE_MAP_AST=1 (default: 2000)
//   UNITRACE_AST_SKIP_INSTALL   skip ast-grep auto-install

import fs from "node:fs";
import { spawnSync } from "node:child_process";
import {
  LANG_PATTERNS,
  detectAstBinary,
  ensureAstTool,
  langForPath,
} from "./ast-context.mjs";

/** Map-specific patterns extending ast-context (public modifiers, Ruby module methods). */
const MAP_LANG_PATTERNS = {
  ...LANG_PATTERNS,
  java: [
    "public class $NAME $$$ { $$$ }",
    "class $NAME $$$ { $$$ }",
    "public void $NAME($$$) { $$$ }",
    "private void $NAME($$$) { $$$ }",
    "protected void $NAME($$$) { $$$ }",
    "public $TYPE $NAME($$$) { $$$ }",
    "$MOD $NAME($$$) { $$$ }",
  ],
  ruby: [
    "module $NAME",
    "class $NAME",
    "def self.$NAME($$$)",
    "def $NAME $$$",
  ],
};

export const REGEX_EXTS = new Set([
  ".py",
  ".js",
  ".mjs",
  ".cjs",
  ".jsx",
  ".ts",
  ".tsx",
  ".go",
  ".rs",
  ".sh",
  ".bash",
]);

export const AST_MAP_LANGS = new Set([
  "java",
  "kotlin",
  "ruby",
  "c",
  "cpp",
  "csharp",
  "swift",
  "lua",
  "php",
]);

const sigCache = new Map();
let astToolReady = false;

export function mapAstMode() {
  return process.env.UNITRACE_MAP_AST ?? "auto";
}

export function mapAstMaxFiles() {
  const n = Number(process.env.UNITRACE_MAP_AST_MAX_FILES ?? 2000);
  return Number.isFinite(n) && n > 0 ? n : 2000;
}

const SKIP_NAMES = new Set(["self", "this", "true", "false", "null", "undefined", "None"]);

function validIdent(name) {
  return typeof name === "string" && /^[A-Za-z_$][\w$]*$/.test(name) && !SKIP_NAMES.has(name);
}

function dedupeSigs(sigs) {
  sigs.sort((a, b) => a.line - b.line || a.name.localeCompare(b.name));
  const seen = new Set();
  return sigs.filter((s) => {
    const key = `${s.line}:${s.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function parseSignatureStream(stdout, lang) {
  const sigs = [];
  for (const line of stdout.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const row = JSON.parse(line);
      const name = row?.metaVariables?.single?.NAME?.text;
      const start = row?.range?.start?.line;
      if (!validIdent(name) || typeof start !== "number") continue;
      sigs.push({ name, line: start + 1, kind: "def", lang });
    } catch {
      /* skip malformed */
    }
  }
  return sigs;
}

function runAstSignaturePatterns(binary, absPath, lang) {
  const patterns = MAP_LANG_PATTERNS[lang];
  if (!patterns?.length) return [];
  const sigs = [];
  for (const pattern of patterns) {
    const res = spawnSync(binary, ["run", "--lang", lang, "-p", pattern, absPath, "--json=stream"], {
      encoding: "utf8",
      maxBuffer: 8 * 1024 * 1024,
    });
    if (res.status !== 0 && !res.stdout) continue;
    sigs.push(...parseSignatureStream(res.stdout || "", lang));
  }
  return dedupeSigs(sigs);
}

export function shouldUseAstForFile(relPath, { fileCount = 0 } = {}) {
  const mode = mapAstMode();
  if (mode === "0") return false;

  const ext = relPath.includes(".") ? relPath.slice(relPath.lastIndexOf(".")).toLowerCase() : "";
  const lang = langForPath(relPath);
  if (!lang || !MAP_LANG_PATTERNS[lang]) return false;

  if (mode === "1") {
    if (fileCount > mapAstMaxFiles()) return AST_MAP_LANGS.has(lang);
    return true;
  }

  if (REGEX_EXTS.has(ext)) return false;
  return AST_MAP_LANGS.has(lang);
}

function ensureMapAstTool() {
  if (astToolReady) return detectAstBinary();
  const install = process.env.UNITRACE_AST_SKIP_INSTALL !== "1";
  ensureAstTool({ install });
  astToolReady = true;
  return detectAstBinary();
}

export function extractAstSignatures(absPath, { binary = undefined, lang = null } = {}) {
  const resolvedLang = lang || langForPath(absPath);
  if (!resolvedLang || !MAP_LANG_PATTERNS[resolvedLang]) return [];

  const bin = binary !== undefined ? binary : ensureMapAstTool();
  if (!bin) return [];

  let mtime = 0;
  try {
    mtime = fs.statSync(absPath).mtimeMs;
  } catch {
    return [];
  }

  const key = `${absPath}:${mtime}:${resolvedLang}:sigs`;
  if (sigCache.has(key)) return sigCache.get(key);

  const sigs = runAstSignaturePatterns(bin, absPath, resolvedLang);
  sigCache.set(key, sigs);
  return sigs;
}

export function clearMapAstCache() {
  sigCache.clear();
  astToolReady = false;
}
