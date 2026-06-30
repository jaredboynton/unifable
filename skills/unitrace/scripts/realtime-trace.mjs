#!/usr/bin/env node
// Two-phase gpt-realtime-2 trace: explore (read tools) then structured submit_trace.
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import {
  RealtimeError,
  askStructured,
  realtimeReasoningConfig,
  withReasoningSteer,
  DEFAULT_UNITRACE_REASONING_EFFORT,
  DEFAULT_SUBMIT_REASONING_EFFORT,
} from "./lib/realtime_client.mjs";
import { buildExploreToolSchemas, dispatchToolBatch, extractFunctionCalls, parseArguments } from "./lib/rt-tools.mjs";
import {
  traceProviderSchema,
  traceProseSchema,
  tracePointerSchema,
  validateTraceObject,
  applyGroundingManifest,
  normalizeReadPath,
  SUBMIT_SCHEMA_NAME,
  SUBMIT_PROSE_SCHEMA_NAME,
  SUBMIT_POINTER_SCHEMA_NAME,
} from "./lib/trace-schema.mjs";
import { renderTraceStructured } from "./lib/render-trace-structured.mjs";
import {
  lintExploreWire,
  parseExploreWire,
  validateTraceWire,
} from "./lib/explore-wire-format.mjs";
import { traceGkWireSubmitRules } from "./lib/explore-output-prompt.mjs";
import { seedExploreReads, shouldStopExplore } from "./lib/rt-map-seed.mjs";
import { extractMapBlock, extractQuestion, compactMapBlock } from "./lib/rt-trace-utils.mjs";
import { pickCodePassages, mergeProseWithPassages } from "./lib/rt-pick-passages.mjs";
import {
  flushFrames,
  logFrame,
  trackSentItem,
  waitForResponse,
} from "./lib/rt-session-utils.mjs";
import { RtAgentSession } from "./lib/rt-agent-session.mjs";
import {
  buildReadIndexEntries,
  buildReadIndex,
  orderReadCacheEntries,
  rehydratePointerSubmit,
} from "./lib/rt-rehydrate-submit.mjs";
import { daemonAsk, daemonEnabled, warmDaemonPool } from "./lib/daemon-client.mjs";
import { createRtinferToolCaller } from "./lib/rtinfer-tool-caller.mjs";
import { runExploreNav } from "./lib/rt-explore-nav.mjs";

const WIRE_SUBMIT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["wire"],
  properties: {
    wire: { type: "string", description: "Complete wire plaintext trace with SECTION blocks and file tokens." },
  },
};
const WIRE_SUBMIT_NAME = "submit_wire_trace";

const UNITRACE_RT_PARALLEL_TOOL_CALLS = envBool("UNITRACE_RT_PARALLEL_TOOL_CALLS", true);

const UNITRACE_SYSTEM = [
  "You are a codebase exploration assistant operating in read-only mode.",
  "Gather ground truth for the question — read load-bearing files only, not the whole repo.",
  "",
  "Use the explore_exec tool only. Write JavaScript that calls tools.grep, tools.read, tools.batch_read, tools.list_dir, and tools.shell.",
  "Batch independent work with Promise.all inside one explore_exec call — e.g. grep for entry symbols and read 40-line spans on 4-8 paths in parallel.",
  "For implementation or control-flow questions, prefer source files over AGENTS/README/docs unless the question is explicitly about docs, policy, or config.",
  "Unless the question explicitly asks about wire/plaintext mode or UNITRACE_WIRE_FORMAT, follow the default structured trace path rather than optional wire-format branches.",
  "",
  "Workflow:",
  "1. Orient from REPO MAP paths in the user message.",
  "2. explore_exec: Promise.all([tools.grep(...), tools.read({path, start_line, end_line}), ...]).",
  "3. Follow imports/spawn under lib/ when they affect the answer; read targeted line ranges, not whole files when possible.",
  "4. Stop after 2-3 explore_exec turns or once 4-8 load-bearing files are read.",
  "",
  "Do not narrate steps or tool calls. Perform all searching/reading silently.",
  "Do NOT emit assistant commentary before explore_exec — call explore_exec immediately.",
  "",
  "tools.grep returns { hits: [{ path, lineNumber, content }], hitCount, truncated }. Use hits.find(h => ...), not grep(...).find(...).",
  "tools.read returns { path, start_line, end_line, line_count, preview } — full text is tracked for submit; use preview only for orientation.",
  "tools.shell is read-only bash (rg, head, git log, etc.) — use when helpful.",
  "",
  "Do NOT write the final answer yet. Only explore with explore_exec.",
  "Never invent paths, functions, or behavior.",
  "Do not call unitrace.sh, trace-gemini.sh, trace-rt.sh, or any explore wrapper recursively.",
].join("\n");

const SUBMIT_SYSTEM = [
  "You synthesize a structured codebase trace from exploration evidence.",
  "You MUST call submit_trace exactly once with a complete JSON object matching the schema.",
  "",
  "Rules:",
  "- Be concise: opening_summary <= 120 words; each section body <= 45 words.",
  "- At most 5 code_passages; each span <= 40 lines.",
  "- Ground every claim in the explore tool log and read excerpts provided.",
  "- For implementation / end-to-end questions, prefer source files over AGENTS/README/docs for evidence; use docs only when the question is about policy, usage, or config.",
  "- For pipeline or end-to-end questions, name the real scripts/functions/modules in order instead of generic stage labels.",
  "- For implementation questions, every flow step should name at least one concrete script or function from the evidence when one is available.",
  "- When a lookup/enforcement/helper function definition is available in the read index, cite that definition rather than nearby imports, interfaces, or constants.",
  "- When the same handler has distinct credential or request branches (for example JWT vs API key), cover each material branch instead of only the first one you saw.",
  "- Prefer citations around the function or branch that implements the behavior, not top-of-file constants or overview comments, unless those are the answer.",
  "- If the question names a script/module, include that file plus the direct load-bearing callees that make the answer true.",
  "- Unless the question explicitly asks about wire/plaintext mode or UNITRACE_WIRE_FORMAT, describe the default structured trace path, not optional wire-format branches.",
  "- Every code_passage.file_path MUST be one of the schema enum values for files read during explore.",
  "- Never use repo-map, grep-only, list_dir-only, or explore_exec-only paths in code_passages.",
  "- When the question contrasts tools, modes, or code paths, comparison_tables MUST be non-empty.",
  "- Include one section per major script/module (not every file read).",
  "- flow_steps: 4-8 short pipeline strings.",
  "- Use empty string or empty arrays only for truly unused optional fields.",
].join("\n");

