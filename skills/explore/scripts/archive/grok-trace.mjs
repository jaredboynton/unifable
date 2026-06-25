#!/usr/bin/env node
// Two-phase grok-build-0.1 trace: explore (read tools) then native json_schema submit.
import { readFileSync, writeFileSync } from "node:fs";
import {
  createResponse,
  extractFunctionCalls,
  extractOutputText,
  parseJsonFromResponse,
  GrokError,
  flushFrames,
  logFrame,
  DEFAULT_MODEL,
} from "./lib/xai_client.mjs";
import { TOOL_SCHEMAS, dispatchTool, parseArguments } from "./lib/gk-tools.mjs";
import {
  traceProviderSchema,
  validateTraceObject,
  applyGroundingManifest,
  normalizeReadPath,
} from "./lib/trace-schema.mjs";
import { renderTraceStructured } from "./lib/render-trace-structured.mjs";
import {
  lintExploreWire,
  parseExploreWire,
  validateTraceWire,
} from "./lib/explore-wire-format.mjs";
import { traceGkWireSubmitRules } from "./lib/explore-output-prompt.mjs";

const EXPLORE_SYSTEM = [
  "You are a codebase exploration assistant operating in read-only mode.",
  "Gather ground truth for the question — read load-bearing files only, not the whole repo.",
  "",
  "Workflow: grep/list_dir to locate, then read_file entry points and direct callees.",
  "After at most two grep/list_dir turns, call read_file on the load-bearing files you found.",
  "You must read at least one file before stopping; prefer 4-8 files for a complete trace.",
  "Follow imports and exec/spawn under lib/ when they affect the answer.",
  "",
  "Stop after roughly 8-12 read_file calls once the main chain is clear.",
  "Skip tests, benchmarks, and tangential helpers unless the question requires them.",
  "Batch multiple read_file calls per turn when possible.",
  "",
  "Do NOT write the final answer yet. Only explore with tools.",
  "Never invent paths, functions, or behavior.",
  "Do not call trace.sh, trace-gemini.sh, trace-rt.sh, trace-gk.sh, or any explore wrapper recursively.",
].join("\n");

const WIRE_SUBMIT_SYSTEM = [
  "You synthesize a codebase trace from exploration evidence.",
  traceGkWireSubmitRules(),
].join("\n\n");

const SUBMIT_SYSTEM = [
  "You synthesize a structured codebase trace from exploration evidence.",
  "Return JSON matching the provided schema exactly.",
  "",
  "Rules:",
  "- Be concise: opening_summary <= 120 words; each section body <= 100 words.",
  "- At most 5 code_passages; each span <= 40 lines.",
  "- Ground every claim in the explore tool log and read excerpts provided.",
  "- Every code_passage.file_path MUST correspond to a file listed under FILES READ DURING EXPLORE.",
  "- Every code_passage.file_path MUST be one of the schema enum values for files read during explore.",
  "- Never use repo-map, grep-only, list_dir-only, or search-only paths in code_passages.",
  "- When the question contrasts tools, modes, or code paths, comparison_tables MUST be non-empty.",
  "- Include one section per major script/module (not every file read).",
  "- flow_steps: 4-8 short pipeline strings.",
  "- Use empty string or empty arrays only for truly unused optional fields.",
].join("\n");

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

const DEFAULT_TIMEOUT = envFloat("EXPLORE_GK_TIMEOUT", 300);
const DEFAULT_EXPLORE_MAX_TURNS = envInt("EXPLORE_GK_EXPLORE_MAX_TURNS", 6);
const SUBMIT_REASK = envBool("EXPLORE_GK_SUBMIT_REASK", true);
const SUBMIT_PACKET_MAX = envInt("EXPLORE_GK_SUBMIT_PACKET_MAX", 80_000);
const EXPLORE_MAX_READS = envInt("EXPLORE_GK_EXPLORE_MAX_READS", 14);
const EXPLORE_MIN_READS = envInt("EXPLORE_GK_EXPLORE_MIN_READS", 4);
const READ_EXCERPT_MAX = envInt("EXPLORE_GK_READ_EXCERPT_MAX", 2500);
const SUBMIT_EXCERPT_FILES = envInt("EXPLORE_GK_SUBMIT_EXCERPT_FILES", 12);
const BASE_URL = process.env.EXPLORE_GK_BASE_URL || "https://api.x.ai/v1";

