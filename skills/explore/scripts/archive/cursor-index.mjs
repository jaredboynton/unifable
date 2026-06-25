#!/usr/bin/env node
// Zero-dependency Cursor repo42 server-side semantic index client.
// Indexes a workspace via the Fast repo RPCs and queries SearchRepositoryV2, so
// codebase_search returns sub-second results from Cursor's server index instead
// of the local search.sh Cerebras loop. Orchestration ported from
// cursor-oauth-opencode/src/cursor-index-cloud.ts. See plans/isolated-cursor-harness.md.
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import { unaryProto } from "./lib/hcursor-h2.mjs";
import {
  REPO42_URL, RPC, CONFIG, CURSOR_CODEBASE_STATUS, repoHeaders,
  resolveIndexMetadata, buildRepositoryContext,
  collectUploadFiles, computeRootHash, chunkUploadFiles,
  encodeFastRepoInitHandshakeV2Request, encodeFastUpdateFileV2Request,
  encodeEnsureIndexCreatedRequest, encodeFastRepoSyncCompleteRequest, encodeSearchRepositoryRequest,
  decodeFastRepoInitHandshakeV2Response, isFastUpdateFileV2Success, decodeSearchRepositoryResponse,
  parseConnectError, isCodebaseNotFound,
} from "./lib/hcursor-index.mjs";

