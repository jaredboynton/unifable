// explore-output-prompt.mjs — shared wire output rules for trace and websearch prompts.

import { fileURLToPath } from "node:url";

export function traceGkWireSubmitRules() {
  return `Submit format (strict — wire plaintext only):
- Return wire plaintext only (not JSON). Do NOT use markdown, mermaid, or fenced code.
- Required SECTION blocks:
  SECTION Overview
  SECTION Flow
  SECTION KeyFiles
  SECTION CodeReferences
- Cite code with <file:repo/relative/path.ext:start-end> tokens only.
- file paths MUST be copied exactly from FILES READ DURING EXPLORE (at most 5 file tokens, spans <= 40 lines each).
- Repo-map, grep-only, list_dir-only, and search-only paths are context — do not cite them.
- Ground every claim in tool output, not memory.`;
}

export function websearchPointerSubmitRules() {
  return `Submit format (strict — structured function call only):
- Call submit_websearch_pointer once with these prose fields:
  executive_summary, in_scope_findings, adjacent_out_of_scope, prior_art, gaps_risks, recommended_next_steps
- Cite sources ONLY via citation_refs array:
  { url_index, excerpt_index, rationale }
- url_index MUST be from FETCH INDEX (0..N-1). excerpt_index MUST exist for that URL.
- Citation coverage is mandatory, not optional: cite EVERY distinct source in the FETCH INDEX that is even loosely relevant to the goal. When the FETCH INDEX holds many sources, your citation_refs SHOULD approach that count — a report citing only a handful out of a large index is incomplete and will be rejected. Aim for at least 10-12 distinct url_index values when that many relevant sources exist; never cite fewer than the number of clearly on-topic sources available.
- Distribute citations across findings (in_scope_findings, prior_art, gaps_risks) so the breadth of evidence is visible — do not cluster every citation on one claim.
- Do NOT paste raw https:// URLs anywhere in prose fields.
- Do NOT paste long quotes in prose — host rehydrates excerpts from excerpt_index.
- Apply scope discipline: In scope | Adjacent | Out of scope verdicts in prose.
- recommended_next_steps: only in-scope items that passed scope discipline.`;
}

export function websearchWireOutputRules() {
  return `Output format (strict — wire plaintext only):
- Plaintext only. Do NOT use markdown: no # or ### headings, no fenced code blocks, no mermaid, no markdown tables.
- Required SECTION blocks (PascalCase names exactly):
  SECTION ExecutiveSummary
  SECTION InScopeFindings
  SECTION AdjacentOutOfScope
  SECTION PriorArt
  SECTION GapsRisks
  SECTION RecommendedNextSteps
- Cite sources with URL tokens:
  <url:https://example.com/path>
- For verified excerpts (max 500 chars, single line), use quote tokens:
  <quote:https://example.com/path|Short verified excerpt from the page>
- Do not embed full page HTML or long bodies in tokens.
- Under SECTION RecommendedNextSteps, only in-scope items that passed scope discipline above.`;
}

export function isWireFormatEnabled(env = process.env) {
  return env.EXPLORE_WIRE_FORMAT === "1" || env.EXPLORE_WIRE_FORMAT === "true";
}

function parseArgs(argv) {
  const args = { kind: null, help: false };
  for (const arg of argv) {
    if (arg === "--gk-submit") args.kind = "gk-submit";
    else if (arg === "--websearch") args.kind = "websearch";
    else if (arg === "--ws-pointer-submit") args.kind = "ws-pointer-submit";
    else if (arg === "--help" || arg === "-h") args.help = true;
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help || !args.kind) {
    process.stderr.write(
      "usage: explore-output-prompt.mjs --gk-submit | --websearch | --ws-pointer-submit\n",
    );
    process.exit(args.help ? 0 : 2);
  }
  if (args.kind === "gk-submit") process.stdout.write(`${traceGkWireSubmitRules()}\n`);
  else if (args.kind === "ws-pointer-submit") process.stdout.write(`${websearchPointerSubmitRules()}\n`);
  else process.stdout.write(`${websearchWireOutputRules()}\n`);
}

const isMain = process.argv[1] === fileURLToPath(import.meta.url);
if (isMain) main();