const WIRE_SUBMIT_SYSTEM = [
  "You synthesize a codebase trace from exploration evidence.",
  `You MUST call ${WIRE_SUBMIT_NAME} exactly once with the wire field containing the complete wire plaintext trace.`,
  traceGkWireSubmitRules(),
].join("\n\n");

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

function envFloat(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

function envInt(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  const n = Number(v);
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

function envBool(name, fallback) {
  const v = process.env[name];
  if (v == null || v === "") return fallback;
  return v === "1" || v.toLowerCase() === "true" || v === "yes";
}

const DEFAULT_TIMEOUT = envFloat("UNITRACE_RT_TIMEOUT", 300);
const DEFAULT_UNITRACE_MAX_TURNS = envInt("UNITRACE_RT_UNITRACE_MAX_TURNS", 3);
const SUBMIT_REASK = envBool("UNITRACE_RT_SUBMIT_REASK", true);
const SUBMIT_PACKET_MAX = envInt("UNITRACE_RT_SUBMIT_PACKET_MAX", 45_000);
const UNITRACE_MAX_READS = envInt("UNITRACE_RT_UNITRACE_MAX_READS", 20);
const UNITRACE_MIN_READS = envInt("UNITRACE_RT_UNITRACE_MIN_READS", 4);
const READ_EXCERPT_MAX = envInt("UNITRACE_RT_READ_EXCERPT_MAX", 6000);
const READ_INDEX_PREVIEW_LINES = envInt("UNITRACE_RT_READ_INDEX_PREVIEW_LINES", 14);
const SUBMIT_EXCERPT_FILES = envInt("UNITRACE_RT_SUBMIT_EXCERPT_FILES", 5);
const UNITRACE_RT_MAP_SEED = envBool("UNITRACE_RT_MAP_SEED", true);
const UNITRACE_RT_STOP_READS = envInt("UNITRACE_RT_STOP_READS", 12);
const UNITRACE_RT_STOP_TOOL_CALLS = envInt("UNITRACE_RT_STOP_TOOL_CALLS", 3);
const UNITRACE_RT_SUBMIT_FRESH_CONTEXT = envBool("UNITRACE_RT_SUBMIT_FRESH_CONTEXT", true);
const UNITRACE_RT_SUBMIT_SLIM_SCHEMA = envBool("UNITRACE_RT_SUBMIT_SLIM_SCHEMA", true);
const UNITRACE_RT_HOST_PASSAGES = envBool("UNITRACE_RT_HOST_PASSAGES", true);
const UNITRACE_RT_SUBMIT_POINTER_INDEX = envBool("UNITRACE_RT_SUBMIT_POINTER_INDEX", true);
const UNITRACE_RT_UNITRACE_REASONING_EFFORT =
  process.env.UNITRACE_RT_UNITRACE_REASONING_EFFORT
  || process.env.UNITRACE_RT_REASONING_EFFORT
  || DEFAULT_UNITRACE_REASONING_EFFORT;
const UNITRACE_RT_SUBMIT_REASONING_EFFORT =
  process.env.UNITRACE_RT_SUBMIT_REASONING_EFFORT
  || process.env.UNITRACE_RT_REASONING_EFFORT
  || DEFAULT_SUBMIT_REASONING_EFFORT;
const UNITRACE_RT_UNITRACE_TOOL_REQUIRED = envBool("UNITRACE_RT_UNITRACE_TOOL_REQUIRED", true);
const UNITRACE_RT_MAP_COMPACT_SUBMIT = envBool("UNITRACE_RT_MAP_COMPACT_SUBMIT", true);

// Daemon submit: synthesize the pointer trace over the warm gpt-realtime daemon
// pool (reasoning omitted), mirroring websearch's runDaemonPointerSubmit. The
// daemon is never on the correctness path — a miss/invalid result falls back to
// the live-session runSubmitPhase. Default on; disable with UNITRACE_RT_DAEMON=0.
const UNITRACE_RT_DAEMON = envBool("UNITRACE_RT_DAEMON", true) && daemonEnabled();
const UNITRACE_RT_NAMESPACE = (process.env.UNITRACE_RT_NAMESPACE || "trace").trim();
const UNITRACE_RT_SYNTH_MODEL = (process.env.UNITRACE_RT_SYNTH_MODEL || "gpt-realtime-2").trim();
// Explore strategy (A/B-decided): nav = host-driven micro-agent (mini navigators
// + host hydration). On the kepler precision set nav delivered the best
// quality-per-second (~2x faster than the agentic explore_exec loop at
// comparable grounding) and fails open to agentic when the daemon is
// unavailable. agentic = legacy full-model explore_exec loop; hybrid = nav with a
// one-turn agentic top-up on thin coverage. See docs/benchmarks/trace-fast.md.
const UNITRACE_RT_UNITRACE_MODE = (process.env.UNITRACE_RT_UNITRACE_MODE || "nav").trim();
const UNITRACE_RT_NAV_MODEL = (process.env.UNITRACE_RT_NAV_MODEL || "gpt-realtime-mini").trim();

function submitTransport() {
  const t = String(process.env.UNITRACE_RT_SUBMIT_TRANSPORT || "rt").toLowerCase();
  if (t === "wire-rt" || t === "rt") return t;
  return "rt";
}

function truncateText(text, max) {
  const s = String(text || "");
  if (s.length <= max) return s;
  return s.slice(0, max) + `\n... [truncated ${s.length - max} chars]`;
}

function mergeExcerpt(prev, next) {
  return prev ? `${prev}\n---\n${next}` : next;
}

function clampExcerptTail(text, max) {
  const s = String(text || "");
  if (s.length <= max) return s;
  // Keep the most recent reads; snap to a line boundary so the first surviving
  // line is not a partial "N|..." fragment.
  const tail = s.slice(s.length - max);
  const nl = tail.indexOf("\n");
  return nl >= 0 ? tail.slice(nl + 1) : tail;
}

function clampExcerptHead(text, max) {
  const s = String(text || "");
  if (s.length <= max) return s;
  const head = s.slice(0, max);
  const nl = head.lastIndexOf("\n");
  return nl >= 0 ? head.slice(0, nl) : head;
}

function extractAnchorSymbols(orderedEntries, { max = 10 } = {}) {
  const patterns = [
    /^\d+\|\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)/gm,
    /^\d+\|\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=/gm,
    /^\d+\|\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)/gm,
  ];
  const out = [];
  const seen = new Set();
  for (const [filePath, excerpt] of orderedEntries) {
    for (const re of patterns) {
      for (const match of String(excerpt || "").matchAll(re)) {
        const name = match[1];
        if (!name || seen.has(name)) continue;
        seen.add(name);
        out.push({ name, filePath });
        if (out.length >= max) return out;
      }
    }
  }
  return out;
}

