#!/usr/bin/env node
// gpt-realtime-2 websearch: alpha/web_run host-side search/open + pointer submit.
// Default mode is "swarm" (parallel fanout + deepen facets). Exa backend and the
// ensemble mode are retired; alpha is the only supported backend.
import { readFileSync, writeFileSync } from "node:fs";
import { RtAgentSession, RealtimeError } from "./lib/rt-agent-session.mjs";
import {
  askStructured,
  realtimeReasoningConfig,
  DEFAULT_SUBMIT_REASONING_EFFORT,
} from "./lib/realtime_client.mjs";
import {
  createWebsearchContext,
  hostFanoutSearch,
  hostOpenTopUrls,
  pruneFetchLogForSubmit,
} from "./lib/rt-web-run-tools.mjs";
import {
  flushFrames,
  logFrame,
} from "./lib/rt-session-utils.mjs";
import {
  SUBMIT_WEBSEARCH_POINTER_NAME,
  validateWebsearchPointer,
  websearchPointerSchema,
} from "./lib/websearch-schema.mjs";
import {
  buildFetchIndex,
  renderWebsearchWire,
} from "./lib/rt-rehydrate-websearch.mjs";
import { websearchPointerSubmitRules } from "./lib/explore-output-prompt.mjs";
import { buildWebsearchSubmitPacket } from "./websearch-lib.mjs";
import { rehydrateWebsearchWire } from "./lib/rehydrate-explore-wire.mjs";
import { daemonAsk, daemonAskBatch, daemonEnabled, warmDaemonPool } from "./lib/daemon-client.mjs";

const WS_BACKEND = (process.env.EXPLORE_WS_BACKEND || "alpha").toLowerCase();

const SUBMIT_SYSTEM = [
  "You synthesize an external research report from fetched evidence.",
  `You MUST call ${SUBMIT_WEBSEARCH_POINTER_NAME} exactly once.`,
  websearchPointerSubmitRules(),
].join("\n\n");

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
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

const DEFAULT_TIMEOUT = Number(process.env.EXPLORE_WS_TIMEOUT) || 600;
const SUBMIT_REASK = envBool("EXPLORE_WS_SUBMIT_REASK", true);
const SUBMIT_PACKET_MAX = envInt("EXPLORE_WS_SUBMIT_PACKET_MAX", 45_000);
// Swarm gathers a deeper candidate pool (fanout + deepen facets → ~45 URLs).
// Open cap 18 matches fanout-class latency (~20s wall); a cap-24 experiment
// raised wall to 22-27s with no QI/citation gain, so 18 is the tuned default.
const SWARM_OPEN_CAP = envInt("EXPLORE_WS_SWARM_OPEN_CAP", 18);
const SUBMIT_AUTHORITY_GATE = envBool("EXPLORE_WS_SUBMIT_AUTHORITY_GATE", true);

// Daemon-pool source scoring (mirrors search-fast.mjs): score each fetched page
// 0-10 for goal relevance in parallel across a warm mini pool, keep pages >=
// floor, and synthesize the report from only the top-ranked sources. Replaces
// the monolithic submit that ingested all ~45 pages in one slow turn.
const WS_DAEMON = envBool("EXPLORE_WS_DAEMON", true) && daemonEnabled();
const WS_SCORER_MODEL = (process.env.EXPLORE_WS_SCORER_MODEL || "gpt-realtime-mini").trim();
const WS_SYNTH_MODEL = (process.env.EXPLORE_WS_SYNTH_MODEL || "gpt-realtime-2").trim();
const WS_SCORE_MIN = envInt("EXPLORE_WS_SCORE_MIN", 4);
const WS_SYNTH_MAX_SOURCES = envInt("EXPLORE_WS_SYNTH_MAX_SOURCES", 24);
const WS_NAMESPACE = "websearch";
const WS_SCORE_EXCERPT_MAX = envInt("EXPLORE_WS_SCORE_EXCERPT_MAX", 1200);

