// enhance-prompt.mjs -- repo-grounded "enhanced prompt" generator for the
// unifable UserPromptSubmit gate. Reads {prompt, cwd} on stdin, prints
// {ok, enhanced_prompt, cited_ranges} on stdout, exits 0 ALWAYS (fail-open;
// the Python hook treats any non-ok / failure as "use the static fallback").
//
// Tier (bench-decided 2026-06-27, /tmp/enhance-bench): Standard
//   retrieveCandidates seed -> 4 parallel gpt-realtime-mini navigators ->
//   term-hydrate -> ONE full gpt-realtime-2 synth (reasoning low + steer on user).
//   Nav's pruning to ~5-7 windows is what lets the full synth score q=9 on real repos; the lite
//   (no-nav) tier collapsed to q=3 on large repos without nav.
//
// Reuses the in-repo explore skill machinery (no copies):
//   ./search-fast.mjs        retrieveCandidates
//   ./lib/daemon-client.mjs  daemonAsk, daemonAskBatch, warmDaemonPool
//   ./lib/rt-explore-nav.mjs NAV_SCHEMA, dedupNavProposals
//
// Env:
//   UNIFABLE_PROMPT_ENHANCE_NAV        mini navigator count (default 4; 0=lite, 8=full)
//   UNIFABLE_PROMPT_ENHANCE_MODEL      synth model (default gpt-realtime-2)
//   UNIFABLE_PROMPT_ENHANCE_NAMESPACE  daemon pool namespace (default prompt-enhance)
//   UNITRACE_AST_SKIP_INSTALL           set to 1 to skip ast-grep network install
//                                      (line-window hydration fallback; bench-used)
//
// The rtinferd daemon pool is shared across all callers (search, enhance,
// judge); this namespace is distinct so it never contends with judge calls.

import { retrieveCandidates } from "./search-fast.mjs";
import { daemonAsk, daemonAskBatch, warmDaemonPool } from "./lib/daemon-client.mjs";
import { NAV_SCHEMA, dedupNavProposals } from "./lib/rt-explore-nav.mjs";
import { fileURLToPath } from "node:url";

const NAV_COUNT = Math.max(0, parseInt(process.env.UNIFABLE_PROMPT_ENHANCE_NAV || "4", 10) || 4);
const SYNTH_MODEL = (process.env.UNIFABLE_PROMPT_ENHANCE_MODEL || "gpt-realtime-2").trim();
const NAMESPACE = (process.env.UNIFABLE_PROMPT_ENHANCE_NAMESPACE || "prompt-enhance").trim();
const MINI = "gpt-realtime-mini";

// SYNTH_SYSTEM for the enhancer synthesis turn. Bench-validated 2026-06-27
// (docs/evals/prompt-enhance.md): the SOLE quality driver is the worked few-shot
// example -- it teaches the "Area N" decomposition + concrete path:line density,
// lifting quality 3.50 -> 4.00 (max 4). The extra decomposition/anti-patterns/
// output-format text in the fat variant earned nothing, so this stays LEAN.
// Realtime caches the prefix at ANY size (no 1024-token floor for the WS API), so
// padding to cross 1024 would only slow cold calls for zero gain. Every token here
// earns its place: the rules + one worked example.
export const SYNTH_SYSTEM = [
  "You rewrite a vague coding prompt into a grounded, actionable enhanced prompt using ONLY the provided code windows.",
  "Rules:",
  "- Map the user's vague ask onto the SPECIFIC files, symbols, and line areas to investigate. Name them concretely as path:line.",
  "- Do NOT restate the user's words -- they already have their prompt. Add the codebase grounding they lack.",
  "- Cite path:line ONLY from the provided windows. Never invent a path or line range not present in a window.",
  "- Do NOT emit repo-specific commands (no `npm test`, `pytest`, `cargo build`, `just test`, `make test`, `go test`, `yarn test`, `pnpm test`). If you mention verification, name the CATEGORY only (a test / typecheck / lint / build that exercises the change).",
  "- Be concise: decompose into 2-4 named investigation areas. Cap ~1200 chars.",
  "- If the windows contain nothing relevant to the ask, return enhanced_prompt=\"\" and cited_ranges=[].",
  "",
  "WORKED EXAMPLE (for voice and density -- do not copy its content):",
  'Vague ask: "our trace script keeps getting stuck and hanging, fix it"',
  "Windows provided: trace.sh:1-40 (the loop + PID check), trace-delegate.mjs:8-30 (the subprocess spawn + wait), lock.ts:5-18 (the lock file write).",
  'Ideal enhanced_prompt: "Investigate why the trace loop hangs and fails to release its lock. Area 1 (root-cause candidate): review trace.sh:18-32 -- the PID liveness check (`kill -0 $pid`) and the 600s age threshold; determine whether a stale PID or a sub-600s respawn leaves the lock held. Area 2: review trace-delegate.mjs:12-26 -- the `node $SCRIPT` spawn and its wait/timeout path; check for an unhandled rejection or missing timeout that lets the delegate hang without exiting. Area 3: review lock.ts:8-16 -- the lock acquisition and release; confirm release runs on every exit path (success, failure, signal). Propose a fix that guarantees lock release on every exit path and tightens stale-PID detection. Verify with a test that exercises the hung-subprocess exit path."',
  'cited_ranges: ["trace.sh:18-32", "trace-delegate.mjs:12-26", "lock.ts:8-16"]',
].join("\n");

