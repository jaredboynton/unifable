#!/usr/bin/env node
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const DEFAULT_DIR = path.join(os.homedir(), ".cache", "explore", "acp");
const SOCKET_PATH = process.env.EXPLORE_ACP_SOCKET || path.join(DEFAULT_DIR, "cursor-agent-acp.sock");
const PID_PATH = process.env.EXPLORE_ACP_PID || path.join(DEFAULT_DIR, "cursor-agent-acp.pid");
const LOG_PATH = process.env.EXPLORE_ACP_LOG || path.join(DEFAULT_DIR, "cursor-agent-acp.log");
const META_PATH = process.env.EXPLORE_ACP_META || path.join(DEFAULT_DIR, "cursor-agent-acp.meta.json");
const LOCK_PATH = process.env.EXPLORE_ACP_LOCK || `${SOCKET_PATH}.lock`;
const DAEMON_ID = `${Date.now()}-${process.pid}-${Math.random().toString(36).slice(2)}`;
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const FINAL_CHUNK_GRACE_MS = Number(process.env.EXPLORE_ACP_FINAL_CHUNK_GRACE_MS || 3_000);
const CURSOR_ACP_ARGS = [
  "--force",
  "--sandbox",
  "disabled",
  "--exclude-tools",
  "shellToolCall,writeShellStdinToolCall,editToolCall,applyAgentDiffToolCall,deleteToolCall",
  "acp",
];

let nextJsonRpcId = 1;
let nextTerminalId = 1;

function usage() {
  console.error("usage: cursor-acp-trace.mjs --prompt-file <path> --out <path> --raw <path> --err <path> --workspace <path> [--model <model>]");
  process.exit(2);
}

function parseArgs(argv) {
  const args = {};
  for (let index = 0; index < argv.length; index += 1) {
    const entry = argv[index];
    if (entry === "--daemon") {
      args.daemon = true;
      continue;
    }
    if (entry === "--stop-daemon") {
      args.stopDaemon = true;
      continue;
    }
    if (entry === "--stream") {
      args.stream = true;
      continue;
    }
    if (!entry.startsWith("--")) {
      usage();
    }
    const key = entry.slice(2).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
    const value = argv[index + 1];
    if (value === undefined) {
      usage();
    }
    args[key] = value;
    index += 1;
  }
  return args;
}

function hasCompleteTraceOutput(text) {
  if (/^## Flow\b/m.test(text)
    && /^## Code references\b/m.test(text)
    && /^## Key files\b/m.test(text)) {
    return true;
  }
  const json = extractTraceJson(text);
  return Boolean(json);
}

function completeTraceOutput(text) {
  const match = /^## Flow\b/m.exec(text);
  if (match) {
    return text.slice(match.index);
  }
  return extractTraceJson(text) || text;
}