function truncateText(text, max) {
  const s = String(text || "");
  if (s.length <= max) return s;
  return s.slice(0, max) + `\n... [truncated ${s.length - max} chars]`;
}

function trackRead(workspace, filesRead, readCache, args, result) {
  if (args?.path && result?.ok && result?.content != null) {
    const rel = normalizeReadPath(workspace, args.path);
    if (rel) {
      filesRead.add(rel);
      readCache.set(rel, truncateText(result.content, READ_EXCERPT_MAX));
      return rel;
    }
  }
  return null;
}

function extractQuestion(prompt) {
  const marker = "QUESTION:";
  const idx = prompt.lastIndexOf(marker);
  if (idx === -1) return prompt.trim();
  return prompt.slice(idx + marker.length).trim();
}

function extractMapBlock(prompt) {
  const start = prompt.indexOf("REPO MAP");
  const q = prompt.indexOf("QUESTION:");
  if (start === -1 || q === -1 || q <= start) return "";
  return prompt.slice(start, q).replace(/^REPO MAP[^\n]*\n?/, "").trim();
}

function buildSubmitPacket({ question, mapBlock, submitInstructions, filesRead, readCache, toolLog, toolResults, wire = false }) {
  const readFiles = [...filesRead].sort();
  const parts = [
    "ORIGINAL QUESTION:",
    question,
    "",
  ];
  if (mapBlock) {
    parts.push(
      "REPO MAP (prefetch; useful for orientation, not citable in code_passages unless also listed below):",
      mapBlock,
      "",
    );
  }
  parts.push(
    "FILES READ DURING EXPLORE:",
    readFiles.join("\n") || "(none)",
    "",
    "CODE_PASSAGES FILE_PATH ENUM:",
    readFiles.join("\n") || "(none)",
    "",
    "Every code_passage.file_path must correspond to a file listed under FILES READ DURING EXPLORE.",
    "Only the CODE_PASSAGES FILE_PATH ENUM values may appear in code_passages[].file_path.",
    "A path seen only in REPO MAP, grep/list_dir/codebase_search output, or TOOL RESULT SNIPPETS is not a valid code_passage file_path.",
    "",
    "TOOL LOG:",
    toolLog.filter((l) => !l.startsWith("phase ")).join("\n") || "(none)",
    "",
    "READ EXCERPTS:",
  );
  const excerptEntries = [...readCache.entries()].slice(0, SUBMIT_EXCERPT_FILES);
  for (const [path, excerpt] of excerptEntries) {
    parts.push(`--- ${path} ---`, excerpt, "");
  }
  if (readCache.size > excerptEntries.length) {
    parts.push(`... (${readCache.size - excerptEntries.length} more files read, omitted from excerpts)`, "");
  }
  parts.push("TOOL RESULT SNIPPETS:");
  for (const tr of toolResults.slice(-20)) {
    parts.push(`[${tr.tool}] ${JSON.stringify(tr.args).slice(0, 80)}`, tr.result, "");
  }
  if (submitInstructions) parts.push("SUBMIT INSTRUCTIONS:", submitInstructions, "");
  if (wire) {
    parts.push(
      "Return the complete wire plaintext trace (SECTION blocks and <file:...> tokens only).",
      "Every <file:...> path must be copied exactly from CODE_PASSAGES FILE_PATH ENUM.",
    );
  } else {
    parts.push(
      "Return the complete structured trace JSON.",
      "Every code_passage.file_path must be copied exactly from CODE_PASSAGES FILE_PATH ENUM.",
    );
  }
  return truncateText(parts.join("\n"), SUBMIT_PACKET_MAX);
}

