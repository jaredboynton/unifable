// websearch-lib.mjs — agy capture + research prompt for explore websearch.
//
// Mirrors unifusion run_gemini.sh: pseudo-TTY path A (script(1)), transcript
// JSONL path B, anti-empty guard. Zero npm dependencies; Node 18+.

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import path, { join } from "node:path";
import { fileURLToPath } from "node:url";
import { buildExploreSkillContext } from "./explore-skill-context.mjs";
import { isWireFormatEnabled, websearchWireOutputRules } from "./lib/explore-output-prompt.mjs";
import { buildRepoContext } from "./repo-context.mjs";

const DEFAULT_MODEL = "Gemini 3.5 Flash (Low)";
const DEFAULT_TIMEOUT_SEC = 600;
const BRAIN_DIR = join(homedir(), ".gemini/antigravity-cli/brain");
const DEFAULT_SKILL_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const SCOPE_DISCIPLINE = `Scope discipline (critical — prevents mis-scoped recommendations):
- Match the problem domain stated in the task. Do NOT recommend tools or approaches meant for a different primary problem unless the task explicitly targets that domain.
- Do NOT recommend heavy infrastructure (always-on daemons, embedding pipelines, mandatory graph indexes, large Docker stacks) when a lightweight path fits — only recommend heavy paths when the task justifies the integration cost and you explain when it pays off.
- Do NOT conflate adjacent domains. Label techniques as Adjacent or Out of scope when they solve a different primary problem than the task.
- Obscure, single-author, experimental, or lightly maintained repos are welcome — frontier research often lives there. Do not filter by stars, vendor, or maintenance cadence.
- Flag unverified marketing or paper claims (exact percentages, "+N% accuracy", "95% token reduction") as unverified unless you found independent reproduction or primary eval data. Prefer qualitative tradeoffs when stats are unsupported.
- Every recommended technique must include an explicit fit verdict: In scope | Adjacent (different problem) | Out of scope for this task — with one sentence why.`;

function resolveSkillContextEnabled(options) {
  if (options.skillContext === true) return true;
  if (options.skillContext === false) return false;
  return process.env.EXPLORE_WEBSEARCH_SKILL_CONTEXT === "1";
}

function buildConsumerContext({ workspace, skillContext, skillDir }) {
  const blocks = [];
  const repoContext = buildRepoContext(workspace);
  if (repoContext) blocks.push(repoContext);

  if (skillContext) {
    const exploreContext = buildExploreSkillContext(skillDir ?? DEFAULT_SKILL_DIR);
    if (exploreContext) blocks.push(exploreContext);
  }

  if (!blocks.length) return "";
  return `${blocks.join("\n\n")}\n\n`;
}

const AGY_RESEARCH_CONSTRAINTS = `- Search via Exa MCP only (web_search_exa / Exa search tools). Do NOT use built-in web_search or other non-Exa search tools.
- Speed: fire multiple Exa search queries in parallel (different angles/phrasings) before reading anything.
- Two-phase workflow: (1) run parallel Exa searches and collect every promising URL from the results; (2) batch-fetch all collected URLs at once with parallel Exa fetch calls (web_fetch_exa). Do not fetch URLs one-by-one interleaved with new searches.`;

const RT_SEARCH_CONSTRAINTS = `- Call exa_search only in Round 1. Fire multiple parallel exa_search calls with varied query phrasings before any fetching.
- Do NOT fetch URLs in this round. Do NOT write the final report.`;

const RT_WEB_RUN_CONSTRAINTS = `- Call web_run ONCE in Round 1 with 4-8 search_query entries batched in that single call.
- Vary phrasings: official docs, specs, reference implementations, tutorials, site: queries.
- Do NOT emit multiple web_run calls — the host coalesces queries, but one batched call is fastest.
- Do NOT write the final report in this round.`;

const RT_FETCH_CONSTRAINTS = `- Call exa_fetch only in Round 2 with url_indices from the SEARCH CATALOG.
- Batch multiple url_indices in one exa_fetch call. Do NOT pass raw URLs. Do NOT search again.`;

export function buildWebsearchWebRunPrompt(
  goal,
  {
    workspace = process.env.EXPLORE_WORKSPACE || process.cwd(),
    skillContext = resolveSkillContextEnabled({}),
    skillDir,
  } = {},
) {
  const consumerContext = buildConsumerContext({ workspace, skillContext, skillDir });
  return `You are an external research agent in the search phase.

${consumerContext}${RT_WEB_RUN_CONSTRAINTS}

${SCOPE_DISCIPLINE}

Requirements:
- Call web_run once with 4-8 batched search_query entries covering different angles on the goal.
- Prefer official docs, specs, and reference implementations.
- Collect source URLs from search results — synthesis happens in the next round.

GOAL:
${goal}`;
}