// Per-file read cache that keeps two layers: "pinned" excerpts (grep-hit
// definition windows — the answer locations) which always survive at the front,
// and "recent" unpinned reads (model exploration) filling the remaining budget.
// This stops a later, less-relevant read from truncating the definition window.
function makeReadTracker(workspace, filesRead, readCache) {
  const pinned = new Map();
  const recent = new Map();
  return (rel, excerpt, opts = {}) => {
    const normalized = normalizeReadPath(workspace, rel);
    if (!normalized) return;
    filesRead.add(normalized);

    if (opts.pin) {
      pinned.set(normalized, clampExcerptHead(mergeExcerpt(pinned.get(normalized), excerpt), READ_EXCERPT_MAX));
    } else {
      recent.set(normalized, clampExcerptTail(mergeExcerpt(recent.get(normalized), excerpt), READ_EXCERPT_MAX));
    }

    const pin = pinned.get(normalized) || "";
    const rec = recent.get(normalized) || "";
    let combined = pin && rec ? `${pin}\n---\n${rec}` : pin || rec;
    if (combined.length > READ_EXCERPT_MAX) {
      combined = pin
        ? mergeExcerpt(pin, clampExcerptTail(rec, Math.max(0, READ_EXCERPT_MAX - pin.length - 5)))
        : clampExcerptTail(rec, READ_EXCERPT_MAX);
    }
    readCache.set(normalized, combined);
  };
}

async function runExplorePhaseSession(session, {
  prompt, question, mapBlock, workspace, deadlineMs, maxTurns, framesPath, filesRead, readCache, toolLog, toolResults,
}) {
  const conn = session.connection;
  const exploreItemIds = new Set();
  const exploreTools = buildExploreToolSchemas();
  const sessionUpdate = {
    type: "session.update",
    session: {
      type: "realtime",
      instructions: UNITRACE_SYSTEM,
      output_modalities: ["text"],
      tools: exploreTools,
      tool_choice: UNITRACE_RT_UNITRACE_TOOL_REQUIRED ? "required" : "auto",
      parallel_tool_calls: UNITRACE_RT_PARALLEL_TOOL_CALLS,
      ...realtimeReasoningConfig(UNITRACE_RT_UNITRACE_REASONING_EFFORT),
    },
  };
  session.prewarm(sessionUpdate);

  const userItem = {
    type: "conversation.item.create",
    item: {
      type: "message",
      role: "user",
      content: [{ type: "input_text", text: withReasoningSteer(prompt) }],
    },
  };
  conn.send(userItem);
  logFrame(framesPath, "send", userItem);
  trackSentItem(exploreItemIds, userItem);

  let seedPaths = [];
  const onRead = makeReadTracker(workspace, filesRead, readCache);
  if (UNITRACE_RT_MAP_SEED) {
    seedPaths = seedExploreReads({
      workspace,
      question: question || extractQuestion(prompt),
      mapBlock,
      filesRead,
      readCache,
      onRead,
    });
    if (seedPaths.length) {
      toolLog.push(`seed reads: ${seedPaths.join(", ")}`);
      const seedNote = {
        type: "conversation.item.create",
        item: {
          type: "message",
          role: "user",
          content: [{
            type: "input_text",
            text: withReasoningSteer(`SEED READS (already in FILES READ; do not rediscover): ${seedPaths.join(", ")}. Use explore_exec for remaining load-bearing paths only.`, UNITRACE_RT_UNITRACE_REASONING_EFFORT),
          }],
        },
      };
      conn.send(seedNote);
      logFrame(framesPath, "send", seedNote);
      trackSentItem(exploreItemIds, seedNote);
    }
  }

  let nudgeCount = 0;
  let toolTurnCount = 0;
  let exploreTurns = 0;
  let maxBatch = 0;

  for (let turn = 0; turn < maxTurns; turn++) {
    if (Date.now() >= deadlineMs) throw new RealtimeError("explore phase timed out");
    if (filesRead.size >= UNITRACE_MAX_READS) break;

    const respCreate = {
      type: "response.create",
      response: {
        output_modalities: ["text"],
        ...(UNITRACE_RT_UNITRACE_TOOL_REQUIRED ? { tool_choice: "required" } : {}),
      },
    };
    session.send(respCreate);

    const pendingArgs = new Map();
    let turnText = "";
    let functionCalls = [];
    let status = "";
    let retried = false;
    while (true) {
      try {
        ({ text: turnText, functionCalls, status } = await waitForResponse(session.connection, {
          deadlineMs, framesPath, pendingArgs, exploreItemIds,
        }));
        break;
      } catch (err) {
        if (!retried && session.isConnectionClosedError(err)) {
          retried = true;
          session.alive = false;
          await session.ensureAlive("explore_retry");
          continue;
        }
        throw err;
      }
    }

    const q = question || extractQuestion(prompt);
    const stopNow = () => shouldStopExplore({
      filesRead,
      question: q,
      workspace,
      toolTurnCount,
      minReads: UNITRACE_MIN_READS,
      stopReads: UNITRACE_RT_STOP_READS,
      stopToolCalls: UNITRACE_RT_STOP_TOOL_CALLS,
    });

    if (!functionCalls.length) {
      if (stopNow()) break;
      if (filesRead.size >= UNITRACE_MIN_READS) break;
      if (turnText && nudgeCount < 1) {
        nudgeCount++;
        const nudge = {
          type: "conversation.item.create",
          item: {
            type: "message",
            role: "user",
            content: [{ type: "input_text", text: withReasoningSteer("Call explore_exec once more for any missing load-bearing files, then stop.", UNITRACE_RT_UNITRACE_REASONING_EFFORT) }],
          },
        };
        conn.send(nudge);
        logFrame(framesPath, "send", nudge);
        trackSentItem(exploreItemIds, nudge);
        continue;
      }
      if (status === "completed" || status === "incomplete") break;
      throw new RealtimeError(
        status
          ? `explore ended with status ${status} and no tool calls`
          : "websocket closed before explore completed"
      );
    }

    exploreTurns++;
    maxBatch = Math.max(maxBatch, functionCalls.length);
    toolTurnCount += functionCalls.length;

    const dispatched = await dispatchToolBatch(functionCalls, workspace, { deadlineMs, onRead });
    for (const { call, args, result } of dispatched) {
      const summary = `${call.name} ${JSON.stringify(args).slice(0, 80)} -> ok=${result.ok}`;
      toolLog.push(summary);
      toolResults.push({ tool: call.name, args, result: truncateText(JSON.stringify(result), 1500) });
    }

    for (const { call, result } of dispatched) {
      const outputItem = {
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: call.call_id,
          output: JSON.stringify(result),
        },
      };
      conn.send(outputItem);
      logFrame(framesPath, "send", outputItem);
      trackSentItem(exploreItemIds, outputItem);
    }

    if (filesRead.size >= UNITRACE_MAX_READS || stopNow()) break;
  }
  flushFrames(framesPath);
  return { toolTurnCount, exploreTurns, maxBatch, seedPaths, exploreItemIds };
}

