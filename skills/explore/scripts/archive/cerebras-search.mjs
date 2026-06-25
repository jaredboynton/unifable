// cerebras-search.mjs — Cerebras chat completions for explore search (pure Node).
//
// Search turns: tool calling with strict tool schemas.
// Finish-only turns: json_schema structured output (no tools) per Cerebras Structured Outputs docs.
//
// Env: CEREBRAS_API_KEY, CEREBRAS_BASE_URL, EXPLORE_SEARCH_MODEL, EXPLORE_SEARCH_TIMEOUT_MS,
//      EXPLORE_CEREBRAS_RETRIES (default 3)

export const FINISH_RESPONSE_SCHEMA = {
  type: "object",
  properties: {
    files: {
      type: "string",
      description: "One repo-relative file per line as path:lines (e.g. src/a.py:1-20). Empty string if nothing matched.",
    },
  },
  required: ["files"],
  additionalProperties: false,
};

const DEFAULT_RETRIES = parseInt(process.env.EXPLORE_CEREBRAS_RETRIES || "3", 10);

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryableError(err) {
  const msg = String(err?.message || err);
  return /API error 5\d\d/.test(msg)
    || /unexpected error occurred/i.test(msg)
    || /fetch failed/i.test(msg)
    || /aborted/i.test(msg);
}

export function parseModelMessage(data) {
  const msg = data?.choices?.[0]?.message;
  if (!msg) throw new Error("Cerebras returned no message");
  const toolCalls = (msg.tool_calls || []).map((tc) => ({
    id: tc.id,
    type: "function",
    function: { name: tc.function.name, arguments: tc.function.arguments },
  }));
  return { content: msg.content ?? null, tool_calls: toolCalls };
}

export async function callCerebrasSearch({
  apiKey,
  baseUrl,
  model,
  systemPrompt,
  messages,
  tools = null,
  finishOnly = false,
  timeoutMs = 60000,
  maxRetries = DEFAULT_RETRIES,
  debug = false,
}) {
  const body = {
    model,
    temperature: 0,
    max_tokens: 2048,
    messages: [{ role: "system", content: systemPrompt }, ...messages],
  };

  if (finishOnly) {
    body.response_format = {
      type: "json_schema",
      json_schema: {
        name: "search_finish",
        strict: true,
        schema: FINISH_RESPONSE_SCHEMA,
      },
    };
    body.reasoning_format = "hidden";
  } else {
    body.tools = tools;
    body.tool_choice = "auto";
    body.parallel_tool_calls = true;
  }

  let lastErr;
  for (let attempt = 1; attempt <= maxRetries; attempt += 1) {
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
      return parseModelMessage(data);
    } catch (err) {
      clearTimeout(timer);
      lastErr = err;
      if (attempt >= maxRetries || !isRetryableError(err)) throw err;
      if (debug) {
        process.stderr.write(`[search] cerebras retry ${attempt}/${maxRetries}: ${err.message}\n`);
      }
      await sleep(400 * attempt);
    }
  }
  throw lastErr;
}