function extractTraceJson(text) {
  const source = String(text || "");
  for (let start = source.indexOf("{"); start !== -1; start = source.indexOf("{", start + 1)) {
    for (let end = source.lastIndexOf("}"); end > start; end = source.lastIndexOf("}", end - 1)) {
      const candidate = source.slice(start, end + 1).trim();
      try {
        const parsed = JSON.parse(candidate);
        if (typeof parsed?.opening_summary === "string" && Array.isArray(parsed?.code_passages)) {
          return candidate;
        }
      } catch {
        // Keep scanning for a complete structured trace object.
      }
    }
  }
  return null;
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function createTraceCollector(onChunk) {
  let resolveTraceReady;
  let rejectTraceReady;
  const traceReady = new Promise((resolve, reject) => {
    resolveTraceReady = resolve;
    rejectTraceReady = reject;
  });
  return {
    output: "",
    raw: [],
    onChunk,
    promptResult: null,
    traceReady,
    resolveTraceReady,
    rejectTraceReady,
  };
}

function recordTraceText(collector, text) {
  collector.output += text;
  collector.onChunk?.(text);
  if (hasCompleteTraceOutput(collector.output)) {
    collector.resolveTraceReady();
  }
}

function appendLog(message) {
  fs.mkdirSync(path.dirname(LOG_PATH), { recursive: true });
  fs.appendFileSync(LOG_PATH, `[${new Date().toISOString()}] ${message}\n`);
}

function tailFile(filePath, maxBytes = 4000) {
  try {
    const stat = fs.statSync(filePath);
    const start = Math.max(0, stat.size - maxBytes);
    const fd = fs.openSync(filePath, "r");
    const buffer = Buffer.alloc(stat.size - start);
    fs.readSync(fd, buffer, 0, buffer.length, start);
    fs.closeSync(fd);
    return buffer.toString("utf8");
  } catch {
    return "";
  }
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`;
}

function mentionsRecursiveTrace(value) {
  try {
    const text = JSON.stringify(value || {}).toLowerCase();
    return /\btrace[-\w]*\.sh\b|\bcursor-acp-trace\.mjs\b/.test(text);
  } catch {
    return false;
  }
}

function findOnPath(command) {
  const pathEnv = process.env.PATH || "";
  for (const dir of pathEnv.split(path.delimiter)) {
    if (!dir) continue;
    const candidate = path.join(dir, command);
    try {
      fs.accessSync(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      // Try the next PATH entry.
    }
  }
  return command;
}

function daemonFingerprint() {
  return {
    home: os.homedir(),
    cursorAgent: findOnPath("cursor-agent"),
    cursorAgentArgs: CURSOR_ACP_ARGS,
    socketPath: SOCKET_PATH,
    pidPath: PID_PATH,
    logPath: LOG_PATH,
    metaPath: META_PATH,
  };
}

function lowerJson(value) {
  try {
    return JSON.stringify(value || {}).toLowerCase();
  } catch {
    return "";
  }
}

function disallowedTraceToolReason(update = {}) {
  if (!update || (update.sessionUpdate !== "tool_call" && update.sessionUpdate !== "tool_call_update")) {
    return null;
  }
  const rawInput = update.rawInput || update.input || {};
  const text = [
    update.title,
    update.kind,
    update.toolCallId,
    update.toolName,
    rawInput.tool_name,
    rawInput.toolName,
    rawInput.name,
    rawInput.command,
    lowerJson(rawInput),
  ].filter(Boolean).join("\n").toLowerCase();
  return /\btrace[-\w]*\.sh\b|\bcursor-acp-trace\.mjs\b/.test(text) ? "recursive trace invocation" : null;
}

function fingerprintKey() {
  return JSON.stringify(daemonFingerprint());
}

function isCursorAuthError(error) {
  const message = String(error?.message || error || "");
  return /(^|\b)(Authentication required|Not logged in)(\b|$)/i.test(message)
    || /(^|\b)authenticate: Internal error(\b|$)/i.test(message)
    || /keychain cannot be found[^\n]*cursor-user/i.test(message);
}

function cursorAuthHint() {
  const scriptPath = new URL(import.meta.url).pathname;
  const envParts = [
    ["HOME", process.env.HOME],
    ["EXPLORE_ACP_SOCKET", process.env.EXPLORE_ACP_SOCKET],
    ["EXPLORE_ACP_PID", process.env.EXPLORE_ACP_PID],
    ["EXPLORE_ACP_LOG", process.env.EXPLORE_ACP_LOG],
    ["EXPLORE_ACP_META", process.env.EXPLORE_ACP_META],
  ]
    .filter(([, value]) => value)
    .map(([key, value]) => `${key}=${shellQuote(value)}`);
  const stopCommand = [...envParts, "node", shellQuote(scriptPath), "--stop-daemon"].join(" ");
  return [
    "",
    "explore: cursor-agent reported an authentication failure inside the cached ACP daemon.",
    "explore: if `cursor-agent status` is logged in, restart the cached daemon and rerun:",
    `explore:   ${stopCommand}`,
    "explore: if status is not logged in, run `cursor-agent login` first.",
    "",
  ].join("\n");
}

function readPid() {
  try {
    const pid = Number(fs.readFileSync(PID_PATH, "utf8").trim());
    return Number.isInteger(pid) && pid > 0 ? pid : null;
  } catch {
    return null;
  }
}

function isAlive(pid) {
  if (!pid) {
    return false;
  }
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function connectSocket() {
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(SOCKET_PATH);
    socket.once("connect", () => resolve(socket));
    socket.once("error", reject);
  });
}

function removeStartLockIfStale() {
  let stale = false;
  try {
    const pid = Number(fs.readFileSync(path.join(LOCK_PATH, "pid"), "utf8").trim());
    if (!Number.isInteger(pid) || pid <= 0 || !isAlive(pid)) {
      stale = true;
    }
  } catch {
    stale = true;
  }
  if (stale) {
    removePathQuiet(LOCK_PATH);
  }
  return stale;
}

function directoryChange(dir) {
  return new Promise((resolve, reject) => {
    let watcher;
    try {
      watcher = fs.watch(dir, () => {
        watcher.close();
        resolve();
      });
    } catch (error) {
      reject(error);
    }
  });
}

function childExitRejection(child, detail) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.reject(new Error(`${detail} exited code=${child.exitCode} signal=${child.signalCode}`));
  }
  return new Promise((_, reject) => {
    child.once("exit", (code, signal) => {
      reject(new Error(`${detail} exited code=${code} signal=${signal}`));
    });
    child.once("error", reject);
  });
}

function removePathQuiet(target) {
  try {
    fs.rmSync(target, { recursive: true, force: true });
  } catch (error) {
    if (error.code !== "ENOENT" && error.code !== "ENOTEMPTY") {
      throw error;
    }
  }
}

async function acquireStartLock() {
  fs.mkdirSync(path.dirname(LOCK_PATH), { recursive: true });
  for (;;) {
    const claimPath = `${LOCK_PATH}.${process.pid}.${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      fs.mkdirSync(claimPath, { recursive: false, mode: 0o700 });
      fs.writeFileSync(path.join(claimPath, "pid"), String(process.pid));
      fs.renameSync(claimPath, LOCK_PATH);
      return () => removePathQuiet(LOCK_PATH);
    } catch (error) {
      removePathQuiet(claimPath);
      if (error.code !== "EEXIST" && error.code !== "ENOTEMPTY") {
        throw error;
      }
      if (removeStartLockIfStale()) {
        continue;
      }
      await directoryChange(path.dirname(LOCK_PATH));
    }
  }
}