const WS_SCORE_INSTRUCTIONS = [
  "You are a research-source relevance scorer. You will be given a research GOAL and exactly one fetched web SOURCE (title + excerpt).",
  "Decide how directly this SOURCE helps answer the GOAL, then return a single integer 0-10 via the score tool.",
  "Use this exact scale:",
  "0-1: unrelated (the source is about something else).",
  "2-3: same general topic but does not substantively address the goal (tangential mention, navigation, marketing).",
  "4-5: useful supporting context (background, adjacent technique, partial coverage).",
  "6-7: directly on-topic evidence (covers a core aspect of the goal with specifics).",
  "8-10: authoritative primary evidence (spec, paper, reference implementation, or detailed writeup that directly answers the goal).",
  "Judge ONLY this source's own content. Reward primary/authoritative sources; do not reward a page just for repeating the goal's keywords.",
  "Score conservatively and consistently. Return only the integer score.",
].join("\n");

const WS_SCORE_SCHEMA = {
  type: "object",
  properties: { score: { type: "integer", description: "0-10 goal relevance: 0-1 unrelated, 4-5 supporting, 6-7 on-topic, 8-10 authoritative primary evidence" } },
  required: ["score"],
  additionalProperties: false,
};

function wsScorePromptFor(goal, entry) {
  const excerpt = (entry.excerpts && entry.excerpts.length ? entry.excerpts.join(" ") : entry.text || "").slice(0, WS_SCORE_EXCERPT_MAX);
  return [
    "GOAL:",
    goal,
    "",
    `SOURCE: ${entry.url}${entry.title ? ` (${entry.title})` : ""}`,
    "EXCERPT:",
    excerpt || "(no excerpt)",
    "",
    "How directly does this SOURCE help answer the GOAL? Return the integer score 0-10 now.",
  ].join("\n");
}

// Score every fetched page in parallel across the warm mini pool, keep pages
// scoring >= floor (best first), and cap to the synthesis source budget. Each
// kept entry retains its original fetchIndex so citation indices stay valid.
// Fail-open: returns the input unchanged if the daemon path is disabled or every
// score failed (the daemon is never on the correctness path).
async function scoreAndRankSources(goal, fetchLog, { debug = false } = {}) {
  if (!WS_DAEMON || !fetchLog.length) return { ranked: fetchLog, scored: false };
  const t0 = Date.now();
  const scores = await daemonAskBatch(
    WS_NAMESPACE,
    fetchLog.map((e) => ({
      system: WS_SCORE_INSTRUCTIONS,
      user: wsScorePromptFor(goal, e),
      schema: WS_SCORE_SCHEMA,
      schemaName: "score",
    })),
    { model: WS_SCORER_MODEL },
  );
  if (scores == null) return { ranked: fetchLog, scored: false };
  const anyScored = scores.some((s) => s && typeof s.score === "number");
  if (!anyScored) return { ranked: fetchLog, scored: false };
  const ranked = fetchLog
    .map((e, i) => ({ e, score: scores[i] && typeof scores[i].score === "number" ? scores[i].score : -1 }))
    .filter((x) => x.score >= WS_SCORE_MIN)
    .sort((a, b) => b.score - a.score)
    .slice(0, WS_SYNTH_MAX_SOURCES)
    .map((x) => x.e);
  if (debug) {
    process.stderr.write(`phase score_ms=${Date.now() - t0} scorer=${WS_SCORER_MODEL} kept=${ranked.length}/${fetchLog.length} floor=${WS_SCORE_MIN}\n`);
  }
  // If the floor pruned everything (all low), fall back to the top-K by score so
  // synthesis still has material rather than an empty report.
  if (!ranked.length) {
    const byScore = fetchLog
      .map((e, i) => ({ e, score: scores[i] && typeof scores[i].score === "number" ? scores[i].score : -1 }))
      .sort((a, b) => b.score - a.score)
      .slice(0, Math.min(WS_SYNTH_MAX_SOURCES, fetchLog.length))
      .map((x) => x.e);
    return { ranked: byScore, scored: true };
  }
  return { ranked, scored: true };
}

