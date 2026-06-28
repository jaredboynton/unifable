#!/usr/bin/env node
// Probe Codex POST alpha/search (standalone web.run path).
import {
  CODEX_ALPHA_SEARCH_URL,
  buildAlphaSearchBody,
  postAlphaSearch,
} from "./lib/codex-alpha-search-client.mjs";

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

const dryRun = process.argv.includes("--dry-run");
const searchModel = argValue("--search-model", process.env.UNITRACE_SEARCH_MODEL || "gpt-5.4");
const query = argValue("--query", "today UTC date and one news headline");
const authPath = process.env.UNITRACE_CODEX_AUTH_PATH || null;

async function main() {
  const body = buildAlphaSearchBody({
    model: searchModel,
    commands: { search_query: [{ q: query }] },
  });

  if (dryRun) {
    console.log(JSON.stringify({
      ok: true,
      dryRun: true,
      url: CODEX_ALPHA_SEARCH_URL,
      searchModel,
      query,
      body,
      headers: [
        "Authorization",
        "ChatGPT-Account-ID",
        "originator",
        "User-Agent (codex_cli_rs, no reqwest suffix)",
        "version",
        "OpenAI-Beta: responses=experimental",
      ],
      requiredFingerprint: "originator + version + codex_cli_rs User-Agent are required; auth+account alone returns 403",
    }, null, 2));
    return;
  }

  const result = await postAlphaSearch({ authPathOverride: authPath, body });

  console.log(JSON.stringify({
    ok: true,
    url: CODEX_ALPHA_SEARCH_URL,
    searchModel,
    query,
    status: result.status,
    output: result.output,
    hasEncryptedOutput: Boolean(result.encryptedOutput),
  }, null, 2));
}

main().catch((err) => {
  console.log(JSON.stringify({ ok: false, error: err.message }, null, 2));
  process.exit(1);
});
