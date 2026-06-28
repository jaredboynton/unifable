// Cerebras HTTP submit for trace-rt (hybrid transport).
import {
  SUBMIT_SCHEMA_NAME,
  traceProseSchema,
  traceProviderSchema,
} from "./trace-schema.mjs";

const DEFAULT_RETRIES = Number(process.env.UNITRACE_CEREBRAS_RETRIES) || 3;
const DEFAULT_TIMEOUT = Number(process.env.UNITRACE_RT_CEREBRAS_SUBMIT_TIMEOUT_MS) || 15_000;
const DEFAULT_MODEL = process.env.UNITRACE_RT_CEREBRAS_MODEL || process.env.UNITRACE_SEARCH_MODEL || "gpt-oss-120b";
const DEFAULT_BASE = process.env.CEREBRAS_BASE_URL || "https://api.cerebras.ai/v1";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryable(err) {
  const msg = String(err?.message || err);
  return /API error 5\d\d/.test(msg) || /fetch failed/i.test(msg) || /aborted/i.test(msg);
}

export async function submitTraceViaCerebras({
  system,
  user,
  schema,
  schemaName = SUBMIT_SCHEMA_NAME,
  hostPassages = false,
  question,
  filesRead = [],
  slim = true,
  timeoutMs = DEFAULT_TIMEOUT,
  apiKey = process.env.CEREBRAS_API_KEY,
  baseUrl = DEFAULT_BASE,
  model = DEFAULT_MODEL,
}) {
  if (!apiKey) throw new Error("CEREBRAS_API_KEY not set");

  const resolvedSchema = schema
    || (hostPassages
      ? traceProseSchema({ question, slim })
      : traceProviderSchema({
        allowedCodePassagePaths: [...filesRead].sort(),
        question,
        slim,
        filesReadCount: filesRead.size,
      }));

  const body = {
    model,
    temperature: 0,
    max_tokens: 4096,
    messages: [
      { role: "system", content: system },
      { role: "user", content: user },
    ],
    response_format: {
      type: "json_schema",
      json_schema: {
        name: schemaName,
        strict: true,
        schema: resolvedSchema,
      },
    },
    reasoning_format: "hidden",
  };

  let lastErr;
  for (let attempt = 1; attempt <= DEFAULT_RETRIES; attempt += 1) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const resp = await fetch(`${baseUrl}/chat/completions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!resp.ok) {
        const text = await resp.text().catch(() => resp.statusText);
        throw new Error(`Cerebras API error ${resp.status}: ${text}`);
      }
      const data = await resp.json();
      const content = data?.choices?.[0]?.message?.content;
      if (!content) throw new Error("Cerebras submit returned no content");
      return JSON.parse(content);
    } catch (err) {
      clearTimeout(timer);
      lastErr = err;
      if (attempt >= DEFAULT_RETRIES || !isRetryable(err)) throw err;
      await sleep(400 * attempt);
    }
  }
  throw lastErr;
}
