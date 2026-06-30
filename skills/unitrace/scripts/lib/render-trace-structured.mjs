// Render structured trace JSON to markdown with hydrated code passages.
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { MAX_SPAN, safeRelPath } from "./trace-schema.mjs";

function fenceFor(code) {
  let longest = 0;
  for (const m of code.matchAll(/`+/g)) longest = Math.max(longest, m[0].length);
  return "`".repeat(Math.max(3, longest + 1));
}

function renderTable(title, columns, rows) {
  const lines = [`### ${title}`, ""];
  lines.push(`| ${columns.join(" | ")} |`);
  lines.push(`| ${columns.map(() => "---").join(" | ")} |`);
  for (const row of rows) {
    lines.push(`| ${row.map((c) => String(c).replace(/\|/g, "\\|")).join(" | ")} |`);
  }
  lines.push("");
  return lines.join("\n");
}

function hydratePassage(repo, passage, index) {
  const rel = safeRelPath(repo, passage.file_path);
  const start = Number(passage.start_line);
  const end = Number(passage.end_line);
  const ref = `<ref${index + 1}>`;
  if (!rel || !start || !end) {
    return `\n${ref} invalid passage: ${JSON.stringify(passage)}`;
  }
  const lines = readFileSync(join(repo, rel), "utf8").split("\n");
  const s = Math.max(1, Math.min(start, lines.length));
  const e = Math.max(s, Math.min(end, lines.length));
  if (e - s + 1 > MAX_SPAN) {
    return `\n${ref} span too large: ${rel}:${s}-${e}`;
  }
  const code = lines.slice(s - 1, e).join("\n");
  const fence = fenceFor(code);
  const rationale = passage.rationale ? `\n_${passage.rationale}_\n` : "";
  return `\n${ref}${rationale}\n${fence}${s}:${e}:${rel}\n${code}\n${fence}`;
}

export function renderTraceStructured(repo, data) {
  const out = [];
  const summary = String(data.opening_summary || "").trim();
  if (summary) {
    out.push(summary, "");
  }

  const steps = Array.isArray(data.flow_steps) ? data.flow_steps : [];
  if (steps.length) {
    out.push("## Flow", "");
    for (const step of steps) out.push(`- ${step}`);
    out.push("");
  }

  const keyFiles = Array.isArray(data.key_files) ? data.key_files : [];
  if (keyFiles.length) {
    out.push("## Key files", "");
    out.push("| File | Role |");
    out.push("| --- | --- |");
    for (const kf of keyFiles) {
      out.push(`| ${kf.path} | ${String(kf.role).replace(/\|/g, "\\|")} |`);
    }
    out.push("");
  }

  const tables = Array.isArray(data.comparison_tables) ? data.comparison_tables : [];
  for (const table of tables) {
    if (table?.title && Array.isArray(table.columns) && Array.isArray(table.rows)) {
      out.push(renderTable(table.title, table.columns, table.rows));
    }
  }

  const sections = Array.isArray(data.sections) ? data.sections : [];
  for (const sec of sections) {
    if (!sec?.heading) continue;
    out.push(`## ${sec.heading}`, "", String(sec.body || "").trim(), "");
  }

  const passages = Array.isArray(data.code_passages) ? data.code_passages : [];
  if (passages.length) {
    out.push("## Code references");
    for (let i = 0; i < passages.length; i++) {
      out.push(hydratePassage(repo, passages[i], i));
    }
    out.push("");
  }

  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
}