async function hasMatchingDaemon() {
  try {
    const response = await requestDaemon({ control: "fingerprint" });
    if (response.fingerprint === fingerprintKey()) {
      return true;
    }
    throw new Error("cached ACP daemon environment changed; restart the explore ACP daemon for this shell before tracing");
  } catch (error) {
    if (/environment changed/.test(String(error.message || error))) {
      throw error;
    }
    return false;
  }
}

async function socketReadyFromChild(child) {
  const childExit = childExitRejection(child, "ACP daemon");
  for (;;) {
    try {
      const socket = await connectSocket();
      socket.end();
      return;
    } catch (error) {
      await Promise.race([directoryChange(path.dirname(SOCKET_PATH)), childExit]);
    }
  }
}

async function ensureDaemon() {
  if (await hasMatchingDaemon()) {
    return;
  }

  const releaseStartLock = await acquireStartLock();
  try {
    if (await hasMatchingDaemon()) {
      return;
    }

    const pid = readPid();
    if (pid && !isAlive(pid)) {
      fs.rmSync(PID_PATH, { force: true });
    }
    fs.rmSync(SOCKET_PATH, { force: true });
    fs.mkdirSync(path.dirname(SOCKET_PATH), { recursive: true });

    const out = fs.openSync(LOG_PATH, "a");
    const child = spawn(process.execPath, [new URL(import.meta.url).pathname, "--daemon"], {
      detached: true,
      stdio: ["ignore", out, out],
      env: process.env,
    });
    await socketReadyFromChild(child);
    child.unref();
    const response = await requestDaemon({ control: "fingerprint" });
    if (response.fingerprint !== fingerprintKey()) {
      throw new Error("started ACP daemon reported a different environment fingerprint");
    }
  } finally {
    releaseStartLock();
  }
}

function writeJsonLine(socket, value) {
  socket.write(JSON.stringify(value) + "\n");
}

