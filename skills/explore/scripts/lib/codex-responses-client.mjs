// codex-responses-client.mjs — patchpress-matching Codex /responses HTTP client.
// Python port: llm/src/llm/providers/codex_responses_client.py
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { arch, homedir, platform, release } from "node:os";
import { dirname, join } from "node:path";
import { RealtimeError } from "./realtime_client.mjs";

export const CODEX_RESPONSES_URL =
  process.env.CODEX_RESPONSES_URL || "https://chatgpt.com/backend-api/codex/responses";
export const AUTH_PATH = process.env.CODEX_AUTH_JSON || join(homedir(), ".codex", "auth.json");
export const CODEX_HOME = process.env.CODEX_HOME || join(homedir(), ".codex");
export const CODEX_INSTALLATION_ID_PATH =
  process.env.CODEX_INSTALLATION_ID_PATH || join(CODEX_HOME, "installation_id");
export const CODEX_ORIGINATOR = process.env.CODEX_INTERNAL_ORIGINATOR_OVERRIDE || "codex_cli_rs";
export const CODEX_CLIENT_VERSION = process.env.CODEX_CLIENT_VERSION || resolveCodexClientVersion();
export const CODEX_USER_AGENT = process.env.CODEX_USER_AGENT || buildCodexUserAgent();

function isUuid(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

function commandOutput(command, args) {
  try {
    return execFileSync(command, args, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
  } catch {
    return "";
  }
}

function parseVersion(value) {
  return String(value || "").match(/\b\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\b/)?.[0] || "";
}

export function resolveCodexClientVersion() {
  const cliVersion = parseVersion(commandOutput("codex", ["--version"]));
  if (cliVersion) return cliVersion;
  try {
    const cached = JSON.parse(readFileSync(join(CODEX_HOME, "version.json"), "utf8"));
    const cachedVersion = parseVersion(cached.latest_version);
    if (cachedVersion) return cachedVersion;
  } catch {
    // fall through
  }
  return "0.0.0";
}

function codexArchitecture() {
  const value = arch();
  if (value === "x64") return "x86_64";
  return value || "unknown";
}

function codexOsDescription() {
  if (platform() === "darwin") {
    const macVersion = commandOutput("sw_vers", ["-productVersion"]);
    return `Mac OS ${macVersion || release()}`;
  }
  return `${platform()} ${release()}`;
}

export function buildCodexUserAgent() {
  const reqwestVersion = process.env.CODEX_REQWEST_VERSION || "0.12.28";
  return `${buildCodexCliUserAgent()} reqwest/${reqwestVersion}`;
}

export function buildCodexCliUserAgent() {
  return `${CODEX_ORIGINATOR}/${CODEX_CLIENT_VERSION} (${codexOsDescription()}; ${codexArchitecture()})`;
}

export function resolveCodexInstallationId() {
  try {
    const existing = readFileSync(CODEX_INSTALLATION_ID_PATH, "utf8").trim();
    if (isUuid(existing)) return existing.toLowerCase();
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const installationId = randomUUID();
  mkdirSync(dirname(CODEX_INSTALLATION_ID_PATH), { recursive: true });
  writeFileSync(CODEX_INSTALLATION_ID_PATH, installationId, { mode: 0o644 });
  return installationId;
}

export async function loadChatgptAuth(authPathOverride = null) {
  const path = authPathOverride || AUTH_PATH;
  const raw = await readFile(path, "utf8");
  const auth = JSON.parse(raw);
  if (auth.auth_mode !== "chatgpt") {
    throw new RealtimeError(`Expected ChatGPT auth in ${path}; got auth_mode=${auth.auth_mode}`);
  }
  const tokens = auth.tokens;
  const accessToken = tokens?.access_token;
  const accountId = tokens?.account_id || tokens?.id_token?.chatgpt_account_id;
  if (!accessToken) throw new RealtimeError(`Missing tokens.access_token in ${path}`);
  if (!accountId) throw new RealtimeError(`Missing ChatGPT account id in ${path}`);
  return { accessToken, accountId };
}

export function newCodexRequestIds() {
  const sessionId = randomUUID();
  const threadId = randomUUID();
  const windowId = `${threadId}:0`;
  const installationId = resolveCodexInstallationId();
  return { sessionId, threadId, windowId, installationId };
}

export function buildCodexResponsesHeaders(auth, ids) {
  return {
    Authorization: `Bearer ${auth.accessToken}`,
    "ChatGPT-Account-Id": auth.accountId,
    originator: CODEX_ORIGINATOR,
    "User-Agent": CODEX_USER_AGENT,
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    "session-id": ids.sessionId,
    "thread-id": ids.threadId,
    "x-client-request-id": ids.threadId,
    "x-codex-installation-id": ids.installationId,
    "x-codex-window-id": ids.windowId,
  };
}

export function buildCodexResponsesBody({
  model,
  promptText,
  ids,
  reasoningEffort = "low",
  tools = [],
  toolChoice = "auto",
  instructions = "You are a focused research assistant. Use web search when needed and answer succinctly with citations when available.",
  requestKind = "rt_web_run",
  harness = "explore",
}) {
  return {
    model,
    instructions,
    input: [
      {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: promptText }],
      },
    ],
    tools,
    tool_choice: toolChoice,
    parallel_tool_calls: false,
    reasoning: { effort: reasoningEffort },
    store: false,
    stream: true,
    include: ["reasoning.encrypted_content"],
    service_tier: "priority",
    prompt_cache_key: `${harness}-web-run-${ids.sessionId}`,
    client_metadata: {
      "x-codex-installation-id": ids.installationId,
      "x-codex-window-id": ids.windowId,
      session_id: ids.sessionId,
      thread_id: ids.threadId,
      codex_harness: harness,
      request_kind: requestKind,
    },
  };
}

export function parseSse(raw) {
  const events = [];
  for (const block of raw.split(/\r?\n\r?\n/)) {
    const dataLines = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart());
    if (dataLines.length === 0) continue;
    const data = dataLines.join("\n");
    if (data === "[DONE]") continue;
    try {
      events.push(JSON.parse(data));
    } catch {
      events.push({ type: "unparsed", data });
    }
  }
  return events;
}

