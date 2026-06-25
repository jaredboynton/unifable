// codex-alpha-search-client.mjs — Codex standalone web.run POST alpha/search client.
//
// Headers (from Exa prior art: llm-liberty, opencode, ai-sdk-oauth-providers + curl ablation):
//   Authorization, ChatGPT-Account-ID, originator, User-Agent (codex_cli_rs, no reqwest),
//   version, OpenAI-Beta: responses=experimental
//
// Transport: curl subprocess by default. Node fetch gets CF 403 with identical headers (TLS fp).
import { randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";
import {
  CODEX_CLIENT_VERSION,
  CODEX_ORIGINATOR,
  buildCodexCliUserAgent,
  loadChatgptAuth,
} from "./codex-responses-client.mjs";
import { RealtimeError } from "./realtime_client.mjs";

const execFileAsync = promisify(execFile);
const ALPHA_TRANSPORT = process.env.EXPLORE_ALPHA_TRANSPORT || "curl";

async function postAlphaSearchCurl(url, headers, bodyJson) {
  const dir = mkdtempSync(join(tmpdir(), "alpha-search-"));
  const bodyPath = join(dir, "body.json");
  writeFileSync(bodyPath, bodyJson);
  const args = [
    "-sS",
    "-w",
    "\n__HTTP__:%{http_code}",
    "-X",
    "POST",
    url,
    "--data-binary",
    `@${bodyPath}`,
  ];
  for (const [key, value] of Object.entries(headers)) {
    args.push("-H", `${key}: ${value}`);
  }
  try {
    const { stdout } = await execFileAsync("curl", args, { maxBuffer: 10 * 1024 * 1024 });
    const match = stdout.match(/\n__HTTP__:(\d+)\s*$/);
    return {
      status: match ? Number(match[1]) : 0,
      raw: stdout.replace(/\n__HTTP__:\d+\s*$/, ""),
    };
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

async function postAlphaSearchHttp(url, headers, bodyJson) {
  const response = await fetch(url, { method: "POST", headers, body: bodyJson });
  return { status: response.status, raw: await response.text() };
}

export const CODEX_ALPHA_SEARCH_URL =
  process.env.CODEX_ALPHA_SEARCH_URL || "https://chatgpt.com/backend-api/codex/alpha/search";
export const CODEX_OPENAI_BETA =
  process.env.CODEX_OPENAI_BETA || "responses=experimental";

export function buildCodexApiHeaders(auth) {
  return {
    Authorization: `Bearer ${auth.accessToken}`,
    "ChatGPT-Account-ID": auth.accountId,
    originator: CODEX_ORIGINATOR,
    "User-Agent": buildCodexCliUserAgent(),
    version: CODEX_CLIENT_VERSION,
    "OpenAI-Beta": CODEX_OPENAI_BETA,
    Accept: "application/json",
    "Content-Type": "application/json",
  };
}

// gpt-5.4 / gpt-5.5 max output tokens (developers.openai.com/api/docs/models/gpt-5.4).
// The alpha/search `open` command streams full page content into this budget; a
// small cap truncates multi-page fetches, so the default matches the model cap.
export const ALPHA_MODEL_OUTPUT_CAP = 128000;

export function buildAlphaSearchBody({
  sessionId = randomUUID(),
  model,
  commands,
  input = null,
  externalWebAccess = true,
  maxOutputTokens = Number(process.env.EXPLORE_WS_ALPHA_MAX_OUTPUT_TOKENS) || ALPHA_MODEL_OUTPUT_CAP,
}) {
  const body = {
    id: sessionId,
    model,
    commands,
    settings: {
      allowed_callers: ["direct"],
      external_web_access: externalWebAccess,
    },
    max_output_tokens: maxOutputTokens,
  };
  if (input != null) body.input = input;
  return body;
}

export function buildAlphaOpenCommands(urls, { lineno = null } = {}) {
  const ops = (urls || [])
    .map((u) => String(u || "").trim())
    .filter(Boolean)
    .map((ref_id) => (lineno == null ? { ref_id } : { ref_id, lineno }));
  return { open: ops };
}

export function buildAlphaSearchCommands({ queries = [], urls = [] } = {}) {
  const commands = {};
  const qs = (queries || []).map((q) => (typeof q === "string" ? { q } : q)).filter((q) => q.q);
  if (qs.length) commands.search_query = qs;
  if (urls?.length) Object.assign(commands, buildAlphaOpenCommands(urls));
  return commands;
}

export async function postAlphaSearch({
  authPathOverride,
  body,
  url = CODEX_ALPHA_SEARCH_URL,
} = {}) {
  const auth = await loadChatgptAuth(authPathOverride);
  const headers = buildCodexApiHeaders(auth);
  const bodyJson = JSON.stringify(body);

  const transport = ALPHA_TRANSPORT === "fetch" ? postAlphaSearchHttp : postAlphaSearchCurl;
  const { status, raw } = await transport(url, headers, bodyJson);

  if (status < 200 || status >= 300) {
    throw new RealtimeError(
      `codex alpha/search HTTP ${status}: ${raw.slice(0, 500)}`,
    );
  }

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (e) {
    throw new RealtimeError(`codex alpha/search invalid JSON: ${e.message}`);
  }

  const output = typeof parsed.output === "string" ? parsed.output.trim() : "";
  if (!output) {
    throw new RealtimeError("codex alpha/search produced no output text");
  }

  return {
    output,
    encryptedOutput: parsed.encrypted_output ?? null,
    body,
    headers,
    status,
    transport: ALPHA_TRANSPORT,
  };
}