async function runExplorePhaseDaemon(model, {
  prompt, question, mapBlock, workspace, deadlineMs, maxTurns, framesPath, filesRead, readCache, toolLog, toolResults,
}) {
  const callModel = createRtinferToolCaller({
    systemPrompt: UNITRACE_SYSTEM,
    toolSpecs: buildExploreToolSchemas(),
    finishToolName: "explore_exec",
    model,
    reasoningEffort: UNITRACE_RT_UNITRACE_REASONING_EFFORT,
    namespace: `${UNITRACE_RT_NAMESPACE}-agentic`,
    schemaName: "trace_explore_turn",
    addendum: [
      "You are operating through rtinferd rather than a live WebSocket session.",
      "Return explore_exec tool calls only. When enough files have been read, return zero tool calls.",
      "Batch independent work with Promise.all inside one explore_exec call when possible.",
    ].join("\n"),
    debug: Boolean(framesPath),
  });

  try {
    const exploreItemIds = new Set();
    const messages = [{ role: "user", content: withReasoningSteer(prompt) }];
    let seedPaths = [];
    const onRead = makeReadTracker(workspace, filesRead, readCache);
    if (UNITRACE_RT_MAP_SEED) {
      seedPaths = seedExploreReads({
        workspace,
        question: question || extractQuestion(prompt),
        mapBlock,
        filesRead,
        readCache,
        onRead,
      });
      if (seedPaths.length) {
        toolLog.push(`seed reads: ${seedPaths.join(", ")}`);
        messages.push({
          role: "user",
          content: withReasoningSteer(
            `SEED READS (already in FILES READ; do not rediscover): ${seedPaths.join(", ")}. Use explore_exec for remaining load-bearing paths only.`,
            UNITRACE_RT_UNITRACE_REASONING_EFFORT,
          ),
        });
      }
    }

    let nudgeCount = 0;
    let toolTurnCount = 0;
    let exploreTurns = 0;
    let maxBatch = 0;

    for (let turn = 0; turn < maxTurns; turn += 1) {
      if (Date.now() >= deadlineMs) throw new RealtimeError("explore phase timed out");
      if (filesRead.size >= UNITRACE_MAX_READS) break;

      const response = await callModel(messages);
      if (response == null) return null;
      const functionCalls = extractFunctionCalls({
        output: (response.tool_calls || []).map((call) => ({
          type: "function_call",
          call_id: call.id,
          name: call.function?.name,
          arguments: call.function?.arguments,
        })),
      });
      const turnText = response.content || "";
      const q = question || extractQuestion(prompt);
      const stopNow = () => shouldStopExplore({
        filesRead,
        question: q,
        workspace,
        toolTurnCount,
        minReads: UNITRACE_MIN_READS,
        stopReads: UNITRACE_RT_STOP_READS,
        stopToolCalls: UNITRACE_RT_STOP_TOOL_CALLS,
      });

      if (!functionCalls.length) {
        if (stopNow()) break;
        if (filesRead.size >= UNITRACE_MIN_READS) break;
        if (turnText && nudgeCount < 1) {
          nudgeCount += 1;
          messages.push({ role: "assistant", content: turnText });
          messages.push({
            role: "user",
            content: withReasoningSteer(
              "Call explore_exec once more for any missing load-bearing files, then stop.",
              UNITRACE_RT_UNITRACE_REASONING_EFFORT,
            ),
          });
          continue;
        }
        break;
      }

      messages.push({
        role: "assistant",
        content: turnText || "",
        tool_calls: response.tool_calls || [],
      });

      exploreTurns += 1;
      maxBatch = Math.max(maxBatch, functionCalls.length);
      toolTurnCount += functionCalls.length;

      const dispatched = await dispatchToolBatch(functionCalls, workspace, { deadlineMs, onRead });
      for (const { call, args, result } of dispatched) {
        const summary = `${call.name} ${JSON.stringify(args).slice(0, 80)} -> ok=${result.ok}`;
        toolLog.push(summary);
        toolResults.push({ tool: call.name, args, result: truncateText(JSON.stringify(result), 1500) });
        messages.push({
          role: "tool",
          tool_call_id: call.call_id,
          content: JSON.stringify(result),
        });
      }

      if (filesRead.size >= UNITRACE_MAX_READS || stopNow()) break;
    }

    return { toolTurnCount, exploreTurns, maxBatch, seedPaths, exploreItemIds };
  } finally {
    callModel.close();
  }
}

// Explore-phase dispatcher (A/B). agentic = full-model explore_exec loop
// (runExplorePhase, legacy default). nav = host-driven micro-agent (mini
// navigators + host hydration, no live-session tool loop). hybrid = nav primary,
// with one agentic turn appended when nav coverage is thin. Fail-open: nav/hybrid
// fall back to the agentic loop whenever the daemon path is unavailable.
async function dispatchExplore({ model, ensureSession, ...args }) {
  const mode = UNITRACE_RT_UNITRACE_MODE;
  if (mode !== "nav" && mode !== "hybrid") {
    if (UNITRACE_RT_DAEMON) {
      const daemonStats = await runExplorePhaseDaemon(model, args);
      if (daemonStats) return daemonStats;
    }
    return runExplorePhaseSession(await ensureSession(), args);
  }

  const { workspace, question, mapBlock, filesRead, readCache, toolLog, framesPath } = args;
  const onRead = makeReadTracker(workspace, filesRead, readCache);
  const navStats = await runExploreNav({
    workspace,
    question,
    mapBlock,
    filesRead,
    readCache,
    onRead,
    namespace: UNITRACE_RT_NAMESPACE,
    navModel: UNITRACE_RT_NAV_MODEL,
    debug: Boolean(framesPath),
  });

  if (!navStats) {
    toolLog.push("phase explore_mode=nav_failopen->agentic");
    if (UNITRACE_RT_DAEMON) {
      const daemonStats = await runExplorePhaseDaemon(model, args);
      if (daemonStats) return daemonStats;
    }
    return runExplorePhaseSession(await ensureSession(), args);
  }
  if (navStats.seedPaths.length) toolLog.push(`seed reads: ${navStats.seedPaths.join(", ")}`);
  toolLog.push(`phase explore_mode=${mode} nav_turns=${navStats.exploreTurns} files_read=${filesRead.size}`);

  if (mode === "hybrid" && filesRead.size < UNITRACE_MIN_READS) {
    toolLog.push(`phase explore_hybrid_topup files_read=${filesRead.size} < ${UNITRACE_MIN_READS}`);
    let topUp = null;
    if (UNITRACE_RT_DAEMON) {
      topUp = await runExplorePhaseDaemon(model, { ...args, maxTurns: 1 });
    }
    if (!topUp) {
      topUp = await runExplorePhaseSession(await ensureSession(), { ...args, maxTurns: 1 });
    }
    return {
      toolTurnCount: navStats.toolTurnCount + topUp.toolTurnCount,
      exploreTurns: navStats.exploreTurns + topUp.exploreTurns,
      maxBatch: Math.max(navStats.maxBatch, topUp.maxBatch),
      seedPaths: [...new Set([...navStats.seedPaths, ...(topUp.seedPaths || [])])],
      exploreItemIds: topUp.exploreItemIds || new Set(),
    };
  }

  return navStats;
}