const ENHANCE_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["enhanced_prompt", "cited_ranges"],
  properties: {
    enhanced_prompt: {
      type: "string",
      description: "Grounded, actionable enhanced prompt. Maps the vague ask onto concrete path:line areas to investigate. Empty string if windows are irrelevant.",
    },
    cited_ranges: {
      type: "array",
      maxItems: 8,
      items: { type: "string", description: "A path:line range cited in the enhanced prompt, taken from a provided window (e.g. 'lib/trace-delegate.mjs:8-22')." },
      description: "The path:line ranges cited in the enhanced prompt, each from a provided window.",
    },
  },
};

const FACETS = [
  "the primary entry point and the top-level control flow that answers the question",
  "the data structures, types, and state that flow through this code path",
  "the helper functions, callees, and imported modules the entry point depends on",
  "error handling, edge cases, fallbacks, and validation on this path",
];

const NAV_INSTRUCTIONS = [
  "You are one of several parallel codebase navigators operating in read-only mode.",
  "You are given a QUESTION, a FACET to focus on, and a READ INDEX of code already retrieved.",
  "Decide what ELSE must be read to answer the QUESTION from your facet, then return it via the navigate tool.",
  "Return grep_terms (symbols/identifiers to locate more code). Empty if nothing new to find.",
  "Only propose terms clearly relevant to the question; never invent paths.",
  "If the READ INDEX already covers your facet, return done:true with empty arrays.",
  "Be precise and minimal -- propose at most a few high-value terms, not a broad sweep.",
].join("\n");

function navPromptFor(question, indexText, facet) {
  return [
    "QUESTION:", question, "",
    `YOUR FACET: ${facet}`, "",
    "READ INDEX (code already retrieved this run):",
    indexText || "(nothing read yet)", "",
    "What else must be read to answer the QUESTION from your facet? Call navigate now.",
  ].join("\n");
}

function buildNavIndex(windows, maxFiles = 14, previewLines = 4) {
  const lines = ["READ INDEX:"];
  for (let i = 0; i < Math.min(windows.length, maxFiles); i++) {
    const w = windows[i];
    const preview = String(w.content || "").split("\n").slice(0, previewLines).map((l) => `  ${l}`).join("\n");
    lines.push(`[${i}] ${w.path} (lines ${w.startLine}-${w.endLine})`, preview, "");
  }
  return lines.join("\n");
}

function buildWindowsText(windows, maxChars = 16000) {
  const parts = ["CODE WINDOWS (retrieved from the repo, confirmed on disk; format path:start-end):"];
  let used = parts[0].length;
  for (const w of windows) {
    const block = `---\n${w.startLine}:${w.endLine}:${w.path}\n${w.content}`;
    if (used + block.length > maxChars) break;
    parts.push(block);
    used += block.length;
  }
  parts.push("---");
  return parts.join("\n");
}

function mergeWindows(into, candidates) {
  const seen = new Set(into.map((w) => w.path));
  for (const c of candidates || []) {
    if (seen.has(c.path)) continue;
    into.push({ path: c.path, startLine: c.startLine, endLine: c.endLine, content: c.content });
    seen.add(c.path);
  }
}