export function buildWebsearchSearchPrompt(
  goal,
  {
    workspace = process.env.EXPLORE_WORKSPACE || process.cwd(),
    skillContext = resolveSkillContextEnabled({}),
    skillDir,
  } = {},
) {
  const consumerContext = buildConsumerContext({ workspace, skillContext, skillDir });
  return `You are an external research agent in the search phase.

${consumerContext}${RT_SEARCH_CONSTRAINTS}

${SCOPE_DISCIPLINE}

Requirements:
- Call exa_search immediately with parallel queries covering different angles on the goal.
- Collect catalog indices for promising URLs — fetching happens in the next round.

GOAL:
${goal}`;
}

export function buildWebsearchFetchPrompt(
  goal,
  {
    workspace = process.env.EXPLORE_WORKSPACE || process.cwd(),
    skillContext = resolveSkillContextEnabled({}),
    skillDir,
  } = {},
) {
  const consumerContext = buildConsumerContext({ workspace, skillContext, skillDir });
  return `You are an external research agent in the fetch phase.

${consumerContext}${RT_FETCH_CONSTRAINTS}

${SCOPE_DISCIPLINE}

Requirements:
- Select promising catalog indices and batch-fetch with exa_fetch.
- Prefer official docs, specs, and reference implementations for the goal below.

GOAL:
${goal}`;
}

export function buildWebsearchExplorePrompt(goal, options = {}) {
  return buildWebsearchSearchPrompt(goal, options);
}

export function buildWebsearchSubmitPacket({
  goal,
  submitInstructions = "",
  fetchIndex = "",
  maxChars = 45_000,
} = {}) {
  const lines = [
    "Round 3: synthesize the final external research report.",
    `Call ${"submit_websearch_pointer"} once with prose fields and citation_refs only.`,
    "",
    `GOAL: ${goal}`,
    "",
    "Rules:",
    "- Do NOT include raw URLs or long quotes in section prose.",
    "- Cite exclusively via citation_refs with url_index and excerpt_index from FETCH INDEX.",
    "- Include citation_refs for every fetched source that supports a claim (no arbitrary cap).",
    "",
    fetchIndex || "FETCH INDEX: (empty)",
  ];

  if (submitInstructions.trim()) {
    lines.push("", submitInstructions.trim());
  }

  let text = lines.join("\n");
  if (text.length > maxChars) {
    text = `${text.slice(0, maxChars)}\n... [submit packet truncated]`;
  }
  return text;
}
export function buildWebsearchPrompt(
  goal,
  {
    workspace = process.env.EXPLORE_WORKSPACE || process.cwd(),
    skillContext = resolveSkillContextEnabled({}),
    skillDir,
    wire = isWireFormatEnabled(),
    backend = "agy",
  } = {},
) {
  const consumerContext = buildConsumerContext({ workspace, skillContext, skillDir });
  const researchConstraints = backend === "rt" ? RT_SEARCH_CONSTRAINTS : AGY_RESEARCH_CONSTRAINTS;
  const outputBlock = wire
    ? websearchWireOutputRules()
    : `Suggested structure:
1. Executive summary (2-4 sentences; state what problem domain you optimized for)
2. In-scope findings (bullet list; each claim cites evidence and includes Fit: In scope + integration cost: low|medium|high)
3. Adjacent / out-of-scope (techniques worth knowing but wrong default for this task — say why)
4. Prior art / GitHub repos (name, link, why it matters; obscure repos encouraged)
5. Gaps, risks, or conflicting claims (include limits of static analysis, index staleness, unverified stats)
6. Recommended next steps (concrete, reproducible; only in-scope items; ordered by ROI)`;

  return `You are an external research agent. Given the task below, extensively search for:
- official documentation, specs, and release notes
- research papers, preprints, and technical reports
- prior art on GitHub and open-source implementations (including obscure, niche, single-author, and experimental repos)
- credible engineering write-ups from authors or maintainers

${consumerContext}Research constraints:
${researchConstraints}
- Keep research recent: prefer sources from the last 12-24 months when the topic moves fast; note when older canonical references remain authoritative.
- Focus on frontier but viable approaches — cutting-edge techniques that are reproducible with documented tooling, data, and eval protocols.
- Favor reproducible methods: explicit versions, datasets, benchmarks, and links to runnable code when available.
- Prioritize low-integration-cost wins (validators, benchmarks, optional MCP add-ons) before architectural rewrites.

${SCOPE_DISCIPLINE}

Output rules (strict):
- Do not narrate your steps or tool calls. Perform all searching/reading silently.
- Only print responses when necessary.
- Output only final findings, cited evidence references (URLs, paper titles/DOIs, repo links with paths when useful).
- Do not recommend something as a next step unless it passed scope discipline above.

${outputBlock}

Task / goal:
${goal}`;
}

export function hasContent(text) {
  return /\S/.test(text || "");
}

export function stripAgyOutput(raw) {
  return raw
    .replace(/\x1b\[[0-9;]*m/g, "")
    .replace(/\^D/g, "")
    .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, "");
}