function buildSubmitPacket({
  question, mapBlock, submitInstructions, filesRead, readCache, toolLog, seedPaths = [], wire = false,
  hostPassages = false,
  pointerIndex = false,
}) {
  const readFiles = [...filesRead].sort();
  const orderedEntries = orderReadCacheEntries(readCache, seedPaths);
  const readIndexEntries = buildReadIndexEntries(orderedEntries, {
    maxFiles: SUBMIT_EXCERPT_FILES + 4,
  });
  const orderedPaths = readIndexEntries;
  const anchorSymbols = extractAnchorSymbols(orderedEntries);
  const usePointerIndex = pointerIndex && hostPassages && !wire;
  const submitMap = UNITRACE_RT_MAP_COMPACT_SUBMIT && mapBlock
    ? compactMapBlock(mapBlock)
    : mapBlock;
  const parts = [
    "ORIGINAL QUESTION:",
    question,
    "",
  ];
  if (mapBlock && !usePointerIndex) {
    parts.push(
      "REPO MAP (prefetch; useful for orientation, not citable in code_passages unless also listed below):",
      submitMap,
      "",
    );
  }
  parts.push(
    "FILES READ DURING EXPLORE:",
    readFiles.join("\n") || "(none)",
    "",
  );
  if (seedPaths.length) {
    parts.push(
      "HIGH PRIORITY FILES (seeded because they are likely load-bearing for this question):",
      seedPaths.join("\n"),
      "Prefer these and their direct callees over tangential helpers/docs unless the evidence clearly points elsewhere.",
      "",
    );
  }
  if (anchorSymbols.length) {
    parts.push(
      "LIKELY ANCHOR SYMBOLS (prefer these exact script/function names in the answer when relevant):",
      ...anchorSymbols.map(({ name, filePath }) => `- ${name} — ${filePath}`),
      "If one of these symbols directly answers the question, prefer citing its definition excerpt instead of nearby imports/constants/comments.",
      "",
    );
  }
  if (!usePointerIndex) {
    parts.push(
      "CODE_PASSAGES FILE_PATH ENUM:",
      readFiles.join("\n") || "(none)",
      "",
      "Only the CODE_PASSAGES FILE_PATH ENUM values may appear in code_passages[].file_path.",
      "A path seen only in REPO MAP, grep/list_dir/explore_exec output is not a valid code_passage file_path.",
      "",
    );
  }
    parts.push(
      "TOOL LOG:",
      toolLog.filter((l) => !l.startsWith("phase ")).slice(-8).join("\n") || "(none)",
      "",
    );
  if (usePointerIndex) {
    parts.push(buildReadIndex(orderedEntries, { maxFiles: SUBMIT_EXCERPT_FILES + 4, previewLines: READ_INDEX_PREVIEW_LINES }), "");
  } else {
    parts.push("READ EXCERPTS:");
    const excerptEntries = orderedEntries.slice(0, SUBMIT_EXCERPT_FILES);
    for (const [path, excerpt] of excerptEntries) {
      parts.push(`--- ${path} ---`, excerpt, "");
    }
    if (readCache.size > excerptEntries.length) {
      parts.push(`... (${readCache.size - excerptEntries.length} more files read, omitted from excerpts)`, "");
    }
  }
  if (wire) {
    parts.push(
      `Call ${WIRE_SUBMIT_NAME} once with the complete wire plaintext trace in the wire field.`,
      "Every <file:...> path must be copied exactly from CODE_PASSAGES FILE_PATH ENUM.",
    );
  } else if (usePointerIndex) {
    parts.push(
      `Call ${SUBMIT_POINTER_SCHEMA_NAME} once with prose fields and citation_spans (excerpt_index + line range).`,
      "Do NOT include code_passages or grounding_manifest — host rehydrates citations from READ INDEX.",
    );
  } else if (hostPassages) {
    parts.push(
      `Call ${SUBMIT_PROSE_SCHEMA_NAME} once with prose fields only (no code_passages — host assembles citations).`,
    );
  } else {
    parts.push(
      `Call ${SUBMIT_SCHEMA_NAME} once with the complete structured trace.`,
      "Every code_passage.file_path must be copied exactly from CODE_PASSAGES FILE_PATH ENUM.",
    );
  }
  if (submitInstructions) {
    parts.push("SUBMIT INSTRUCTIONS:", submitInstructions, "");
  }
  return { text: truncateText(parts.join("\n"), SUBMIT_PACKET_MAX), orderedPaths };
}