function requestDaemon(payload, { onChunk } = {}) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    let settled = false;
    const socket = net.createConnection(SOCKET_PATH);

    const settle = (callback, value) => {
      if (settled) return;
      settled = true;
      socket.end();
      callback(value);
    };

    const handleLine = (line) => {
      if (!line.trim()) {
        return;
      }
      try {
        const message = JSON.parse(line);
        if (message.ok && message.event === "chunk") {
          if (typeof message.text === "string") {
            onChunk?.(message.text);
          }
          return;
        }
        if (message.ok) {
          settle(resolve, message);
        } else {
          settle(reject, new Error(message.error || "ACP trace failed"));
        }
      } catch (error) {
        settle(reject, error);
      }
    };

    socket.setEncoding("utf8");
    socket.on("connect", () => writeJsonLine(socket, payload));
    socket.on("data", (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        handleLine(line);
      }
    });
    socket.on("error", (error) => {
      settle(reject, error);
    });
    socket.on("close", () => {
      if (settled) return;
      if (buffer.trim()) {
        handleLine(buffer);
      } else {
        settle(reject, new Error("ACP daemon closed without a response"));
      }
    });
  });
}

async function runClient(args) {
  for (const key of ["promptFile", "out", "raw", "err", "workspace"]) {
    if (!args[key]) {
      usage();
    }
  }
  const prompt = fs.readFileSync(args.promptFile, "utf8");
  const request = {
    prompt,
    workspace: path.resolve(args.workspace),
    model: args.model || "auto",
  };

  try {
    await ensureDaemon();
    const response = await requestDaemon({ ...request, stream: Boolean(args.stream) }, {
      onChunk: args.stream ? (text) => process.stderr.write(text) : undefined,
    });
    fs.writeFileSync(args.out, response.output || "");
    fs.writeFileSync(args.raw, (response.raw || []).map((entry) => JSON.stringify(entry)).join("\n") + "\n");
  } catch (error) {
    const logTail = tailFile(LOG_PATH);
    const detail = logTail ? `${error.stack || error.message}\n--- ACP daemon log tail (${LOG_PATH}) ---\n${logTail}` : (error.stack || error.message);
    fs.appendFileSync(args.err, `ACP trace failed: ${detail}\n`);
    if (isCursorAuthError(detail)) {
      fs.appendFileSync(args.err, cursorAuthHint());
    }
    process.exit(1);
  }
}

function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

class AcpProcess {
  constructor() {
    this.child = spawn("cursor-agent", CURSOR_ACP_ARGS, { stdio: ["pipe", "pipe", "pipe"] });
    this.buffer = "";
    this.stderrTail = "";
    this.pending = new Map();
    this.notifications = [];
    this.collectors = new Map();
    this.sessions = new Map();
    this.terminals = new Map();
    this.authMethods = [];
    this.ready = this.initialize();

    this.child.stdout.setEncoding("utf8");
    this.child.stderr.setEncoding("utf8");
    this.child.stdout.on("data", (chunk) => this.onData(chunk));
    this.child.stderr.on("data", (chunk) => {
      this.stderrTail = `${this.stderrTail}${chunk}`.slice(-4000);
      appendLog(`cursor-agent stderr: ${chunk.trimEnd()}`);
    });
    this.child.on("exit", (code, signal) => {
      const stderr = this.stderrTail.trim();
      const detail = stderr ? `: ${stderr}` : "";
      const error = new Error(`cursor-agent acp exited code=${code} signal=${signal}${detail}`);
      for (const pending of this.pending.values()) {
        pending.reject(error);
      }
      this.pending.clear();
      for (const collector of this.collectors.values()) {
        collector.rejectTraceReady(error);
      }
      this.collectors.clear();
      appendLog(error.message);
      process.exit(1);
    });
  }

  send(method, params) {
    const id = nextJsonRpcId++;
    const message = { jsonrpc: "2.0", id, method, params };
    this.child.stdin.write(JSON.stringify(message) + "\n");
    return new Promise((resolve, reject) => {
      this.pending.set(id, { method, resolve, reject, sessionId: params?.sessionId });
    });
  }