function resolveAgyBin(override) {
  if (override) return override;
  const which = spawnSync("sh", ["-c", "command -v agy"], { encoding: "utf8" });
  const bin = (which.stdout || "").trim();
  if (which.status !== 0 || !bin) {
    throw new Error("agy CLI not found on PATH (install Antigravity CLI)");
  }
  return bin;
}

function haveScript() {
  const which = spawnSync("sh", ["-c", "command -v script"], { encoding: "utf8" });
  return which.status === 0 && Boolean((which.stdout || "").trim());
}

function* walkFiles(dir) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return;
  }
  for (const ent of entries) {
    const path = join(dir, ent.name);
    if (ent.isDirectory()) yield* walkFiles(path);
    else yield path;
  }
}

function findNewestTranscript(sinceMs) {
  let best = null;
  let bestMtime = sinceMs;
  for (const path of walkFiles(BRAIN_DIR)) {
    if (!path.endsWith("transcript.jsonl")) continue;
    let st;
    try {
      st = statSync(path);
    } catch {
      continue;
    }
    if (st.mtimeMs >= sinceMs && st.mtimeMs >= bestMtime) {
      best = path;
      bestMtime = st.mtimeMs;
    }
  }
  return best;
}

function extractFromTranscript(path) {
  const lines = readFileSync(path, "utf8").split("\n");
  let last = null;
  for (const line of lines) {
    if (!line.trim()) continue;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch {
      continue;
    }
    if (rec.source === "MODEL" && rec.status === "DONE" && rec.type === "PLANNER_RESPONSE") {
      last = rec;
    }
  }
  return typeof last?.content === "string" ? last.content : "";
}

function killProcessGroup(pid, signal) {
  try {
    process.kill(-pid, signal);
  } catch {
    try {
      process.kill(pid, signal);
    } catch {
      /* ignore */
    }
  }
}

const activeChildren = new Set();

function stopActiveChildren(signal = "SIGTERM") {
  for (const child of activeChildren) {
    if (child.exitCode === null && child.signalCode === null) {
      killProcessGroup(child.pid, signal);
    }
  }
}

process.on("SIGINT", () => {
  stopActiveChildren();
  process.exit(130);
});
process.on("SIGTERM", () => {
  stopActiveChildren();
  process.exit(143);
});

function runWithTimeout(cmd, args, { cwd, timeoutSec }) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      cwd,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });
    activeChildren.add(child);

    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr?.on("data", (chunk) => {
      stderr += chunk;
    });

    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      killProcessGroup(child.pid, "SIGTERM");
      setTimeout(() => killProcessGroup(child.pid, "SIGKILL"), 2000);
    }, timeoutSec * 1000);

    const finish = (result) => {
      clearTimeout(timer);
      activeChildren.delete(child);
      resolve(result);
    };

    child.on("error", (err) => {
      finish({ code: 127, stdout, stderr: `${stderr}${err.message}`, timedOut });
    });

    child.on("close", (code) => {
      finish({ code: code ?? 1, stdout, stderr, timedOut });
    });
  });
}

export async function runAgyWebsearch(prompt, options = {}) {
  const agyBin = resolveAgyBin(options.agyBin || process.env.EXPLORE_AGY_BIN);
  const model = options.model ?? process.env.EXPLORE_AGY_MODEL ?? DEFAULT_MODEL;
  const omitModel = process.env.EXPLORE_AGY_NO_MODEL === "1";
  const timeoutSec = Number(options.timeoutSec ?? process.env.EXPLORE_AGY_TIMEOUT ?? DEFAULT_TIMEOUT_SEC);
  const extTimeoutSec = timeoutSec + 30;
  const printTimeout = `${timeoutSec}s`;

  if (!haveScript()) {
    throw new Error("script(1) not found on PATH (required for agy pseudo-TTY capture)");
  }

  const scratch = mkdtempSync(join(tmpdir(), "explore-websearch-"));
  const markerMs = Date.now();
  writeFileSync(join(scratch, ".t"), "");

  const agyArgs = ["-p", prompt, "--dangerously-skip-permissions", "--print-timeout", printTimeout];
  if (!omitModel && model) {
    agyArgs.push("--model", model);
  }

  const scriptArgs = ["-q", "/dev/null", agyBin, ...agyArgs];
  const { stdout, stderr, timedOut } = await runWithTimeout("script", scriptArgs, {
    cwd: scratch,
    timeoutSec: extTimeoutSec,
  });

  let answer = stripAgyOutput(stdout);

  if (!hasContent(answer)) {
    const transcript = findNewestTranscript(markerMs);
    if (transcript) {
      answer = extractFromTranscript(transcript);
    }
  }

  rmSync(scratch, { recursive: true, force: true });

  if (!hasContent(answer)) {
    const err = new Error("agy produced no output (print-mode bug #76; transcript fallback also empty)");
    err.stderrTail = stderr.trim().split("\n").slice(-10).join("\n");
    err.timedOut = timedOut;
    throw err;
  }

  return answer;
}