// F1 strategies: each explores a distinct source class so the merged candidate
// pool spans papers, repos/docs, production systems and standards in parallel.
function buildFanoutStrategies(goal) {
  const g = String(goal || "").slice(0, 400);
  return [
    { label: "papers", queries: [
      { q: g, domains: ["arxiv.org"] },
      { q: g, domains: ["openreview.net"] },
      { q: `${g} benchmark evaluation`, domains: ["aclanthology.org"] },
      { q: `${g} system latency throughput`, domains: ["usenix.org"] },
    ] },
    { label: "repos-docs", queries: [
      { q: g, domains: ["github.com"] },
      { q: `${g} reference implementation`, domains: ["github.com"] },
      { q: `${g} official documentation API guide` },
    ] },
    { label: "production", queries: [
      { q: `${g} production engineering at scale` },
      { q: `${g} case study architecture design` },
    ] },
    { label: "standards", queries: [
      { q: `${g} specification standard protocol RFC` },
    ] },
  ];
}

// Deepen facets: goal-derived aspect coverage that complements the source-class
// fanout above. Where fanout diversifies WHERE it looks (papers/repos/standards),
// these diversify WHAT aspect of the goal is probed (benchmarks, limits, how it
// works, recency), so the merged pool covers sub-topics a single broad query
// misses. Goal-derived (not query-specific) so they generalize to any goal. The
// swarm fires these concurrently alongside the fanout strategies.
function buildDeepenFacets(goal) {
  const g = String(goal || "").slice(0, 400);
  return [
    { label: "deepen:benchmarks", queries: [{ q: `${g} benchmark evaluation results comparison` }] },
    { label: "deepen:limitations", queries: [{ q: `${g} limitations failure modes tradeoffs criticism` }] },
    { label: "deepen:implementation", queries: [{ q: `${g} implementation details how it works internals` }] },
    { label: "deepen:recency", queries: [{ q: `${g} 2025 2026 recent advances state of the art` }] },
  ];
}
const SUBMIT_REASONING =
  process.env.EXPLORE_WS_SUBMIT_REASONING_EFFORT
  || process.env.EXPLORE_WS_REASONING_EFFORT
  || DEFAULT_SUBMIT_REASONING_EFFORT;

async function runPointerSubmitPhase(conn, {
  submitPacket, fetchLog, deadlineMs, framesPath, reask,
}) {
  const onSend = (obj) => logFrame(framesPath, "send", obj);
  const onRecv = (obj) => logFrame(framesPath, "recv", obj);
  let lastError = null;
  let userText = submitPacket;
  const schema = websearchPointerSchema({ fetchLog });

  for (let attempt = 0; attempt <= (reask ? 1 : 0); attempt += 1) {
    if (Date.now() >= deadlineMs) throw new RealtimeError("submit phase timed out");

    let parsed;
    try {
      parsed = await askStructured(conn, {
        system: SUBMIT_SYSTEM,
        user: userText,
        schema,
        schemaName: SUBMIT_WEBSEARCH_POINTER_NAME,
        deadlineMs,
        onSend,
        onRecv,
        reasoningEffort: SUBMIT_REASONING,
      });
    } catch (e) {
      if (attempt < (reask ? 1 : 0)) {
        lastError = e.message;
        userText = `${submitPacket}\n\nPREVIOUS SUBMIT FAILED: ${e.message}\nFix and call ${SUBMIT_WEBSEARCH_POINTER_NAME} again.`;
        continue;
      }
      throw e;
    }

    const err = validateWebsearchPointer(parsed, fetchLog);
    if (err) {
      lastError = err;
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nVALIDATION FAILED: ${err}\nFix citation_refs and call ${SUBMIT_WEBSEARCH_POINTER_NAME} again.`;
        continue;
      }
      throw new RealtimeError(`pointer submit validation failed: ${err}`);
    }

    return renderWebsearchWire(parsed, fetchLog);
  }
  throw new RealtimeError(lastError || "pointer submit failed");
}

// Daemon synthesis: run the pointer submit over the warm full-model pool with
// reasoning OMITTED ("none"), reusing the same validate + reask loop. Returns
// wire text on success or null to signal fail-open to the session submit. The
// daemon is never on the correctness path.
async function runDaemonPointerSubmit({ submitPacket, fetchLog, reask, debug = false }) {
  const schema = websearchPointerSchema({ fetchLog });
  let userText = submitPacket;
  const t0 = Date.now();
  for (let attempt = 0; attempt <= (reask ? 1 : 0); attempt += 1) {
    const parsed = await daemonAsk(
      WS_NAMESPACE,
      {
        system: SUBMIT_SYSTEM,
        user: userText,
        schema,
        schemaName: SUBMIT_WEBSEARCH_POINTER_NAME,
        reasoningEffort: "none",
      },
      { model: WS_SYNTH_MODEL },
    );
    if (!parsed) return null; // daemon miss -> fall back to session submit
    const err = validateWebsearchPointer(parsed, fetchLog);
    if (err) {
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nVALIDATION FAILED: ${err}\nFix citation_refs and call ${SUBMIT_WEBSEARCH_POINTER_NAME} again.`;
        continue;
      }
      if (debug) process.stderr.write(`phase synth_daemon_invalid=${err}\n`);
      return null; // validation failed after reask -> fall back
    }
    if (debug) process.stderr.write(`phase synth_ms=${Date.now() - t0} synth=${WS_SYNTH_MODEL}\n`);
    return renderWebsearchWire(parsed, fetchLog);
  }
  return null;
}