async function runSubmitPhase(conn, {
  submitPacket, orderedPaths = [], workspace, deadlineMs, framesPath, filesRead, readCache, toolTurns, reask,
  question, seedPaths = [], hostPassages = UNITRACE_RT_HOST_PASSAGES, authPath,
}) {
  const slim = UNITRACE_RT_SUBMIT_SLIM_SCHEMA;
  const transport = submitTransport();
  const onSend = (obj) => logFrame(framesPath, "send", obj);
  const onRecv = (obj) => logFrame(framesPath, "recv", obj);
  const usePointerIndex = hostPassages && UNITRACE_RT_SUBMIT_POINTER_INDEX && transport !== "wire-rt";
  const useHostPassages = hostPassages && !usePointerIndex;

  let lastError = null;
  let userText = typeof submitPacket === "string" ? submitPacket : submitPacket.text;
  const schemaName = usePointerIndex
    ? SUBMIT_POINTER_SCHEMA_NAME
    : useHostPassages
      ? SUBMIT_PROSE_SCHEMA_NAME
      : SUBMIT_SCHEMA_NAME;
  const schema = usePointerIndex
    ? tracePointerSchema({ question, slim, orderedPaths })
    : useHostPassages
      ? traceProseSchema({ question, slim })
      : traceProviderSchema({
        allowedCodePassagePaths: [...filesRead].sort(),
        question,
        slim,
        filesReadCount: filesRead.size,
      });

  const submitSystem = usePointerIndex
    ? [
      SUBMIT_SYSTEM.replace(/submit_trace/g, SUBMIT_POINTER_SCHEMA_NAME),
      "Return citation_spans with excerpt_index from READ INDEX plus line ranges — not full code.",
      "Do NOT include code_passages or grounding_manifest — host rehydrates citations.",
    ].join("\n")
    : useHostPassages
      ? [
        SUBMIT_SYSTEM.replace(/submit_trace/g, SUBMIT_PROSE_SCHEMA_NAME),
        "Do NOT include code_passages or grounding_manifest — host fills those.",
      ].join("\n")
      : SUBMIT_SYSTEM;

  for (let attempt = 0; attempt <= (reask ? 1 : 0); attempt++) {
    if (Date.now() >= deadlineMs) throw new RealtimeError("submit phase timed out");

    let parsed;
    try {
      parsed = await askStructured(conn, {
        system: submitSystem,
        user: userText,
        schema,
        schemaName,
        deadlineMs,
        onSend,
        onRecv,
        reasoningEffort: UNITRACE_RT_SUBMIT_REASONING_EFFORT,
      });
    } catch (e) {
      if (attempt < (reask ? 1 : 0)) {
        lastError = e.message;
        userText = `${userText}\n\nPREVIOUS SUBMIT FAILED: ${e.message}\nFix and call ${schemaName} again.`;
        continue;
      }
      throw e;
    }

    if (usePointerIndex) {
      parsed = rehydratePointerSubmit({
        pointer: parsed,
        orderedPaths,
        workspace,
        filesRead,
        readCache,
        toolTurns,
        seedPaths,
        question,
      });
    } else if (useHostPassages) {
      const passages = pickCodePassages({
        workspace,
        filesRead,
        readCache,
        seedPaths,
        question,
      });
      parsed = mergeProseWithPassages(parsed, passages, filesRead, toolTurns);
    } else {
      parsed = applyGroundingManifest(parsed, filesRead, toolTurns);
    }

    const err = validateTraceObject(parsed, { workspace, filesRead, toolTurns });
    if (err) {
      lastError = err;
      if (attempt < (reask ? 1 : 0)) {
        userText = `${userText}\n\nVALIDATION FAILED: ${err}\nFix grounding and call ${schemaName} again.`;
        continue;
      }
      throw new RealtimeError(`structured trace validation failed: ${err}`);
    }
    return parsed;
  }
  throw new RealtimeError(lastError || "structured submit failed");
}

// Daemon pointer submit: run the pointer-index submit over the warm daemon pool
// with submit reasoning (default low), reusing the same rehydrate + validate + reask
// loop as the live-session path. Returns rendered markdown + structured object on
// success, or null to signal fail-open to runSubmitPhase. The daemon is never on
// the correctness path.
async function runDaemonPointerSubmit({
  submitPacket, orderedPaths = [], workspace, filesRead, readCache, toolTurns, reask,
  question, seedPaths = [], debug = false,
}) {
  const slim = UNITRACE_RT_SUBMIT_SLIM_SCHEMA;
  const schema = tracePointerSchema({ question, slim, orderedPaths });
  const submitSystem = [
    SUBMIT_SYSTEM.replace(/submit_trace/g, SUBMIT_POINTER_SCHEMA_NAME),
    "Return citation_spans with excerpt_index from READ INDEX plus line ranges — not full code.",
    "Do NOT include code_passages or grounding_manifest — host rehydrates citations.",
  ].join("\n");
  let userText = typeof submitPacket === "string" ? submitPacket : submitPacket.text;
  const t0 = Date.now();

  for (let attempt = 0; attempt <= (reask ? 1 : 0); attempt += 1) {
    let parsed = await daemonAsk(
      UNITRACE_RT_NAMESPACE,
      {
        system: submitSystem,
        user: userText,
        schema,
        schemaName: SUBMIT_POINTER_SCHEMA_NAME,
        reasoningEffort: UNITRACE_RT_SUBMIT_REASONING_EFFORT,
      },
      { model: UNITRACE_RT_SYNTH_MODEL },
    );
    if (!parsed) return null; // daemon miss -> fall back to session submit

    parsed = rehydratePointerSubmit({
      pointer: parsed,
      orderedPaths,
      workspace,
      filesRead,
      readCache,
      toolTurns,
      seedPaths,
      question,
    });

    const err = validateTraceObject(parsed, { workspace, filesRead, toolTurns });
    if (err) {
      if (attempt < (reask ? 1 : 0)) {
        userText = `${userText}\n\nVALIDATION FAILED: ${err}\nFix grounding and call ${SUBMIT_POINTER_SCHEMA_NAME} again.`;
        continue;
      }
      if (debug) process.stderr.write(`phase submit_daemon_invalid=${err}\n`);
      return null; // validation failed after reask -> fall back
    }
    if (debug) process.stderr.write(`phase submit_daemon_ms=${Date.now() - t0} synth=${UNITRACE_RT_SYNTH_MODEL}\n`);
    return { markdown: renderTraceStructured(workspace, parsed), structured: parsed };
  }
  return null;
}