export function collectOutputText(events) {
  let deltaText = "";
  let doneText = "";
  for (const event of events) {
    if (event.type === "response.output_text.delta" && typeof event.delta === "string") {
      deltaText += event.delta;
    }
    if (event.type === "response.output_text.done" && typeof event.text === "string") {
      doneText += event.text;
    }
    if (event.type === "response.output_item.done") {
      const item = event.item;
      if (item?.type === "message" && Array.isArray(item.content)) {
        for (const part of item.content) {
          if (part.type === "output_text" && typeof part.text === "string") doneText += part.text;
        }
      }
    }
  }
  return (deltaText || doneText).trim();
}

export async function postCodexResponses({ authPathOverride, ids, body, url = CODEX_RESPONSES_URL, timeoutMs = Number(process.env.EXPLORE_WS_RESPONSES_TIMEOUT_MS) || 30000 } = {}) {
  const auth = await loadChatgptAuth(authPathOverride);
  const requestIds = ids || newCodexRequestIds();
  const headers = buildCodexResponsesHeaders(auth, requestIds);
  // Bound the request so a stalled web_search backend can't hang the caller
  // forever (e.g. the F3 ensemble's Promise.all). The signal aborts the socket
  // so node can exit even if the server never responds.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.max(1000, timeoutMs));
  let response;
  let raw;
  try {
    response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    raw = await response.text();
  } catch (e) {
    if (e?.name === "AbortError") {
      throw new RealtimeError(`codex responses timed out after ${timeoutMs}ms`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok) {
    throw new RealtimeError(
      `codex responses HTTP ${response.status}: ${raw.slice(0, 500)}`,
    );
  }
  const events = parseSse(raw);
  const outputText = collectOutputText(events);
  const completed = events.find((event) => event.type === "response.completed");
  return {
    outputText,
    events,
    responseId: completed?.response?.id ?? null,
    ids: requestIds,
    body,
    headers,
  };
}