async function runPointerReplay(replayPath, fetchLogFixture) {
  const lines = readFileSync(replayPath, "utf8").trim().split("\n").filter(Boolean);
  let argsJson = null;
  for (const line of lines) {
    let env;
    try {
      env = JSON.parse(line);
    } catch {
      continue;
    }
    const event = env.event || env;
    if (event?.type === "response.function_call_arguments.done" && event.name === SUBMIT_WEBSEARCH_POINTER_NAME) {
      argsJson = event.arguments;
    }
    if (event?.type === "response.done" || event?.type === "response.completed") {
      const resp = event.response || event;
      const output = Array.isArray(resp.output) ? resp.output : [];
      for (const item of output) {
        if (item?.type === "function_call" && item.name === SUBMIT_WEBSEARCH_POINTER_NAME && item.arguments) {
          argsJson = typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments);
        }
      }
    }
  }
  if (!argsJson) throw new RealtimeError("replay missing submit_websearch_pointer arguments");
  const pointer = JSON.parse(argsJson);
  const err = validateWebsearchPointer(pointer, fetchLogFixture);
  if (err) throw new RealtimeError(`replay validation failed: ${err}`);
  return renderWebsearchWire(pointer, fetchLogFixture);
}

function fixtureFetchLog() {
  return [
    {
      fetchIndex: 0,
      url: "https://modelcontextprotocol.io/spec",
      title: "MCP Spec",
      text: "Model Context Protocol (MCP) is an open protocol that enables seamless integration between LLM applications and external data sources and tools.",
      excerpts: [
        "Model Context Protocol (MCP) is an open protocol that enables seamless integration between LLM applications and external data sources and tools.",
      ],
    },
    {
      fetchIndex: 1,
      url: "https://github.com/modelcontextprotocol/servers",
      title: "MCP Servers",
      text: "Reference implementations for the Model Context Protocol (MCP).",
      excerpts: ["Reference implementations for the Model Context Protocol (MCP)."],
    },
  ];
}