async function runWireSubmitPhase(conn, {
  submitPacket, workspace, deadlineMs, framesPath, filesRead, reask,
}) {
  const onSend = (obj) => logFrame(framesPath, "send", obj);
  const onRecv = (obj) => logFrame(framesPath, "recv", obj);
  let lastError = null;
  let userText = submitPacket;

  for (let attempt = 0; attempt <= (reask ? 1 : 0); attempt += 1) {
    if (Date.now() >= deadlineMs) throw new RealtimeError("wire submit phase timed out");

    let parsed;
    try {
      parsed = await askStructured(conn, {
        system: WIRE_SUBMIT_SYSTEM,
        user: userText,
        schema: WIRE_SUBMIT_SCHEMA,
        schemaName: WIRE_SUBMIT_NAME,
        deadlineMs,
        onSend,
        onRecv,
        reasoningEffort: UNITRACE_RT_SUBMIT_REASONING_EFFORT,
      });
    } catch (e) {
      if (attempt < (reask ? 1 : 0)) {
        lastError = e.message;
        userText = `${submitPacket}\n\nPREVIOUS SUBMIT FAILED: ${e.message}\nFix and call ${WIRE_SUBMIT_NAME} again.`;
        continue;
      }
      throw e;
    }

    const text = String(parsed?.wire || "").trim();
    if (!text) {
      lastError = "empty wire field";
      if (attempt < (reask ? 1 : 0)) continue;
      throw new RealtimeError(lastError);
    }

    const lint = lintExploreWire(text);
    const wireParsed = parseExploreWire(text);
    const validation = validateTraceWire(wireParsed, workspace, { allowedPaths: [...filesRead] });
    if (!validation.ok) {
      lastError = validation.errors.join("; ");
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nVALIDATION FAILED: ${lastError}\nFix grounding and call ${WIRE_SUBMIT_NAME} again.`;
        continue;
      }
      throw new RealtimeError(`wire trace validation failed: ${lastError}`);
    }
    if (!lint.ok) {
      lastError = lint.issues.join("; ");
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nFORMAT FAILED: ${lastError}\nUse wire plaintext only.`;
        continue;
      }
    }
    return text.endsWith("\n") ? text : `${text}\n`;
  }
  throw new RealtimeError(lastError || "wire submit failed");
}

async function runStructuredTrace({
  explorePrompt, submitInstructions, question, mapBlock: mapBlockArg, workspace, model, authPath,
  timeoutSec, exploreMaxTurns, framesPath, replayPath,
}) {
  const toolLog = [];
  const toolResults = [];
  const filesRead = new Set();
  const readCache = new Map();

  if (replayPath) return runStructuredReplay(replayPath, workspace, toolLog);

  const q = question || extractQuestion(explorePrompt);
  const mapBlock = mapBlockArg || extractMapBlock(explorePrompt);
  // Warm the daemon synth pool concurrently with the session connect + explore so
  // the submit batch never pays a connect+handshake. Fire-and-forget; fail-open.
  if (UNITRACE_RT_DAEMON) {
    warmDaemonPool(UNITRACE_RT_NAMESPACE, undefined, { model: UNITRACE_RT_SYNTH_MODEL }).catch(() => {});
    if (
      (UNITRACE_RT_UNITRACE_MODE === "nav" || UNITRACE_RT_UNITRACE_MODE === "hybrid") &&
      UNITRACE_RT_NAV_MODEL !== UNITRACE_RT_SYNTH_MODEL
    ) {
      warmDaemonPool(UNITRACE_RT_NAMESPACE, undefined, { model: UNITRACE_RT_NAV_MODEL }).catch(() => {});
    }
  }
  let session = null;
  const ensureSession = async () => {
    if (session) return session;
    session = new RtAgentSession({ model, authPath, framesPath });
    const connectStart = Date.now();
    await session.connect();
    const connectMs = Date.now() - connectStart;
    // Handshake cost is ~2% of wall (benchmarked); kept as a phase metric so future
    // tuning stays measurement-driven. The trace is generation-bound, not transport-bound.
    toolLog.push(`phase connect_ms=${connectMs}`);
    return session;
  };
  const deadlineMs = Date.now() + timeoutSec * 1000;

  try {
    const exploreStart = Date.now();
    const exploreStats = await dispatchExplore({
      model,
      ensureSession,
      prompt: explorePrompt,
      question: q,
      mapBlock,
      workspace,
      deadlineMs,
      maxTurns: exploreMaxTurns,
      framesPath,
      filesRead,
      readCache,
      toolLog,
      toolResults,
    });

    const exploreMs = Date.now() - exploreStart;
    toolLog.push(
      `phase explore_ms=${exploreMs} files_read=${filesRead.size} explore_turns=${exploreStats.exploreTurns} max_batch=${exploreStats.maxBatch} tool_calls=${exploreStats.toolTurnCount}`
    );

    const transport = submitTransport();
    if (UNITRACE_RT_SUBMIT_FRESH_CONTEXT && transport === "rt" && session) {
      await session.pruneItems(exploreStats.exploreItemIds);
    }

    const { text: submitPacket, orderedPaths } = buildSubmitPacket({
      question: q,
      mapBlock,
      submitInstructions,
      filesRead,
      readCache,
      toolLog,
      seedPaths: exploreStats.seedPaths || [],
      hostPassages: UNITRACE_RT_HOST_PASSAGES,
      pointerIndex: UNITRACE_RT_SUBMIT_POINTER_INDEX,
    });

    const submitStart = Date.now();
    let structured;
    if (transport === "wire-rt") {
      const { text: wirePacket } = buildSubmitPacket({
        question: q,
        mapBlock,
        submitInstructions,
        filesRead,
        readCache,
        toolLog,
        seedPaths: exploreStats.seedPaths || [],
        wire: true,
        hostPassages: false,
        pointerIndex: false,
      });
      const wireSession = await ensureSession();
      const wireText = await runWireSubmitPhase(wireSession.connection, {
        submitPacket: wirePacket,
        workspace,
        deadlineMs,
        framesPath,
        filesRead,
        reask: SUBMIT_REASK,
      });
      toolLog.push(`phase submit_ms=${Date.now() - submitStart}`);
      flushFrames(framesPath);
      return { text: wireText, toolLog };
    }

    const usePointerSubmit =
      UNITRACE_RT_HOST_PASSAGES && UNITRACE_RT_SUBMIT_POINTER_INDEX && transport === "rt";
    if (UNITRACE_RT_DAEMON && usePointerSubmit) {
      const daemonResult = await runDaemonPointerSubmit({
        submitPacket,
        orderedPaths,
        workspace,
        filesRead,
        readCache,
        toolTurns: exploreStats.toolTurnCount,
        reask: SUBMIT_REASK,
        question: q,
        seedPaths: exploreStats.seedPaths || [],
        debug: Boolean(framesPath),
      });
      if (daemonResult) {
        toolLog.push(`phase submit_ms=${Date.now() - submitStart} synth=daemon:${UNITRACE_RT_SYNTH_MODEL}`);
        flushFrames(framesPath);
        return { text: daemonResult.markdown, toolLog, structured: daemonResult.structured };
      }
    }

    const submitSession = await ensureSession();
    structured = await runSubmitPhase(submitSession.connection, {
      submitPacket,
      orderedPaths,
      workspace,
      deadlineMs,
      framesPath,
      filesRead,
      readCache,
      toolTurns: exploreStats.toolTurnCount,
      reask: SUBMIT_REASK,
      question: q,
      seedPaths: exploreStats.seedPaths || [],
      authPath,
    });

    toolLog.push(`phase submit_ms=${Date.now() - submitStart}`);

    const markdown = renderTraceStructured(workspace, structured);
    flushFrames(framesPath);
    return { text: markdown, toolLog, structured };
  } finally {
    if (session) session.close();
  }
}