async function runExplorePhase({
  prompt, workspace, model, deadlineMs, maxTurns, framesPath, filesRead, readCache, toolLog, toolResults,
  createFn, exploreResponses, replayMode = false,
}) {
  let previousId = null;
  let nudgeCount = 0;
  let toolTurnCount = 0;

  async function nextResponse({ input, instructions, tools = TOOL_SCHEMAS, includeInstructions = false } = {}) {
    if (input == null) {
      throw new GrokError("xAI responses.create requires input");
    }
    if (exploreResponses?.length) {
      return exploreResponses.shift();
    }
    if (replayMode) {
      return {
        id: `replay_done_${Date.now()}`,
        output: [{ type: "message", content: [{ type: "output_text", text: "" }] }],
      };
    }
    const remaining = Math.max(5, Math.ceil((deadlineMs - Date.now()) / 1000));
    return createFn({
      model,
      baseUrl: BASE_URL,
      instructions: includeInstructions ? instructions : undefined,
      input,
      tools,
      previousResponseId: previousId || undefined,
      timeoutSec: remaining,
      framesPath,
    });
  }

  let response = await nextResponse({
    input: [{ role: "user", content: prompt }],
    instructions: EXPLORE_SYSTEM,
    includeInstructions: true,
  });
  previousId = response.id;

  for (let turn = 0; turn < maxTurns; turn++) {
    if (Date.now() >= deadlineMs) throw new GrokError("explore phase timed out");
    if (filesRead.size >= EXPLORE_MAX_READS) break;

    const functionCalls = extractFunctionCalls(response);
    const turnText = extractOutputText(response);

    if (!functionCalls.length) {
      if (filesRead.size >= EXPLORE_MIN_READS) break;
      if (turnText && nudgeCount < 1 && !replayMode && !(exploreResponses && exploreResponses.length)) {
        nudgeCount++;
        response = await nextResponse({
          input: [{ role: "user", content: "Read a few more load-bearing files with tools, then stop." }],
          instructions: EXPLORE_SYSTEM,
          includeInstructions: !previousId,
        });
        previousId = response.id;
        continue;
      }
      break;
    }

    const outputs = [];
    let readsThisTurn = 0;
    for (const call of functionCalls) {
      const args = parseArguments(call.arguments);
      const result = dispatchTool(call.name, args, workspace);
      if (trackRead(workspace, filesRead, readCache, args, result)) readsThisTurn++;
      toolLog.push(`${call.name} ${JSON.stringify(args).slice(0, 120)} -> ok=${result.ok}`);
      toolResults.push({ tool: call.name, args, result: truncateText(JSON.stringify(result), 1500) });
      outputs.push({
        type: "function_call_output",
        call_id: call.call_id,
        output: JSON.stringify(result),
      });
    }
    toolTurnCount += functionCalls.length;

    const nextTools = filesRead.size === 0 && turn >= 1
      ? TOOL_SCHEMAS.filter((tool) => tool.name === "read_file")
      : TOOL_SCHEMAS;
    if (filesRead.size === 0 && turn >= 1 && readsThisTurn === 0) {
      outputs.push({
        role: "user",
        content: "You have only searched or listed files. Next, call read_file on the load-bearing files needed to answer the question.",
      });
      toolLog.push("nudge require_read_file_before_submit");
    }
    response = await nextResponse({ input: outputs, tools: nextTools });
    previousId = response.id;

    if (filesRead.size >= EXPLORE_MAX_READS) break;
  }

  if (filesRead.size === 0) {
    throw new GrokError("explore phase read no files; cannot submit grounded code_passages");
  }

  return toolTurnCount;
}

async function runSubmitPhase({
  submitPacket, workspace, model, deadlineMs, framesPath, filesRead, toolTurns, reask, createFn, submitResponse,
}) {
  const schema = traceProviderSchema({ allowedCodePassagePaths: [...filesRead].sort() });
  const textFormat = {
    type: "json_schema",
    name: "submit_trace",
    schema,
    strict: true,
  };

  let userText = submitPacket;
  let lastError = null;

  for (let attempt = 0; attempt <= (reask ? 1 : 0); attempt++) {
    if (Date.now() >= deadlineMs) throw new GrokError("submit phase timed out");
    const remaining = Math.max(5, Math.ceil((deadlineMs - Date.now()) / 1000));

    let response;
    if (submitResponse && attempt === 0) {
      response = submitResponse;
    } else {
      response = await createFn({
        model,
        baseUrl: BASE_URL,
        instructions: SUBMIT_SYSTEM,
        input: [{ role: "user", content: userText }],
        textFormat,
        timeoutSec: remaining,
        framesPath,
      });
    }

    let parsed;
    try {
      parsed = parseJsonFromResponse(response);
    } catch (e) {
      lastError = e.message;
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nPREVIOUS SUBMIT FAILED: ${e.message}\nFix and return valid JSON.`;
        continue;
      }
      throw e;
    }

    parsed = applyGroundingManifest(parsed, filesRead, toolTurns);
    const err = validateTraceObject(parsed, { workspace, filesRead, toolTurns });
    if (err) {
      lastError = err;
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nVALIDATION FAILED: ${err}\nFix grounding and return valid JSON.`;
        continue;
      }
      throw new GrokError(`structured trace validation failed: ${err}`);
    }
    return parsed;
  }
  throw new GrokError(lastError || "structured submit failed");
}