function parseRangePath(r) {
  const m = String(r).match(/^(.+?)(?::\d+(?:-\d+)?)?$/);
  return m ? m[1] : null;
}

// Drop any cited range whose path is not in the windows we actually saw --
// defense-in-depth against synth hallucination (the bench showed lite-full
// could invent paths from a 32-window pool; nav's focused pool avoided it,
// and this filter is the source-level backstop).
function filterCitedRanges(windows, cited) {
  const paths = new Set(windows.map((w) => w.path));
  return (cited || []).filter((r) => {
    const p = parseRangePath(r);
    return p && paths.has(p);
  });
}

async function run() {
  let input;
  try {
    const raw = await readStdin();
    input = JSON.parse(raw || "{}");
  } catch {
    return { ok: false };
  }
  const prompt = String(input.prompt || "").trim();
  const cwd = String(input.cwd || "").trim();
  if (!prompt || !cwd) return { ok: false };

  let windows = [];
  try {
    // Warm mini + full pools concurrently with the seed retrieve (off critical path).
    const [{ candidates }] = await Promise.all([
      retrieveCandidates(cwd, prompt),
      warmDaemonPool(NAMESPACE, 4, { model: SYNTH_MODEL }).catch(() => {}),
      NAV_COUNT > 0 ? warmDaemonPool(NAMESPACE, 4, { model: MINI }).catch(() => {}) : Promise.resolve(),
    ]);
    windows = (candidates || []).map((c) => ({ path: c.path, startLine: c.startLine, endLine: c.endLine, content: c.content }));

    // Nav fanout (mini navigators propose grep terms; host hydrates via one combined retrieve).
    if (NAV_COUNT > 0 && windows.length) {
      const indexText = buildNavIndex(windows);
      const requests = Array.from({ length: NAV_COUNT }, (_, i) => ({
        system: NAV_INSTRUCTIONS,
        user: navPromptFor(prompt, indexText, FACETS[i % FACETS.length]),
        schema: NAV_SCHEMA,
        schemaName: "navigate",
      }));
      const results = await daemonAskBatch(NAMESPACE, requests, { model: MINI });
      if (results) {
        const { terms } = dedupNavProposals(results);
        if (terms.length) {
          const more = await retrieveCandidates(cwd, terms.join(" "), { maxSpans: 8 });
          mergeWindows(windows, more.candidates || []);
        }
      }
    }

    if (!windows.length) return { ok: false };

    const synthUser = `USER ASK:\n${prompt}\n\n${buildWindowsText(windows)}\n\nWrite the enhanced prompt now (call the enhance tool).`;
    const obj = await daemonAsk(
      NAMESPACE,
      {
        system: SYNTH_SYSTEM,
        user: synthUser,
        schema: ENHANCE_SCHEMA,
        schemaName: "enhance",
        reasoningEffort: "low",
      },
      { model: SYNTH_MODEL },
    );
    if (!obj || typeof obj.enhanced_prompt !== "string" || !obj.enhanced_prompt.trim()) {
      return { ok: false };
    }
    const enhanced = obj.enhanced_prompt.slice(0, 1200);
    const cited = filterCitedRanges(windows, Array.isArray(obj.cited_ranges) ? obj.cited_ranges : []).slice(0, 8);
    return { ok: true, enhanced_prompt: enhanced, cited_ranges: cited };
  } catch {
    return { ok: false };
  }
}

function readStdin() {
  return new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (c) => (data += c));
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", () => resolve(""));
    // Some hosts provide a TTY-like stdin that never emits 'end'; bail after a beat.
    const t = setTimeout(() => { try { process.stdin.destroy(); } catch {} resolve(data); }, 20000);
    process.stdin.on("end", () => clearTimeout(t));
  });
}

const _isMain = process.argv[1] === fileURLToPath(import.meta.url);
if (_isMain) {
  run().then((out) => {
    try { process.stdout.write(JSON.stringify(out)); } catch {}
    process.exit(0);
  }).catch(() => {
    try { process.stdout.write(JSON.stringify({ ok: false })); } catch {}
    process.exit(0);
  });
}