export function loadToken() {
  const env = process.env.CURSOR_AUTH_TOKEN || process.env.CURSOR_ACCESS_TOKEN;
  if (env) return env.trim();
  const authPath = join(process.env.HOME || homedir(), ".cursor", "auth.json");
  if (!existsSync(authPath)) throw new Error(`no token: set CURSOR_AUTH_TOKEN or create ${authPath}`);
  const j = JSON.parse(readFileSync(authPath, "utf8"));
  const tok = j.accessToken || j.access_token;
  if (!tok) throw new Error(`no accessToken in ${authPath}`);
  return tok;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Unary repo42 call -> { body, ok, timedOut }. Never throws (transport errors -> ok:false).
async function callRepo42(rpcPath, body, token, timeoutMs) {
  try {
    const res = await unaryProto({ accessToken: token, url: REPO42_URL, path: rpcPath, body, headers: repoHeaders(), timeoutMs });
    return { body: res.body, ok: res.status === 200, timedOut: false, status: res.status };
  } catch (e) {
    return { body: Buffer.alloc(0), ok: false, timedOut: /timeout/i.test(e?.message || ""), status: 0, error: e?.message };
  }
}

export async function handshake(token, context, metadata, files, rootHash) {
  const res = await callRepo42(RPC.handshake, encodeFastRepoInitHandshakeV2Request(context, metadata, files.length, rootHash), token, 60000);
  if (res.timedOut || res.body.length === 0) return { ok: false, reason: res.error || `handshake http ${res.status}`, raw: res };
  const decoded = decodeFastRepoInitHandshakeV2Response(res.body);
  return { ok: true, ...decoded };
}

// Full bootstrap: handshake -> upload chunks -> ensure -> sync. Returns { outcome, uploadCount }.
export async function bootstrap(token, context, metadata, forceUpload = false) {
  const files = collectUploadFiles(context.workspacePath);
  if (files.length === 0) return { outcome: "failed", reason: "no uploadable files", uploadCount: 0 };
  const rootHash = computeRootHash(files);
  const hs = await handshake(token, context, metadata, files, rootHash);
  if (!hs.ok) return { outcome: "failed", reason: hs.reason, uploadCount: 0 };
  if (hs.status !== 2) return { outcome: "failed", reason: `handshake status ${hs.status}`, uploadCount: 0, codebases: hs.codebases };

  const syncTargets = hs.codebases.filter((c) =>
    c.codebaseId && c.status !== CURSOR_CODEBASE_STATUS.COPY_IN_PROGRESS && (forceUpload || c.status !== CURSOR_CODEBASE_STATUS.UP_TO_DATE));
  if (syncTargets.length === 0) {
    return hs.codebases.some((c) => c.status === CURSOR_CODEBASE_STATUS.UP_TO_DATE)
      ? { outcome: "already-indexed", uploadCount: 0 }
      : { outcome: "failed", reason: "no sync targets", uploadCount: 0 };
  }

  const uploadChunks = chunkUploadFiles(files);
  const syncStatuses = [];
  for (const codebase of syncTargets) {
    let failed = 0, total = 0;
    for (const chunk of uploadChunks) {
      total += chunk.length;
      const up = await callRepo42(RPC.updateFile, encodeFastUpdateFileV2Request(codebase.codebaseId, metadata, chunk), token, 60000);
      if (up.timedOut || !up.ok || !isFastUpdateFileV2Success(up.body)) failed += chunk.length;
    }
    syncStatuses.push({ codebaseId: codebase.codebaseId, success: failed === 0 && total > 0, failedUploadCount: failed, totalUploadCount: total });
  }
  if (syncStatuses.some((s) => !s.success)) return { outcome: "failed", reason: "upload failures", uploadCount: 0 };

  const ensure = await callRepo42(RPC.ensureIndex, encodeEnsureIndexCreatedRequest(context, metadata), token, 60000);
  if (ensure.timedOut || !ensure.ok) return { outcome: "failed", reason: "ensure failed", uploadCount: 0 };
  const sync = await callRepo42(RPC.syncComplete, encodeFastRepoSyncCompleteRequest(syncStatuses, metadata), token, 60000);
  if (sync.timedOut || !sync.ok) return { outcome: "failed", reason: "sync-complete failed", uploadCount: 0 };
  return { outcome: "uploaded", uploadCount: syncStatuses.reduce((n, s) => n + s.totalUploadCount, 0) };
}

// Single search call -> { ok, results } | { ok:false, outcome }.
export async function searchOnce(token, query, context, metadata, topK = CONFIG.topK) {
  const res = await callRepo42(RPC.search, encodeSearchRepositoryRequest(query, context, metadata, topK), token, 20000);
  if (res.timedOut) return { ok: false, outcome: "timeout" };
  if (res.body.length === 0) return { ok: false, outcome: "no-results" };
  const err = parseConnectError(res.body);
  if (err) return { ok: false, outcome: isCodebaseNotFound(err) ? "codebase-not-found" : "rpc-error", error: err };
  const results = decodeSearchRepositoryResponse(res.body, metadata.pathEncryptionKey);
  if (results.length === 0) return { ok: false, outcome: "no-results" };
  return { ok: true, results };
}

// Search with bootstrap-on-miss + retry backoff (cursor-index-cloud.ts:467-510).
export async function search(token, query, context, metadata, { bootstrapOnMiss = true } = {}) {
  let r = await searchOnce(token, query, context, metadata);
  if (r.ok) return r;
  const miss = r.outcome === "codebase-not-found" || r.outcome === "no-results";
  if (!bootstrapOnMiss || !miss) return r;
  const boot = await bootstrap(token, context, metadata, r.outcome === "no-results");
  if (boot.outcome !== "already-indexed" && boot.outcome !== "uploaded") return { ok: false, outcome: "bootstrap-failed", boot };
  for (const wait of [1000, 2000, 4000]) {
    await sleep(wait);
    r = await searchOnce(token, query, context, metadata);
    if (r.ok) return r;
    if (r.outcome !== "no-results") break;
  }
  return { ok: false, outcome: "no-results-after-index", boot };
}

// SearchRepositoryV2 returns path + line range + score but no code excerpt, so
// enrich from local disk: read the cited lines. Fast server-side ranking + real code.
function localExcerpt(workspace, relPath, start, end) {
  try {
    const lines = readFileSync(join(workspace, relPath), "utf8").split("\n");
    return lines.slice(Math.max(0, start - 1), Math.min(lines.length, end)).join("\n").slice(0, 1500);
  } catch { return ""; }
}

// Render results as startLine:endLine:path fenced citations (harness trace style).
export function renderResults(results, workspace) {
  return results.filter((r) => r.path || (r.contents || "").trim()).map((r) => {
    const path = r.path.replace(/^\.\//, "");
    const start = r.startLine !== undefined ? Math.max(1, Math.floor(r.startLine)) : 1;
    const end = r.endLine !== undefined ? Math.max(start, Math.floor(r.endLine)) : start;
    const score = Number.isFinite(r.score) ? r.score.toFixed(3) : "0.000";
    const excerpt = (r.contents || "").trim() || (workspace ? localExcerpt(workspace, path, start, end) : "") || "(no excerpt)";
    return ["```" + `${start}:${end}:${path}` + ` (score ${score})`, excerpt, "```"].join("\n");
  }).join("\n\n");
}

// Programmatic entry for the harness: returns { ok, text } | { ok:false, reason }.
export async function indexedSearch(workspace, query, token) {
  const metadata = resolveIndexMetadata(workspace, token);
  if (!metadata.workspaceUri || !metadata.pathEncryptionKey) return { ok: false, reason: "no index metadata" };
  const context = buildRepositoryContext(workspace, metadata);
  const r = await search(token, query, context, metadata);
  if (!r.ok) return { ok: false, reason: r.outcome };
  const text = renderResults(r.results, workspace);
  return text ? { ok: true, text } : { ok: false, reason: "no-results" };
}

function argValue(name, fallback) { const i = process.argv.indexOf(name); return i === -1 ? fallback : process.argv[i + 1]; }

async function main() {
  const workspace = argValue("--workspace", process.cwd());
  const json = process.argv.includes("--json");
  const token = loadToken();
  const metadata = resolveIndexMetadata(workspace, token);
  const context = buildRepositoryContext(workspace, metadata);

  if (process.argv.includes("--handshake")) {
    const files = collectUploadFiles(workspace);
    const hs = await handshake(token, context, metadata, files, computeRootHash(files));
    if (!hs.ok) { console.error(`handshake failed: ${hs.reason}`); process.exit(1); }
    const out = { status: hs.status, files: files.length, codebases: hs.codebases };
    console.log(json ? JSON.stringify(out, null, 2) : `handshake status=${hs.status} files=${files.length} codebases=${hs.codebases.map((c) => c.codebaseId.slice(0, 8) + ":" + c.status).join(", ")}`);
    return;
  }
  if (process.argv.includes("--index")) {
    const boot = await bootstrap(token, context, metadata, process.argv.includes("--force"));
    console.log(json ? JSON.stringify(boot, null, 2) : `index outcome=${boot.outcome} uploads=${boot.uploadCount}${boot.reason ? " (" + boot.reason + ")" : ""}`);
    process.exit(boot.outcome === "uploaded" || boot.outcome === "already-indexed" ? 0 : 1);
  }
  const query = argValue("--search");
  if (query) {
    const r = await search(token, query, context, metadata);
    if (!r.ok) { console.error(`search ${r.outcome}`); process.exit(1); }
    if (json) console.log(JSON.stringify(r.results, null, 2));
    else console.log(renderResults(r.results, workspace));
    return;
  }
  console.error("usage: cursor-index.mjs [--handshake | --index [--force] | --search <query>] [--workspace <dir>] [--json]");
  process.exit(2);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((e) => { process.stderr.write(`cursor-index error: ${e?.message || e}\n`); process.exit(1); });
}