  respond(id, result) {
    this.child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id, result }) + "\n");
  }

  reject(id, code, message) {
    this.child.stdin.write(JSON.stringify(rpcError(id, code, message)) + "\n");
  }

  onData(chunk) {
    this.buffer += chunk;
    let newline;
    while ((newline = this.buffer.indexOf("\n")) !== -1) {
      const line = this.buffer.slice(0, newline);
      this.buffer = this.buffer.slice(newline + 1);
      if (!line.trim()) {
        continue;
      }
      let message;
      try {
        message = JSON.parse(line);
      } catch (error) {
        appendLog(`failed to parse ACP line: ${error.message}: ${line}`);
        continue;
      }
      this.recordMessage(message);
      if (message.id !== undefined && message.method) {
        this.handleClientRequest(message).catch((error) => {
          this.reject(message.id, -32603, error.message);
        });
        continue;
      }
      if (message.id !== undefined && this.pending.has(message.id)) {
        const pending = this.pending.get(message.id);
        this.pending.delete(message.id);
        this.recordForSession(pending.sessionId, message);
        if (message.error) {
          pending.reject(new Error(`${pending.method}: ${message.error.message}`));
        } else {
          pending.resolve(message.result);
        }
      }
    }
  }

  recordMessage(message) {
    this.notifications.push(message);
    const sessionId = message.params?.sessionId;
    this.recordForSession(sessionId, message);
  }

  recordForSession(sessionId, message) {
    if (!sessionId) {
      return;
    }
    const collector = this.collectors.get(sessionId);
    if (!collector) {
      return;
    }
    collector.raw.push(message);
    if (message.method !== "session/update") {
      return;
    }
    const update = message.params?.update;
    const blockedReason = disallowedTraceToolReason(update);
    if (blockedReason) {
      const error = new Error(`trace harness blocked disallowed Cursor tool call: ${blockedReason}`);
      appendLog(`${error.message}: ${JSON.stringify(update).slice(0, 2000)}`);
      collector.rejectTraceReady(error);
      this.child.kill("SIGTERM");
      return;
    }
    if (update?.sessionUpdate !== "agent_message_chunk") {
      return;
    }
    const content = update.content;
    if (content?.type === "text" && typeof content.text === "string") {
      recordTraceText(collector, content.text);
    }
  }

  async initialize() {
    const result = await this.send("initialize", {
      protocolVersion: 1,
      clientCapabilities: {
        fs: { readTextFile: true, writeTextFile: true },
        terminal: true,
      },
      clientInfo: {
        name: "codex-explore-trace",
        title: "Codex Explore Trace",
        version: "0.1.0",
      },
    });
    this.authMethods = Array.isArray(result.authMethods) ? result.authMethods : [];
    appendLog(`initialized cursor-agent acp: ${JSON.stringify(result.agentCapabilities || {})}`);
  }

  // cursor-agent advertises authMethods (e.g. cursor_login) but accepts
  // session/new directly when the CLI login is still valid. Only call
  // authenticate reactively when a session/new is rejected for auth — for
  // instance a long-lived daemon whose cached credentials went stale — then
  // retry once.
  async newSession(workspace) {
    const params = { cwd: workspace, mcpServers: [] };
    appendLog("session/new without trace MCP/profile restrictions");
    try {
      const session = await this.send("session/new", params);
      appendLog(`session/new ok: ${session.sessionId || "no-session-id"}`);
      return session;
    } catch (error) {
      if (!/authenticat|not logged in|unauthorized/i.test(error.message) || !this.authMethods.length) {
        throw error;
      }
      const methodId = this.authMethods[0].id;
      appendLog(`session/new rejected (${error.message}); authenticating via ${methodId} and retrying`);
      await this.send("authenticate", { methodId });
      const session = await this.send("session/new", params);
      appendLog(`session/new ok after auth: ${session.sessionId || "no-session-id"}`);
      return session;
    }
  }

  async trace({ prompt, workspace, model }, onChunk) {
    await this.ready;
    const session = await this.newSession(workspace);
    const sessionId = session.sessionId;
    const collector = createTraceCollector(onChunk);
    this.sessions.set(sessionId, { workspace });
    this.collectors.set(sessionId, collector);

    try {
      await this.configureSession(sessionId, session, model);
      appendLog(`session/prompt start: ${sessionId}`);
      collector.promptResult = await this.send("session/prompt", {
        sessionId,
        prompt: [{ type: "text", text: prompt }],
      });
      appendLog(`session/prompt returned: ${sessionId}`);
      await Promise.race([collector.traceReady, delay(FINAL_CHUNK_GRACE_MS)]);
      if (!collector.output.trim()) {
        throw new Error("cursor-agent ended before producing trace output");
      }
      collector.resolveTraceReady();
      await collector.traceReady;
    } finally {
      this.collectors.delete(sessionId);
    }

    return { output: completeTraceOutput(collector.output), raw: collector.raw };
  }

  async configureSession(sessionId, session, model) {
    const configOptions = Array.isArray(session.configOptions) ? session.configOptions : [];
    const modeOption = configOptions.find((option) => option.id === "mode");
    const requestedMode = process.env.EXPLORE_CURSOR_MODE || "ask";
    if (modeOption?.options?.some((option) => option.value === requestedMode)) {
      await this.send("session/set_config_option", {
        sessionId,
        configId: "mode",
        value: requestedMode,
      }).catch((error) => appendLog(`failed to set ${requestedMode} mode: ${error.message}`));
    } else if (session.modes?.availableModes?.some((mode) => mode.id === requestedMode)) {
      await this.send("session/set_mode", { sessionId, modeId: requestedMode })
        .catch((error) => appendLog(`failed to set ${requestedMode} mode: ${error.message}`));
    }

    if (!model || model === "auto" || model === "default" || model === "default[]") {
      return;
    }
    const modelOption = configOptions.find((option) => option.id === "model");
    const selected = modelOption?.options?.find((option) =>
      option.value === model ||
      option.value.startsWith(`${model}[`) ||
      option.name?.toLowerCase() === model.toLowerCase()
    );
    if (selected) {
      await this.send("session/set_config_option", {
        sessionId,
        configId: "model",
        value: selected.value,
      }).catch((error) => appendLog(`failed to set model ${model}: ${error.message}`));
    }
  }

  async handleClientRequest(message) {
    const { id, method, params } = message;
    switch (method) {
      case "fs/read_text_file":
        this.respond(id, await this.readTextFile(params));
        return;
      case "fs/write_text_file":
        this.respond(id, await this.writeTextFile(params));
        return;
      case "terminal/create":
        this.respond(id, await this.terminalCreate(params));
        return;
      case "terminal/output":
        this.respond(id, this.terminalOutput(params));
        return;
      case "terminal/wait_for_exit":
        this.respond(id, await this.terminalExit(params));
        return;
      case "terminal/kill":
        this.respond(id, this.terminalKill(params));
        return;
      case "terminal/release":
        this.respond(id, this.terminalRelease(params));
        return;
      case "session/request_permission":
        this.respond(id, this.permissionResponse(params));
        return;
      default:
        this.reject(id, -32601, `client method not implemented: ${method}`);
    }
  }

  sessionRoot(sessionId) {
    return this.sessions.get(sessionId)?.workspace || process.cwd();
  }

  ensureInsideSession(sessionId, candidatePath) {
    const root = path.resolve(this.sessionRoot(sessionId));
    const absolute = path.resolve(candidatePath);
    const relative = path.relative(root, absolute);
    if (relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`path outside trace workspace: ${absolute}`);
    }
    return absolute;
  }

  async readTextFile(params = {}) {
    const absolute = this.ensureInsideSession(params.sessionId, params.path);
    const text = await fs.promises.readFile(absolute, "utf8");
    const line = Number(params.line || 1);
    const limit = params.limit === undefined ? undefined : Number(params.limit);
    if ((!line || line <= 1) && !limit) {
      return { content: text };
    }
    const lines = text.split(/(?<=\n)/);
    const start = Math.max(0, line - 1);
    const end = limit ? start + Math.max(0, limit) : undefined;
    return { content: lines.slice(start, end).join("") };
  }

  async writeTextFile(params = {}) {
    const absolute = this.ensureInsideSession(params.sessionId, params.path);
    await fs.promises.mkdir(path.dirname(absolute), { recursive: true });
    await fs.promises.writeFile(absolute, String(params.content ?? ""), "utf8");
    return {};
  }

  async terminalCreate(params = {}) {
    const cwd = this.ensureInsideSession(params.sessionId, params.cwd || this.sessionRoot(params.sessionId));
    const command = params.command;
    if (typeof command !== "string" || !command) {
      throw new Error("terminal/create missing command");
    }
    const args = Array.isArray(params.args) ? params.args.map(String) : [];
    if (mentionsRecursiveTrace({ command, args })) {
      throw new Error("recursive trace-cursor.sh invocation blocked");
    }
    const env = { ...process.env };
    for (const entry of params.env || []) {
      if (entry?.name) {
        env[String(entry.name)] = String(entry.value ?? "");
      }
    }
    const limit = Number(params.outputByteLimit || 1024 * 1024);
    const terminalId = `term_${nextTerminalId++}`;
    const child = spawn(command, args, { cwd, env, shell: false });
    const terminal = {
      child,
      output: "",
      truncated: false,
      limit,
      exitStatus: null,
      exitListeners: [],
    };
    const record = (chunk) => {
      terminal.output += chunk.toString();
      if (Buffer.byteLength(terminal.output, "utf8") > terminal.limit) {
        terminal.truncated = true;
        while (Buffer.byteLength(terminal.output, "utf8") > terminal.limit) {
          terminal.output = terminal.output.slice(Math.max(1, Math.floor(terminal.output.length / 10)));
        }
      }
    };
    child.stdout.on("data", record);
    child.stderr.on("data", record);
    child.on("exit", (code, signal) => {
      terminal.exitStatus = { exitCode: code, signal };
      for (const listener of terminal.exitListeners.splice(0)) {
        listener(terminal.exitStatus);
      }
    });
    child.on("error", (error) => {
      record(`failed to start command: ${error.message}\n`);
      terminal.exitStatus = { exitCode: 127, signal: null };
      for (const listener of terminal.exitListeners.splice(0)) {
        listener(terminal.exitStatus);
      }
    });
    this.terminals.set(terminalId, terminal);
    return { terminalId };
  }

  getTerminal(terminalId) {
    const terminal = this.terminals.get(terminalId);
    if (!terminal) {
      throw new Error(`unknown terminalId: ${terminalId}`);
    }
    return terminal;
  }

  terminalOutput(params = {}) {
    const terminal = this.getTerminal(params.terminalId);
    const result = { output: terminal.output, truncated: terminal.truncated };
    if (terminal.exitStatus) {
      result.exitStatus = terminal.exitStatus;
    }
    return result;
  }

  terminalExit(params = {}) {
    const terminal = this.getTerminal(params.terminalId);
    if (terminal.exitStatus) {
      return terminal.exitStatus;
    }
    return new Promise((resolve) => terminal.exitListeners.push(resolve));
  }

  terminalKill(params = {}) {
    const terminal = this.getTerminal(params.terminalId);
    if (!terminal.exitStatus) {
      terminal.child.kill("SIGTERM");
    }
    return null;
  }

  terminalRelease(params = {}) {
    const terminal = this.getTerminal(params.terminalId);
    if (!terminal.exitStatus) {
      terminal.child.kill("SIGTERM");
    }
    this.terminals.delete(params.terminalId);
    return null;
  }

  permissionResponse(params = {}) {
    appendLog(`permission request: ${JSON.stringify(params).slice(0, 4000)}`);
    if (mentionsRecursiveTrace(params)) {
      appendLog("permission denied: recursive trace invocation is blocked");
      return { outcome: { outcome: "cancelled" } };
    }
    const allow = (params.options || []).find((option) => option.kind === "allow_once")
      || (params.options || []).find((option) => option.kind === "allow_always");
    if (allow) {
      return { outcome: { outcome: "selected", optionId: allow.optionId } };
    }
    return { outcome: { outcome: "cancelled" } };
  }
}