// Swarm is the sole alpha fetch mode. It combines fanout's source-class breadth
// with deepen's aspect coverage in ONE concurrent search wave — every fanout
// strategy and every deepen facet fired together through hostFanoutSearch's
// Promise.all — then a single ranked open pass. This reaches deepen-class
// coverage at fanout-class latency: deepen's two sequential search+open rounds
// collapse into one parallel burst, so wall time is bounded by the slowest single
// search, not the sum of rounds. "Multi-agent, multi-deepen": N concurrent search
// agents, each blind to the others, merged into one shared fetchLog. The older
// fanout/deepen/search-open/search-only/combined modes are retired — swarm beat
// them on depth (45 URLs) at equal-or-better latency (docs/benchmarks/websearch-swarm.md).
async function runAlphaWebsearch({ goal, authPath, ctx }) {
  const searchStart = Date.now();
  // Warm the daemon scorer pool concurrently with the host fanout so the per-page
  // scoring batch never pays a connect+handshake. Fire-and-forget; fail-open.
  if (WS_DAEMON) {
    warmDaemonPool(WS_NAMESPACE, undefined, { model: WS_SCORER_MODEL }).catch(() => {});
    if (WS_SYNTH_MODEL !== WS_SCORER_MODEL) {
      warmDaemonPool(WS_NAMESPACE, undefined, { model: WS_SYNTH_MODEL }).catch(() => {});
    }
  }
  await hostFanoutSearch(ctx, {
    authPathOverride: authPath,
    strategies: [...buildFanoutStrategies(goal), ...buildDeepenFacets(goal)],
  });
  const searchMs = Date.now() - searchStart;
  if (!ctx.fetchLog.length) throw new RealtimeError("swarm search produced empty fetch log");
  const fetchStart = Date.now();
  try {
    await hostOpenTopUrls(ctx, { authPathOverride: authPath, cap: SWARM_OPEN_CAP, query: goal });
  } catch (e) {
    process.stderr.write(`swarm open phase failed (continuing with snippets): ${e.message}\n`);
  }
  return { searchMs, fetchMs: Date.now() - fetchStart };
}

async function runWebsearch({
  submitInstructions, goal, model, authPath,
  timeoutSec, framesPath, replayPath, hydrate, backend = WS_BACKEND,
}) {
  const ctx = createWebsearchContext();

  if (replayPath) {
    const fetchLog = fixtureFetchLog();
    const wire = await runPointerReplay(replayPath, fetchLog);
    const text = hydrate ? rehydrateWebsearchWire(wire) : wire;
    return { text, wire, toolLog: ["replay submit_websearch_pointer"] };
  }

  const session = new RtAgentSession({ model, authPath, framesPath });
  await session.connect();
  const deadlineMs = Date.now() + timeoutSec * 1000;

  try {
    let searchMs;
    let fetchMs;
    // Alpha is the only supported backend. The exa RT backend is retired — the
    // native alpha arms beat it on judged quality and breadth (see
    // docs/benchmarks/websearch-frontier.md). The separate websearch-gemini path
    // still uses Exa MCP; that is unrelated to this backend.
    if (backend === "alpha") {
      ({ searchMs, fetchMs } = await runAlphaWebsearch({ goal, authPath, ctx }));
    } else {
      throw new RealtimeError(`unsupported EXPLORE_WS_BACKEND: ${backend} (only 'alpha' is supported; 'exa' is retired)`);
    }

    // Authority-gate the submit candidate set: keep opened pages and
    // high-authority sources, drop low-authority snippet-only entries that
    // otherwise leak into citations as noise.
    if (SUBMIT_AUTHORITY_GATE) {
      pruneFetchLogForSubmit(ctx);
    }

    // Score each fetched page 0-10 for goal relevance in parallel across the warm
    // mini pool, keep pages >= floor (best first), and synthesize from only the
    // top-ranked sources. Fail-open: scoreAndRankSources returns the pruned log
    // unchanged when the daemon path is unavailable. fetchIndex is preserved on
    // each entry so citation indices stay valid against the FULL fetchLog.
    let scoreMs = 0;
    const scoreStart = Date.now();
    const { ranked: synthSources, scored } = await scoreAndRankSources(goal, ctx.fetchLog, { debug: true });
    scoreMs = Date.now() - scoreStart;

    // Swarm runs every search/open over HTTP while the RT socket sits idle with no
    // reader. The WebSocket pong is only emitted from the frame read loop
    // (realtime_client.mjs), so server pings during that idle window go unanswered
    // and the server idle-closes the socket. session.alive only flips false on
    // explicit close() — never on a silent idle-close — so ensureAlive() is a
    // stale no-op here. Reconnect unconditionally before submit: the submit phase
    // reconfigures the session from scratch (session.update below), so a fresh
    // socket is correct and carries no lost state.
    // Only needed for the fallback session submit; the daemon synth uses its own
    // warm pool. Defer the reconnect until we know we need it.

    const submitStart = Date.now();
    const submitPacket = buildWebsearchSubmitPacket({
      goal,
      submitInstructions,
      fetchIndex: buildFetchIndex(synthSources, { previewExcerpts: 3 }),
      maxChars: SUBMIT_PACKET_MAX,
    });

    // Synthesis: prefer the warm full-model daemon pool (reasoning omitted); fall
    // back to the session submit on any daemon miss. Citations validate/rehydrate
    // against the FULL fetchLog so indices remain stable regardless of pruning.
    let wire = null;
    if (WS_DAEMON) {
      wire = await runDaemonPointerSubmit({
        submitPacket,
        fetchLog: ctx.fetchLog,
        reask: SUBMIT_REASK,
        debug: true,
      });
    }
    if (wire == null) {
      if (backend === "alpha") {
        await session.reconnectFresh("post-host-search-idle");
      }
      const submitSession = {
        type: "session.update",
        session: {
          type: "realtime",
          instructions: SUBMIT_SYSTEM,
          output_modalities: ["text"],
          tools: [],
          ...realtimeReasoningConfig(SUBMIT_REASONING),
        },
      };
      session.prewarm(submitSession);
      wire = await runPointerSubmitPhase(session.connection, {
        submitPacket,
        fetchLog: ctx.fetchLog,
        deadlineMs,
        framesPath,
        reask: SUBMIT_REASK,
      });
    }
    const submitMs = Date.now() - submitStart;

    process.stderr.write(
      `phase search_ms=${searchMs} fetch_ms=${fetchMs} score_ms=${scoreMs} scored=${scored} synth_sources=${synthSources.length} submit_ms=${submitMs} searches=${ctx.searchCount} fetches=${ctx.fetchCount} urls_fetched=${ctx.fetchLog.length}\n`,
    );

    const text = hydrate ? rehydrateWebsearchWire(wire) : wire;
    return { text, wire, toolLog: [] };
  } finally {
    session.close();
  }
}