async function runWireStructuredTrace({
  explorePrompt, submitInstructions, question, mapBlock: mapBlockArg, workspace, model, authPath,
  timeoutSec, exploreMaxTurns, framesPath, replayPath,
}) {
  const toolLog = [];
  const toolResults = [];
  const filesRead = new Set();
  const readCache = new Map();

  if (replayPath) throw new RealtimeError("wire replay not supported yet");

  const session = new RtAgentSession({ model, authPath, framesPath });
  await session.connect();
  const deadlineMs = Date.now() + timeoutSec * 1000;

  try {
    const exploreStart = Date.now();
    const q = question || extractQuestion(explorePrompt);
    const mapBlock = mapBlockArg || extractMapBlock(explorePrompt);
    const exploreStats = await runExplorePhaseSession(session, {
      prompt: explorePrompt,
      question: q,
      mapBlock,
      workspace,
      deadlineMs,
      maxTurns: exploreMaxTurns,
      framesPath,
      filesRead,
      readCache,
      toolLog,
      toolResults,
    });

    toolLog.push(
      `phase explore_ms=${Date.now() - exploreStart} files_read=${filesRead.size} explore_turns=${exploreStats.exploreTurns} max_batch=${exploreStats.maxBatch} tool_calls=${exploreStats.toolTurnCount}`
    );

    if (UNITRACE_RT_SUBMIT_FRESH_CONTEXT) {
      await session.pruneItems(exploreStats.exploreItemIds);
    }

    const { text: submitPacket } = buildSubmitPacket({
      question: q,
      mapBlock,
      submitInstructions,
      filesRead,
      readCache,
      toolLog,
      seedPaths: exploreStats.seedPaths || [],
      wire: true,
      hostPassages: false,
      pointerIndex: false,
    });

    const submitStart = Date.now();
    const wireText = await runWireSubmitPhase(session.connection, {
      submitPacket,
      workspace,
      deadlineMs,
      framesPath,
      filesRead,
      reask: SUBMIT_REASK,
    });
    toolLog.push(`phase submit_ms=${Date.now() - submitStart}`);

    flushFrames(framesPath);
    return { text: wireText, toolLog };
  } finally {
    session.close();
  }
}

function runStructuredReplay(replayPath, workspace, toolLog) {
  let argsJson = "";
  for (const line of readFileSync(replayPath, "utf8").split("\n")) {
    if (!line.trim()) continue;
    const rec = JSON.parse(line);
    if (rec.dir !== "recv") continue;
    const env = rec.event;
    if (!env) continue;
    if (env.type === "response.function_call_arguments.done" && env.arguments) {
      argsJson = env.arguments;
    }
    if (env.type === "response.done" || env.type === "response.completed") {
      const resp = env.response || env;
      const output = Array.isArray(resp.output) ? resp.output : [];
      for (const item of output) {
        if (item?.type === "function_call" && item.arguments) {
          argsJson = typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments);
        }
      }
    }
  }
  if (!argsJson) throw new RealtimeError("replay missing submit_trace arguments");
  const structured = JSON.parse(argsJson);
  toolLog.push("replay submit_trace");
  const markdown = renderTraceStructured(workspace, structured);
  return { text: markdown, toolLog, structured };
}

async function main() {
  const promptFile = argValue("--prompt-file");
  const mapFile = argValue("--map-file");
  const questionArg = argValue("--question");
  const submitPromptFile = argValue("--submit-prompt-file");
  const out = argValue("--out");
  const raw = argValue("--raw");
  const structuredOut = argValue("--structured-out");
  const errFile = argValue("--err");
  const workspace = argValue("--workspace", process.cwd());
  const model = argValue("--model", process.env.UNITRACE_RT_MODEL || "gpt-realtime-2");
  const authPath = argValue("--auth-path", process.env.UNITRACE_CODEX_AUTH_PATH);
  const framesPath = argValue("--frames");
  const replayPath = argValue("--replay");
  const timeoutSec = Number(argValue("--timeout", String(DEFAULT_TIMEOUT)));
  const exploreMaxTurns = Number(argValue("--explore-max-turns", String(DEFAULT_UNITRACE_MAX_TURNS)));
  const wire = argValue("--wire", process.env.UNITRACE_WIRE_FORMAT || "0") === "1";

  if (!promptFile || !out || !raw || !errFile) {
    process.stderr.write(
      "usage: realtime-trace.mjs --prompt-file --workspace --out --raw --err [--submit-prompt-file] [--structured-out] [--wire 1]\n"
    );
    process.exit(2);
  }

  const explorePrompt = readFileSync(promptFile, "utf8");
  const submitInstructions = submitPromptFile ? readFileSync(submitPromptFile, "utf8") : "";
  const mapBlockFromFile = mapFile ? readFileSync(mapFile, "utf8") : "";
  const question = questionArg || extractQuestion(explorePrompt);

  let result;
  try {
    if (wire) {
      result = await runWireStructuredTrace({
        explorePrompt,
        submitInstructions,
        question,
        mapBlock: mapBlockFromFile,
        workspace,
        model,
        authPath,
        timeoutSec,
        exploreMaxTurns,
        framesPath,
        replayPath,
      });
    } else {
      result = await runStructuredTrace({
        explorePrompt,
        submitInstructions,
        question,
        mapBlock: mapBlockFromFile,
        workspace,
        model,
        authPath,
        timeoutSec,
        exploreMaxTurns,
        framesPath,
        replayPath,
      });
    }
  } catch (e) {
    const msg = e instanceof RealtimeError ? e.message : (e?.message || String(e));
    writeFileSync(errFile, msg + "\n", "utf8");
    process.stderr.write(`realtime-trace: ${msg}\n`);
    process.exit(1);
  }

  const text = result.text.endsWith("\n") ? result.text : result.text + "\n";
  writeFileSync(out, text, "utf8");
  writeFileSync(raw, text, "utf8");
  if (structuredOut && result.structured) {
    writeFileSync(structuredOut, JSON.stringify(result.structured, null, 2) + "\n", "utf8");
  }
  const errLines = result.toolLog.length ? ["tool log:", ...result.toolLog] : [];
  writeFileSync(errFile, errLines.join("\n") + (errLines.length ? "\n" : ""), "utf8");
  process.exit(0);
}

main().catch((e) => {
  process.stderr.write(`realtime-trace fatal: ${e?.message || e}\n`);
  process.exit(1);
});
