#!/usr/bin/env node
// rehydrate-explore-wire.mjs — convert explore wire plaintext to judge-friendly markdown.

import fs from "node:fs";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  FILE_TOKEN_RE,
  QUOTE_TOKEN_RE,
  URL_TOKEN_RE,
  parseExploreWire,
} from "./explore-wire-format.mjs";
import { MAX_SPAN, safeRelPath } from "./trace-schema.mjs";

function fenceFor(code) {
  let longest = 0;
  for (const m of code.matchAll(/`+/g)) longest = Math.max(longest, m[0].length);
  return "`".repeat(Math.max(3, longest + 1));
}

function sectionHeading(name) {
  const spaced = name.replace(/([a-z])([A-Z])/g, "$1 $2");
  return `## ${spaced}`;
}

function hydrateFileToken(repo, pathValue, startLine, endLine, refIndex) {
  const rel = safeRelPath(repo, pathValue);
  const ref = `<ref${refIndex}>`;
  if (!rel) return `\n${ref} invalid file token: ${pathValue}:${startLine}-${endLine}\n`;

  let lines;
  try {
    lines = readFileSync(`${repo}/${rel}`, "utf8").split("\n");
  } catch {
    return `\n${ref} could not read \`${rel}:${startLine}-${endLine}\`\n`;
  }

  let s = Math.max(1, Math.min(startLine, lines.length));
  let e = Math.max(s, Math.min(endLine, lines.length));
  if (e - s + 1 > MAX_SPAN) {
    return `\n${ref} span too large: ${rel}:${s}-${e}\n`;
  }

  const code = lines.slice(s - 1, e).join("\n");
  const fence = fenceFor(code);
  return `\n${ref}\n${fence}${s}:${e}:${rel}\n${code}\n${fence}\n`;
}

function replaceFileTokens(body, repo, refStart = 1) {
  let refIndex = refStart;
  return body.replace(new RegExp(FILE_TOKEN_RE.source, "g"), (_raw, path, start, end) => {
    const block = hydrateFileToken(repo, path, Number(start), Number(end), refIndex);
    refIndex += 1;
    return block.trimEnd();
  });
}

function replaceUrlTokens(body) {
  return body.replace(new RegExp(URL_TOKEN_RE.source, "g"), (_raw, url) => url);
}

function replaceQuoteTokens(body) {
  return body.replace(new RegExp(QUOTE_TOKEN_RE.source, "g"), (_raw, url, excerpt) => {
    const text = excerpt.trim();
    return `\n- Source: ${url}\n\n> ${text}\n`;
  });
}

function hydrateSectionBody(body, repo, refStart) {
  let out = replaceQuoteTokens(body);
  out = replaceUrlTokens(out);
  out = replaceFileTokens(out, repo, refStart);
  return out.trim();
}

export function rehydrateTraceWire(text, workspace) {
  const parsed = parseExploreWire(text);
  const out = [];
  let refIndex = 1;

  for (const section of parsed.sections) {
    if (section.name === "_preamble") {
      if (section.body) out.push(section.body, "");
      continue;
    }
    out.push(sectionHeading(section.name), "");
    const body = hydrateSectionBody(section.body, workspace, refIndex);
    refIndex += section.fileTokens.length;
    out.push(body, "");
  }

  if (!parsed.sections.some((s) => s.name !== "_preamble")) {
    out.push(hydrateSectionBody(text, workspace, 1));
  }

  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
}

export function rehydrateWebsearchWire(text) {
  const parsed = parseExploreWire(text);
  const out = [];

  for (const section of parsed.sections) {
    if (section.name === "_preamble") {
      if (section.body) out.push(section.body, "");
      continue;
    }
    out.push(sectionHeading(section.name), "");
    let body = section.body;
    body = replaceQuoteTokens(body);
    body = replaceUrlTokens(body);
    out.push(body.trim(), "");
  }

  if (!parsed.sections.some((s) => s.name !== "_preamble")) {
    let body = text;
    body = replaceQuoteTokens(body);
    body = replaceUrlTokens(body);
    out.push(body.trim());
  }

  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim() + "\n";
}

export function rehydrateExploreWire(text, { mode = "trace", workspace = process.cwd() } = {}) {
  if (mode === "websearch") return rehydrateWebsearchWire(text);
  return rehydrateTraceWire(text, workspace);
}

function parseArgs(argv) {
  const args = { mode: "trace", workspace: process.cwd(), file: null, help: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === "--mode" && argv[i + 1]) args.mode = argv[++i];
    else if (arg.startsWith("--mode=")) args.mode = arg.slice(7);
    else if (arg === "--workspace" && argv[i + 1]) args.workspace = argv[++i];
    else if (arg.startsWith("--workspace=")) args.workspace = arg.slice(12);
    else if (arg === "--file" && argv[i + 1]) args.file = argv[++i];
    else if (arg.startsWith("--file=")) args.file = arg.slice(7);
    else if (arg === "--help" || arg === "-h") args.help = true;
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stderr.write(
      "usage: rehydrate-explore-wire.mjs --mode trace|websearch --workspace ROOT [--file PATH]\n",
    );
    process.exit(0);
  }
  const input = args.file ? fs.readFileSync(args.file, "utf8") : fs.readFileSync(0, "utf8");
  const out = rehydrateExploreWire(input, { mode: args.mode, workspace: args.workspace });
  process.stdout.write(out);
}

const isMain = process.argv[1] === fileURLToPath(import.meta.url);
if (isMain) main();
