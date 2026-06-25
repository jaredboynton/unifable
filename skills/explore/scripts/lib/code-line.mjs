// code-line.mjs — shared classification of "preamble" (non-load-bearing) source
// lines. One definition used by BOTH the read pipeline (to hide these lines from
// the model so it can only cite real code) and the bench scorer (to mark a cited
// span grounded only when it overlaps a non-preamble line). Keeping them in sync
// is what makes "comments stripped from the model" imply "citations grounded".

// Single-line preamble test (no block-comment state): blank, shebang, line
// comment, block-comment fragment, "use strict", import/require, bare re-export
// list, and bare punctuation / closing braces.
export const PREAMBLE_RE = [
  /^\s*$/,
  /^\s*#!/,
  /^\s*(\/\/|#|;;|--)/,
  /^\s*(\/\*|\*\/|\*)/,
  /^\s*(['"])use strict\1;?\s*$/,
  /^\s*(import|from|require|using|use|#include|package|@import)\b/,
  /^\s*export\s*\{[^}]*\}\s*(from\b.*)?;?\s*$/,
  /^\s*[{}()[\];,]+\s*$/,
];

export function isPreambleLine(line) {
  return PREAMBLE_RE.some((re) => re.test(line));
}

// Per-language comment delimiters, used by the stateful hider to track
// multi-line block comments (which the single-line test cannot see).
export function commentSyntaxFor(p) {
  const ext = String(p || "").split(".").pop().toLowerCase();
  if (["js", "mjs", "cjs", "jsx", "ts", "tsx", "c", "cc", "cpp", "h", "hpp", "java", "go", "rs", "swift", "kt", "kts", "scala", "php", "dart"].includes(ext)) {
    return { block: [["/*", "*/"]] };
  }
  if (ext === "py") return { block: [['"""', '"""'], ["'''", "'''"]] };
  if (["sh", "bash", "zsh", "rb", "pl", "pm", "r", "yaml", "yml", "toml", "ini", "conf", "cfg", "mk"].includes(ext)) {
    return { block: [] };
  }
  if (["md", "markdown", "html", "htm", "xml", "vue", "svelte"].includes(ext)) {
    return { block: [["<!--", "-->"]] };
  }
  return { block: [["/*", "*/"]] };
}

// Stateful per-file classifier: returns true for any line that should be hidden
// from the model (full preamble set + multi-line block-comment bodies). Feed it
// every line of the read window in order so block state stays correct.
// Opens a multi-line `import { ... }` / `export { ... }` brace-list (no closing
// brace on the same line). Deliberately does NOT match `export function`/
// `export const`/`export class` — those are definitions, not name lists.
const IMPORT_BLOCK_OPEN_RE = /^(import|export)\s*\{[^}]*$/;

export function makeLineHider(p) {
  const { block } = commentSyntaxFor(p);
  let closing = null;   // inside a block comment: the awaited closing delimiter
  let inImport = false; // inside a multi-line import/export-from name list
  return (raw) => {
    const t = String(raw == null ? "" : raw);
    const tt = t.trim();
    if (closing) {
      const idx = tt.indexOf(closing);
      if (idx < 0) return true;
      const after = tt.slice(idx + closing.length).trim();
      closing = null;
      return after === "" ? true : isPreambleLine(after);
    }
    if (inImport) {
      // Continuation of a multi-line import; the brace-closing line ends it
      // (e.g. `} from "./x";`). Hide the whole list including that line.
      if (tt.includes("}")) inImport = false;
      return true;
    }
    for (const [open, close] of block) {
      if (tt.startsWith(open)) {
        const rest = tt.slice(open.length);
        const ci = rest.indexOf(close);
        if (ci >= 0) {
          const after = rest.slice(ci + close.length).trim();
          return after === "" ? true : isPreambleLine(after);
        }
        closing = close;
        return true;
      }
    }
    if (IMPORT_BLOCK_OPEN_RE.test(tt)) {
      inImport = true;
      return true;
    }
    return isPreambleLine(t);
  };
}