async function runWireSubmitPhase({
  submitPacket, workspace, model, deadlineMs, framesPath, filesRead, reask, createFn, submitResponse,
}) {
  let userText = submitPacket;
  let lastError = null;

  for (let attempt = 0; attempt <= (reask ? 1 : 0); attempt += 1) {
    if (Date.now() >= deadlineMs) throw new GrokError("wire submit phase timed out");
    const remaining = Math.max(5, Math.ceil((deadlineMs - Date.now()) / 1000));

    let response;
    if (submitResponse && attempt === 0) {
      response = submitResponse;
    } else {
      response = await createFn({
        model,
        baseUrl: BASE_URL,
        instructions: WIRE_SUBMIT_SYSTEM,
        input: [{ role: "user", content: userText }],
        timeoutSec: remaining,
        framesPath,
      });
    }

    const text = extractOutputText(response).trim();
    if (!text) {
      lastError = "empty wire submit response";
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nPREVIOUS SUBMIT FAILED: empty response\nReturn wire plaintext.`;
        continue;
      }
      throw new GrokError(lastError);
    }

    const lint = lintExploreWire(text);
    const parsed = parseExploreWire(text);
    const validation = validateTraceWire(parsed, workspace, { allowedPaths: [...filesRead] });
    if (!validation.ok) {
      lastError = validation.errors.join("; ");
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nVALIDATION FAILED: ${lastError}\nFix grounding and return wire plaintext.`;
        continue;
      }
      throw new GrokError(`wire trace validation failed: ${lastError}`);
    }
    if (!lint.ok) {
      lastError = lint.issues.join("; ");
      if (attempt < (reask ? 1 : 0)) {
        userText = `${submitPacket}\n\nFORMAT FAILED: ${lastError}\nUse wire plaintext only (no markdown).`;
        continue;
      }
    }
    return text.endsWith("\n") ? text : `${text}\n`;
  }
  throw new GrokError(lastError || "wire submit failed");
}

async function runStructuredTrace({
  explorePrompt, submitInstructions, question, workspace, model, timeoutSec, exploreMaxTurns, framesPath, replay,
  createFn = createResponse,
}) {
  const toolLog = [];
  const toolResults = [];
  const filesRead = new Set();
  const readCache = new Map();
  const deadlineMs = Date.now() + timeoutSec * 1000;

  let exploreResponses = null;
  let submitResponse = null;
  const replayMode = Boolean(replay);
  if (replay) {
    const fixture = JSON.parse(readFileSync(replay, "utf8"));
    exploreResponses = [...(fixture.explore_responses || [])];
    submitResponse = fixture.submit_response || null;
  }

  try {
    const exploreStart = Date.now();
    const toolTurnCount = await runExplorePhase({
      prompt: explorePrompt,
      workspace,
      model,
      deadlineMs,
      maxTurns: exploreMaxTurns,
      framesPath,
      filesRead,
      readCache,
      toolLog,
      toolResults,
      createFn,
      exploreResponses,
      replayMode,
    });
    toolLog.push(`phase explore_ms=${Date.now() - exploreStart} files_read=${filesRead.size}`);

    const q = question || extractQuestion(explorePrompt);
    const submitPacket = buildSubmitPacket({
      question: q,
      mapBlock: extractMapBlock(explorePrompt),
      submitInstructions,
      filesRead,
      readCache,
      toolLog,
      toolResults,
    });

    const submitStart = Date.now();
    const structured = await runSubmitPhase({
      submitPacket,
      workspace,
      model,
      deadlineMs,
      framesPath,
      filesRead,
      toolTurns: toolTurnCount,
      reask: SUBMIT_REASK,
      createFn,
      submitResponse,
    });
    toolLog.push(`phase submit_ms=${Date.now() - submitStart}`);

    const markdown = renderTraceStructured(workspace, structured);
    return { text: markdown, toolLog, structured };
  } finally {
    flushFrames(framesPath);
  }
}