async function main() {
  const promptFile = argValue("--prompt-file");
  const goalArg = argValue("--goal");
  const submitPromptFile = argValue("--submit-prompt-file");
  const out = argValue("--out");
  const raw = argValue("--raw");
  const errFile = argValue("--err");
  const model = argValue("--model", process.env.EXPLORE_WS_MODEL || "gpt-realtime-2");
  const authPath = argValue("--auth-path", process.env.EXPLORE_CODEX_AUTH_PATH);
  const framesPath = argValue("--frames");
  const replayPath = argValue("--replay");
  const timeoutSec = Number(argValue("--timeout", String(DEFAULT_TIMEOUT)));
  const hydrate = argValue("--hydrate", process.env.EXPLORE_WS_HYDRATE || "0") === "1";

  if (!promptFile || !out || !raw || !errFile) {
    process.stderr.write(
      "usage: realtime-websearch.mjs --prompt-file --workspace --out --raw --err [--submit-prompt-file] [--goal] [--replay PATH] [--hydrate 1]\n",
    );
    process.exit(2);
  }

  const promptText = readFileSync(promptFile, "utf8");
  const submitInstructions = submitPromptFile ? readFileSync(submitPromptFile, "utf8") : "";
  const goal = goalArg || promptText.match(/GOAL:\s*(.+)/i)?.[1]?.trim() || promptText.trim();

  let result;
  try {
    result = await runWebsearch({
      submitInstructions,
      goal,
      model,
      authPath,
      timeoutSec,
      framesPath,
      replayPath,
      hydrate,
    });
  } catch (err) {
    const msg = err instanceof RealtimeError ? err.message : String(err.message || err);
    writeFileSync(errFile, `${msg}\n`, { flag: "a" });
    process.stderr.write(`${msg}\n`);
    process.exit(1);
  }

  writeFileSync(raw, result.wire || result.text, "utf8");
  writeFileSync(out, result.text, "utf8");
  if (result.toolLog.length) {
    writeFileSync(errFile, `${result.toolLog.join("\n")}\n`, { flag: "a" });
  }
}

main();