class TraceDaemon {
  constructor() {
    this.acp = new AcpProcess();
  }

  async start() {
    fs.mkdirSync(path.dirname(SOCKET_PATH), { recursive: true });
    fs.rmSync(SOCKET_PATH, { force: true });
    fs.writeFileSync(PID_PATH, String(process.pid));
    fs.writeFileSync(META_PATH, JSON.stringify({ pid: process.pid, daemonId: DAEMON_ID, fingerprint: fingerprintKey(), socketPath: SOCKET_PATH }) + "\n");
    const server = net.createServer((socket) => this.onConnection(socket));
    server.listen(SOCKET_PATH, () => {
      appendLog(`trace ACP daemon listening on ${SOCKET_PATH}`);
    });
    process.on("SIGTERM", () => {
      server.close();
      this.shutdown();
    });
    process.on("SIGINT", () => {
      server.close();
      this.shutdown();
    });
  }

  shutdown() {
    appendLog("trace ACP daemon shutting down");
    if (this.ownsDaemonFiles()) {
      fs.rmSync(SOCKET_PATH, { force: true });
      fs.rmSync(PID_PATH, { force: true });
      fs.rmSync(META_PATH, { force: true });
    }
    for (const terminal of this.acp.terminals.values()) {
      if (!terminal.exitStatus) {
        terminal.child.kill("SIGTERM");
      }
    }
    this.acp.child.kill("SIGTERM");
    process.exit(0);
  }

