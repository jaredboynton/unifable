#!/usr/bin/env node
// Zero-dependency reimplementation of cursor-agent ask mode for the explore tracer.
// Drives Cursor AgentService/Run directly (HTTP/2 Connect + protobuf), runs the
// client-side read-only tool loop (read/grep/ls), and prints the assembled trace.
// No node_modules, no cursor-agent subprocess. See plans/isolated-cursor-harness.md.
import { readFileSync, writeFileSync, appendFileSync, existsSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { homedir } from "node:os";
import {
  encodeRunRequest, encodeHeartbeat, encodeGetBlobResult, encodeSetBlobResult,
  encodeRequestContextResult, encodeReadSuccess, encodeReadRejected,
  encodeGrepContentSuccess, encodeGrepError, encodeLsSuccess, encodeLsRejected,
  encodeShellResultSuccess, encodeShellResultRejected,
  encodeShellStreamStart, encodeShellStreamStdout, encodeShellStreamStderr,
  encodeShellStreamExit, encodeShellStreamRejected, encodeBackgroundShellRejected,
  encodeDiagnosticsResult, encodeMcpEmptyResult, encodeMcpTextSuccess, encodeRawRejectResult,
  decodeServerMessage, decodeInteractionUpdate, decodeExecServerMessage,
  decodeReadArgs, decodeGrepArgs, decodeLsArgs, decodeShellArgs, decodeMcpArgs, decodeKvServerMessage,
  decodeValue, blobKeyHex, CODEBASE_SEARCH_TOOL, BATCH_READ_TOOL,
} from "./lib/hcursor-proto.mjs";
import { openRun } from "./lib/hcursor-h2.mjs";
import { toolRead, toolGrep, toolLs, toolShell, toolCodebaseSearch, toolBatchRead } from "./lib/htools.mjs";

const DEFAULT_SYSTEM = [
  "You are a codebase tracing assistant operating in read-only ask mode.",
  "Explore the repository with the read, grep, and ls tools to answer the question from ground truth.",
  "When you need several files, call the batch_read tool ONCE with all their paths instead of calling read one file at a time -- it is much faster.",
  "Cite real code using fenced blocks whose info string is startLine:endLine:relative/path",
  "(for example ```12:34:src/foo.ts then the lines then ```). Only cite files you actually read.",
  "Structure the answer with ## Flow, ## Key files, and ## Code references sections. Be concise and correct.",
].join("\n");

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

// MCP arg map values are protobuf google.protobuf.Value bytes (proxy.ts:705).
// Primary decode is Value; fall back to JSON/raw text like decodeMcpArgValue does.
function decodeJsonArg(buf) {
  if (!buf) return undefined;
  const v = decodeValue(Buffer.from(buf));
  if (v !== undefined) return v;
  const s = Buffer.from(buf).toString("utf8");
  try { return JSON.parse(s); } catch { return s; }
}

function loadToken() {
  const env = process.env.CURSOR_AUTH_TOKEN || process.env.CURSOR_ACCESS_TOKEN;
  if (env) return env.trim();
  const authPath = join(process.env.HOME || homedir(), ".cursor", "auth.json");
  if (!existsSync(authPath)) throw new Error(`no token: set CURSOR_AUTH_TOKEN or create ${authPath}`);
  const j = JSON.parse(readFileSync(authPath, "utf8"));
  const tok = j.accessToken || j.access_token;
  if (!tok) throw new Error(`no accessToken in ${authPath}`);
  return tok;
}

// Build client replies for one server message. Returns { text, thinking, replies }
// where replies is an array of frame buffers to send back (shell streaming emits many).
// Exported so tests can drive the tool loop with synthetic exec frames.
export function handleMessage(buf, ctx) {
  const env = decodeServerMessage(buf);
  if (env.case === "interactionUpdate") {
    const u = decodeInteractionUpdate(env.value);
    if (u.case === "text") return { text: u.text };
    if (u.case === "thinking") return { thinking: u.text };
    return {};
  }
  if (env.case === "kvServerMessage") {
    const kv = decodeKvServerMessage(env.value);
    if (kv.case === "getBlobArgs") {
      const data = ctx.blobStore.get(blobKeyHex(kv.blobId)) || null;
      return { replies: [encodeGetBlobResult(kv.id, data)] };
    }
    if (kv.case === "setBlobArgs") {
      if (kv.blobId && kv.blobData) ctx.blobStore.set(blobKeyHex(kv.blobId), kv.blobData);
      return { replies: [encodeSetBlobResult(kv.id)] };
    }
    return {};
  }
  if (env.case === "execServerMessage") {
    const ex = decodeExecServerMessage(env.value);
    const { id, execId } = ex;
    switch (ex.case) {
      case "requestContextArgs":
        return { replies: [encodeRequestContextResult(id, execId, ctx.workspace, null)] };
      case "readArgs": {
        const { path } = decodeReadArgs(ex.args);
        const r = toolRead(ctx.workspace, path);
        ctx.toolLog.push(`read ${path} ${r.ok ? "ok" : "rej:" + r.reason}`);
        return { replies: [r.ok ? encodeReadSuccess(id, execId, r) : encodeReadRejected(id, execId, path, r.reason)] };
      }
      case "grepArgs": {
        const a = decodeGrepArgs(ex.args);
        const r = toolGrep(ctx.workspace, a);
        ctx.toolLog.push(`grep ${JSON.stringify(a.pattern)} ${r.ok ? r.fileMatches.length + " files" : "err:" + r.reason}`);
        return { replies: [r.ok ? encodeGrepContentSuccess(id, execId, r) : encodeGrepError(id, execId, r.reason)] };
      }
      case "lsArgs": {
        const a = decodeLsArgs(ex.args);
        const r = toolLs(ctx.workspace, a.path);
        ctx.toolLog.push(`ls ${a.path || "."} ${r.ok ? r.files.length + "f/" + r.dirs.length + "d" : "rej:" + r.reason}`);
        return { replies: [r.ok ? encodeLsSuccess(id, execId, r) : encodeLsRejected(id, execId, a.path, r.reason)] };
      }
      case "shellArgs": {
        const a = decodeShellArgs(ex.args);
        const r = toolShell(ctx.workspace, a);
        ctx.toolLog.push(`shell ${JSON.stringify((a.command || "").slice(0, 60))} ${r.ok ? "exit=" + r.exitCode : "rej:" + r.reason}`);
        if (!r.ok) return { replies: [encodeShellResultRejected(id, execId, a.command, ctx.workspace, r.reason)] };
        return { replies: [encodeShellResultSuccess(id, execId, r)] };
      }
      case "shellStreamArgs": {
        // Reject the streaming shell variant and steer the model to the native
        // read-only tools. A success reply (shell_stream events OR a plain
        // ShellResult) is accepted by the server but leaves the model
        // heartbeating without completing -- the real client's stream success
        // depends on a turn/session lifecycle this single-turn harness does not
        // replicate (verified: st2/st3 frames -> heartbeats, no text). Rejection
        // does NOT hang: the model falls back to read/grep/ls (verified: st run
        // recovered, exit 0). shell_stream is rare in real traces (<=1/run).
        const a = decodeShellArgs(ex.args);
        ctx.toolLog.push(`shell-stream rejected ${JSON.stringify((a.command || "").slice(0, 60))}`);
        return { replies: [encodeShellStreamRejected(id, execId, a.command, ctx.workspace, "streaming shell is disabled; use the read, grep, or ls tools instead")] };
      }
      case "backgroundShellSpawnArgs": {
        const a = decodeShellArgs(ex.args);
        return { replies: [encodeBackgroundShellRejected(id, execId, a.command, ctx.workspace, "background shells disabled in read-only trace")] };
      }
      case "diagnosticsArgs":
        return { replies: [encodeDiagnosticsResult(id, execId)] };
      case "mcpArgs": {
        const m = decodeMcpArgs(ex.args);
        const toolName = m.toolName || m.name;
        if (toolName === CODEBASE_SEARCH_TOOL) {
          const query = decodeJsonArg(m.args.query);
          const dirs = decodeJsonArg(m.args.target_directories);
          const r = toolCodebaseSearch(ctx.workspace, typeof query === "string" ? query : String(query || ""), Array.isArray(dirs) ? dirs : []);
          ctx.toolLog.push(`codebase_search ${JSON.stringify(String(query || "").slice(0, 60))} ${r.ok ? `ok[${r.source || "?"}] ` + r.text.length + "B" : "err:" + r.reason}`);
          return { replies: [encodeMcpTextSuccess(id, execId, r.ok ? r.text : `search unavailable: ${r.reason}`, !r.ok)] };
        }
        if (toolName === BATCH_READ_TOOL) {
          const paths = decodeJsonArg(m.args.paths);
          const list = Array.isArray(paths) ? paths : [];
          const r = toolBatchRead(ctx.workspace, list);
          ctx.toolLog.push(`batch_read ${list.length} paths ${r.ok ? "ok " + r.text.length + "B" : "err:" + r.reason}`);
          return { replies: [encodeMcpTextSuccess(id, execId, r.ok ? r.text : `batch_read failed: ${r.reason}`, !r.ok)] };
        }
        ctx.toolLog.push(`mcp ${toolName} -> empty`);
        return { replies: [encodeMcpEmptyResult(id, execId)] };
      }
      case "writeArgs": case "deleteArgs":
        // read-only enforcement: reject mutating tools (reply at their own field)
        return { replies: [encodeRawRejectResult(id, execId, ex.field)] };
      default:
        // CRITICAL: always reply to every exec request, or the server blocks the
        // whole stream (tool calls are batched). Arg field == result field, so an
        // empty result at ex.field is a safe no-op for tools we don't implement
        // (mcp-resources, fetch, record/computer-use, write-shell-stdin, etc.).
        if (ex.field) {
          ctx.toolLog.push(`empty-reply exec ${ex.case} (field ${ex.field})`);
          return { replies: [encodeRawRejectResult(id, execId, ex.field)] };
        }
        ctx.toolLog.push(`unhandled exec ${ex.case} (no field)`);
        return {};
    }
  }
  return {};
}

async function runLive({ token, model, workspace, userText, system, framesPath }) {
  const ctx = { workspace, blobStore: new Map(), toolLog: [] };
  let text = "";
  let thinking = "";
  const conn = await new Promise((resolveConn, rejectConn) => {
    let hb = null;
    const handle = openRun({
      accessToken: token,
      onMessage: (buf) => {
        if (framesPath) appendFileSync(framesPath, JSON.stringify({ dir: "recv", b64: buf.toString("base64") }) + "\n");
        const out = handleMessage(buf, ctx);
        if (out.text) text += out.text;
        if (out.thinking) thinking += out.thinking;
        for (const rep of out.replies || []) {
          if (framesPath) appendFileSync(framesPath, JSON.stringify({ dir: "send", b64: Buffer.from(rep).toString("base64") }) + "\n");
          handle.write(rep);
        }
      },
      onEnd: (trailer) => {
        if (hb) clearInterval(hb);
        handle.close();
        if (trailer && trailer.error) rejectConn(new Error(`Connect ${trailer.error.code}: ${trailer.error.message}`));
        else resolveConn();
      },
      onError: (e) => { if (hb) clearInterval(hb); rejectConn(e); },
    });
    const runBytes = encodeRunRequest({ modelId: model, systemPrompt: system, userText, blobStore: ctx.blobStore });
    if (framesPath) appendFileSync(framesPath, JSON.stringify({ dir: "send", b64: Buffer.from(runBytes).toString("base64") }) + "\n");
    handle.write(runBytes);
    hb = setInterval(() => handle.write(encodeHeartbeat()), 5000);
    hb.unref?.(); // don't let the heartbeat timer keep the process alive on its own
  });
  return { text, thinking, toolLog: ctx.toolLog };
}

function runReplay({ workspace, framesPath }) {
  const ctx = { workspace, blobStore: new Map(), toolLog: [] };
  let text = "";
  let thinking = "";
  for (const line of readFileSync(framesPath, "utf8").split("\n")) {
    if (!line.trim()) continue;
    const rec = JSON.parse(line);
    if (rec.dir !== "recv") continue;
    const out = handleMessage(Buffer.from(rec.b64, "base64"), ctx);
    if (out.text) text += out.text;
    if (out.thinking) thinking += out.thinking;
  }
  return { text, thinking, toolLog: ctx.toolLog };
}

async function main() {
  const promptFile = argValue("--prompt-file");
  const out = argValue("--out");
  const raw = argValue("--raw");
  const errFile = argValue("--err");
  const workspace = argValue("--workspace", process.cwd());
  const model = argValue("--model", process.env.UNITRACE_MODEL || "composer-2.5-fast");
  const framesPath = argValue("--frames");
  const replayDir = argValue("--replay");
  const system = argValue("--system", DEFAULT_SYSTEM);
  const userText = promptFile ? readFileSync(promptFile, "utf8") : (argValue("--prompt") || "Trace this repository.");

  // Offline smoke test: replay a bundled fixture of recorded frames with no
  // token/network. Doubles as the deterministic CI gate for the decode+tool loop.
  // --mode/--emit-trace are accepted as no-ops (streaming + stdout are the default).
  const selfTest = process.argv.includes("--self-test");

  let result;
  try {
    if (selfTest) {
      const fixture = fileURLToPath(new URL("./test/fixtures/selftest.frames.ndjson", import.meta.url));
      if (!existsSync(fixture)) throw new Error(`self-test fixture missing: ${fixture}`);
      if (framesPath) {
        const data = readFileSync(fixture, "utf8");
        writeFileSync(framesPath, data);                                  // requested path
        writeFileSync(join(dirname(framesPath), "frames.ndjson"), data);  // for --replay <dir>
      }
      result = runReplay({ workspace, framesPath: fixture });
    } else if (replayDir) {
      result = runReplay({ workspace, framesPath: join(replayDir, "frames.ndjson") });
    } else {
      const token = loadToken();
      result = await runLive({ token, model, workspace, userText, system, framesPath });
    }
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (errFile) appendFileSync(errFile, `harness error: ${msg}\n`);
    else process.stderr.write(`harness error: ${msg}\n`);
    if (out && result?.text) writeFileSync(out, result.text);
    process.exit(1);
  }

  if (raw) writeFileSync(raw, result.text);
  if (out) writeFileSync(out, result.text);
  if (errFile && result.toolLog.length) appendFileSync(errFile, "tool log:\n" + result.toolLog.join("\n") + "\n");
  if (!out) process.stdout.write(result.text + (result.text.endsWith("\n") ? "" : "\n"));
  if (!result.text) {
    if (errFile) appendFileSync(errFile, "harness: no text produced\n");
    process.exit(1);
  }
  // One-shot CLI: exit explicitly so a lingering h2 session/timer can't keep the
  // event loop alive after the stream has ended and output is flushed.
  process.exit(0);
}

// Run only when invoked directly, so the module stays importable for tests.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => { process.stderr.write(`harness fatal: ${e?.message || e}\n`); process.exit(1); });
}