async function runWireStructuredTrace({
  explorePrompt, submitInstructions, question, workspace, model, timeoutSec, exploreMaxTurns, framesPath, replay,
  createFn = createResponse,
}) {
  const toolLog = [];
  const toolResults = [];
  const filesRead = new Set();
  const readCache = new Map();
  const deadlineMs = Date.now() + timeoutSec * 1000;

  let exploreResponses = null;
  let submitResponse = null;
  const replayMode = Boolean(replay);
  if (replay) {
    const fixture = JSON.parse(readFileSync(replay, "utf8"));
    exploreResponses = [...(fixture.explore_responses || [])];
    submitResponse = fixture.submit_response || null;
  }

  try {
    const exploreStart = Date.now();
    await runExplorePhase({
      prompt: explorePrompt,
      workspace,
      model,
      deadlineMs,
      maxTurns: exploreMaxTurns,
      framesPath,
      filesRead,
      readCache,
      toolLog,
      toolResults,
      createFn,
      exploreResponses,
      replayMode,
    });
    toolLog.push(`phase explore_ms=${Date.now() - exploreStart} files_read=${filesRead.size}`);

    const q = question || extractQuestion(explorePrompt);
    const submitPacket = buildSubmitPacket({
      question: q,
      mapBlock: extractMapBlock(explorePrompt),
      submitInstructions,
      filesRead,
      readCache,
      toolLog,
      toolResults,
      wire: true,
    });

    const submitStart = Date.now();
    const wireText = await runWireSubmitPhase({
      submitPacket,
      workspace,
      model,
      deadlineMs,
      framesPath,
      filesRead,
      reask: SUBMIT_REASK,
      createFn,
      submitResponse,
    });
    toolLog.push(`phase submit_ms=${Date.now() - submitStart}`);
    return { text: wireText, toolLog };
  } finally {
    flushFrames(framesPath);
  }
}

async function main() {
  const promptFile = argValue("--prompt-file");
  const submitPromptFile = argValue("--submit-prompt-file");
  const out = argValue("--out");
  const raw = argValue("--raw");
  const structuredOut = argValue("--structured-out");
  const errFile = argValue("--err");
  const workspace = argValue("--workspace", process.cwd());
  const model = argValue("--model", DEFAULT_MODEL);
  const framesPath = argValue("--frames");
  const replayPath = argValue("--replay");
  const timeoutSec = Number(argValue("--timeout", String(DEFAULT_TIMEOUT)));
  const exploreMaxTurns = Number(argValue("--explore-max-turns", String(DEFAULT_EXPLORE_MAX_TURNS)));
  const wire = argValue("--wire", process.env.EXPLORE_WIRE_FORMAT || "0") === "1";

  if (!promptFile || !out || !raw || !errFile) {
    process.stderr.write(
      "usage: grok-trace.mjs --prompt-file --workspace --out --raw --err [--submit-prompt-file] [--structured-out] [--wire 1]\n"
    );
    process.exit(2);
  }

  const explorePrompt = readFileSync(promptFile, "utf8");
  const submitInstructions = submitPromptFile ? readFileSync(submitPromptFile, "utf8") : "";

  let result;
  try {
    if (wire) {
      result = await runWireStructuredTrace({
        explorePrompt,
        submitInstructions,
        question: extractQuestion(explorePrompt),
        workspace,
        model,
        timeoutSec,
        exploreMaxTurns,
        framesPath,
        replay: replayPath,
      });
    } else {
      result = await runStructuredTrace({
        explorePrompt,
        submitInstructions,
        question: extractQuestion(explorePrompt),
        workspace,
        model,
        timeoutSec,
        exploreMaxTurns,
        framesPath,
        replay: replayPath,
      });
    }
  } catch (e) {
    const msg = e instanceof GrokError ? e.message : (e?.message || String(e));
    writeFileSync(errFile, msg + "\n", "utf8");
    process.stderr.write(`grok-trace: ${msg}\n`);
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
  process.stderr.write(`grok-trace fatal: ${e?.message || e}\n`);
  process.exit(1);
});