  ownsDaemonFiles() {
    try {
      const meta = JSON.parse(fs.readFileSync(META_PATH, "utf8"));
      return meta.pid === process.pid && meta.daemonId === DAEMON_ID && meta.socketPath === SOCKET_PATH;
    } catch {
      return false;
    }
  }

  onConnection(socket) {
    socket.setEncoding("utf8");
    let buffer = "";
    socket.on("data", (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf("\n")) !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) {
          continue;
        }
        let request;
        try {
          request = JSON.parse(line);
        } catch (error) {
          writeJsonLine(socket, { ok: false, error: error.message });
          socket.end();
          continue;
        }
        if (request.control === "fingerprint") {
          writeJsonLine(socket, { ok: true, fingerprint: fingerprintKey() });
          socket.end();
          continue;
        }
        if (request.control === "shutdown") {
          socket.end(JSON.stringify({ ok: true }) + "\n", () => this.shutdown());
          continue;
        }
        this.handleRequest(socket, request).catch((error) => {
          if (!socket.destroyed) {
            writeJsonLine(socket, { ok: false, error: error.stack || error.message });
            socket.end();
          }
        });
      }
    });
  }

  async handleRequest(socket, request) {
    const streamChunk = request.stream
      ? (text) => {
        if (!socket.destroyed) {
          writeJsonLine(socket, { ok: true, event: "chunk", text });
        }
      }
      : undefined;
    const result = await this.acp.trace(request, streamChunk);
    if (!socket.destroyed) {
      writeJsonLine(socket, { ok: true, event: "done", ...result });
      socket.end();
    }
  }
}

async function stopDaemon() {
  try {
    const response = await requestDaemon({ control: "fingerprint" });
    if (response.fingerprint !== fingerprintKey()) {
      console.error(`explore: refusing to stop ACP daemon on ${SOCKET_PATH}; environment fingerprint does not match this shell`);
      process.exit(1);
    }
    await requestDaemon({ control: "shutdown" });
    return;
  } catch {
    // If the socket is stale, fall through to safe file cleanup.
  }

  const pid = readPid();
  if (pid && isAlive(pid)) {
    console.error(`explore: refusing to signal pid ${pid}; ${PID_PATH} is live but no ACP daemon answered on ${SOCKET_PATH}`);
    process.exit(1);
  }
  fs.rmSync(SOCKET_PATH, { force: true });
  fs.rmSync(PID_PATH, { force: true });
  fs.rmSync(META_PATH, { force: true });
}

const args = parseArgs(process.argv.slice(2));
if (args.daemon) {
  process.env.EXPLORE_INSIDE_TRACE_DAEMON = "1";
  new TraceDaemon().start();
} else if (args.stopDaemon) {
  await stopDaemon();
} else {
  await runClient(args);
}
