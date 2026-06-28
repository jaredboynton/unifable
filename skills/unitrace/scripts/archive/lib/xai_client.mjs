// Zero-dep xAI Responses API client (OpenAI-compatible /v1/responses).
import { appendFileSync } from "node:fs";

export const DEFAULT_BASE_URL = process.env.UNITRACE_GK_BASE_URL || "https://api.x.ai/v1";
export const DEFAULT_MODEL = process.env.UNITRACE_GK_MODEL || "grok-build-0.1";

export class GrokError extends Error {
  constructor(message) {
    super(message);
    this.name = "GrokError";
  }
}

export function apiKey() {
  const key = process.env.XAI_API_KEY || "";
  if (!key.trim()) throw new GrokError("XAI_API_KEY not set");
  return key.trim();
}

export function providerError(body, status) {
  if (!body || typeof body !== "object") return `HTTP ${status}`;
  const err = body.error && typeof body.error === "object" ? body.error : body;
  const code = err.code || err.type || "error";
  const msg = err.message || err.detail || JSON.stringify(err).slice(0, 200);
  return `${code}: ${msg}`;
}

export function extractFunctionCalls(response) {
  const out = [];
  const items = response?.output;
  if (!Array.isArray(items)) return out;
  for (const item of items) {
    if (!item || item.type !== "function_call") continue;
    const callId = item.call_id || item.id;
    const name = item.name;
    if (callId && name) {
      out.push({
        call_id: String(callId),
        name: String(name),
        arguments: typeof item.arguments === "string" ? item.arguments : JSON.stringify(item.arguments || {}),
      });
    }
  }
  return out;
}

export function extractOutputText(response) {
  const parts = [];
  for (const item of response?.output || []) {
    if (item?.type !== "message") continue;
    const content = item.content;
    if (typeof content === "string") {
      parts.push(content);
      continue;
    }
    if (!Array.isArray(content)) continue;
    for (const block of content) {
      if (!block || typeof block !== "object") continue;
      if (typeof block.text === "string") parts.push(block.text);
      else if (block.type === "output_text" && typeof block.text === "string") parts.push(block.text);
    }
  }
  if (!parts.length && typeof response?.output_text === "string") {
    parts.push(response.output_text);
  }
  return parts.join("");
}

export function parseJsonFromResponse(response) {
  const text = extractOutputText(response).trim();
  if (!text) throw new GrokError("response contained no text output");
  try {
    return JSON.parse(text);
  } catch (e) {
    const decoder = extractJsonObject(text);
    if (decoder) return decoder;
    throw new GrokError(`JSON parse failed: ${e.message}`);
  }
}

function extractJsonObject(text) {
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== "{") continue;
    try {
      const slice = text.slice(i);
      let depth = 0;
      for (let j = 0; j < slice.length; j++) {
        if (slice[j] === "{") depth++;
        else if (slice[j] === "}") {
          depth--;
          if (depth === 0) return JSON.parse(slice.slice(0, j + 1));
        }
      }
    } catch { /* try next brace */ }
  }
  return null;
}

function summarizeForLog(obj) {
  if (!obj || typeof obj !== "object") return obj;
  const copy = { ...obj };
  if (Array.isArray(copy.input)) {
    copy.input = copy.input.map((item) => {
      if (item?.type === "function_call_output" && typeof item.output === "string" && item.output.length > 200) {
        return { ...item, output: `${item.output.slice(0, 200)}...` };
      }
      return item;
    });
  }
  if (copy.text?.format?.schema) {
    copy.text = { format: { type: copy.text.format.type, name: copy.text.format.name } };
  }
  return copy;
}

export async function createResponse({
  model = DEFAULT_MODEL,
  baseUrl = DEFAULT_BASE_URL,
  instructions,
  input,
  tools,
  previousResponseId,
  textFormat,
  timeoutSec = 120,
  framesPath,
  fetchImpl = fetch,
}) {
  const body = { model, input };
  if (instructions) body.instructions = instructions;
  if (tools?.length) body.tools = tools;
  if (previousResponseId) body.previous_response_id = previousResponseId;
  if (textFormat) {
    body.text = { format: textFormat };
  }

  logFrame(framesPath, "send", summarizeForLog(body));

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutSec * 1000);
  let resp;
  try {
    resp = await fetchImpl(`${baseUrl.replace(/\/$/, "")}/responses`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        authorization: `Bearer ${apiKey()}`,
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (e) {
    if (e.name === "AbortError") throw new GrokError(`request timed out after ${timeoutSec}s`);
    throw new GrokError(`fetch failed: ${e.message}`);
  } finally {
    clearTimeout(timer);
  }

  const raw = await resp.text();
  let data;
  try {
    data = JSON.parse(raw);
  } catch {
    throw new GrokError(`invalid JSON response: HTTP ${resp.status} ${raw.slice(0, 200)}`);
  }

  logFrame(framesPath, "recv", { status: resp.status, id: data.id, output_len: data.output?.length });

  if (!resp.ok) {
    throw new GrokError(providerError(data, resp.status));
  }
  return data;
}

let frameBuffer = [];

export function flushFrames(framesPath) {
  if (!framesPath || !frameBuffer.length) return;
  appendFileSync(framesPath, frameBuffer.join(""));
  frameBuffer = [];
}

export function logFrame(framesPath, direction, obj) {
  if (!framesPath) return;
  frameBuffer.push(JSON.stringify({ dir: direction, event: obj }) + "\n");
  if (frameBuffer.length >= 32) flushFrames(framesPath);
}
