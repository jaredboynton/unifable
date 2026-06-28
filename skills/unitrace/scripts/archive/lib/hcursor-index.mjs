// Zero-dependency port of Cursor's repo42 RepositoryService wire protocol:
// AES-256-CTR path encryption + the Fast repo index/search protobuf codec.
// Ported from cursor-oauth-opencode/src/cursor-index-{wire,cloud}.ts. Pure Node
// (node:crypto, node:child_process for git/sqlite3). No third-party deps.
import { createCipheriv, createDecipheriv, createHash, createHmac, randomBytes, randomUUID } from "node:crypto";
import { existsSync, readFileSync, readdirSync, lstatSync, realpathSync, mkdirSync, writeFileSync } from "node:fs";
import { basename, dirname, extname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { homedir, release } from "node:os";
import { execFileSync } from "node:child_process";

// ---- RPC catalog / host (cursor-index-cloud.ts:28-31,53-54) ----
export const REPO42_URL = "https://repo42.cursor.sh";
export const RPC = {
  handshake: "/aiserver.v1.RepositoryService/FastRepoInitHandshakeV2",
  updateFile: "/aiserver.v1.RepositoryService/FastUpdateFileV2",
  syncComplete: "/aiserver.v1.RepositoryService/FastRepoSyncComplete",
  ensureIndex: "/aiserver.v1.RepositoryService/EnsureIndexCreated",
  search: "/aiserver.v1.RepositoryService/SearchRepositoryV2",
};
export const CURSOR_CLIENT_VERSION = "3.3.8";
export const CONFIG = { topK: 10, uploadMaxFiles: 300, uploadMaxFileBytes: 256_000, uploadMaxBatchBytes: 900_000 };

// ---- constants (cursor-index-wire.ts:74-88, cursor-index-cloud.ts:32-43) ----
export const CURSOR_CODEBASE_STATUS = { UP_TO_DATE: 1, OUT_OF_SYNC: 2, EMPTY: 3, EMPTY_WITH_COPY_AVAILABLE: 4, COPY_IN_PROGRESS: 5 };
const SIMILARITY_METRIC_TYPE_SIMHASH = 1;
const PATH_KEY_HASH_TYPE_SHA256 = 1;
const FAST_UPDATE_STATUS_SUCCESS = 1;
const FAST_UPDATE_TYPE_ADD = 1;
const FAST_UPDATE_TYPE_BATCH = 4;
const SYNC_CODEBASE_STATUS_SUCCESS = 1;
const SYNC_CODEBASE_STATUS_FAILURE = 2;
const AES_256_CTR = "aes-256-ctr";
const PATH_SEPARATOR_PATTERN = /([./\\])/;
const TEXT_EXTENSIONS = new Set([
  ".cjs", ".css", ".go", ".html", ".js", ".json", ".jsonc", ".jsx", ".md", ".mjs",
  ".py", ".rs", ".sh", ".sql", ".ts", ".tsx", ".txt", ".yaml", ".yml",
]);
const DEFAULT_IGNORES = new Set([
  ".git", ".hg", ".svn", ".cursor", ".omc", ".sisyphus", ".wire-harness-runs",
  "node_modules", "dist", "build", ".next", ".turbo", "coverage",
]);
const SENSITIVE_FILENAMES = new Set([
  ".env", ".env.local", ".env.development", ".env.production", ".npmrc", ".pypirc",
  "credentials.json", "secrets.json",
]);
const CURSOR_GLOBAL_STATE_DB = join(homedir(), "Library/Application Support/Cursor/User/globalStorage/state.vscdb");

// ============================ protobuf writer ============================
// Byte-faithful to the reference: writeString skips empty; writeBool/writeInt32
// always write (incl. 0/false). Do NOT swap for hproto's skipping Writer.
function writeVarint(value, out) {
  let v = value >>> 0;
  while (v > 0x7f) { out.push((v & 0x7f) | 0x80); v >>>= 7; }
  out.push(v);
}
function writeTag(field, wireType, out) { writeVarint((field << 3) | wireType, out); }
function writeString(field, value, out) {
  if (!value) return;
  const bytes = Buffer.from(value, "utf8");
  writeTag(field, 2, out); writeVarint(bytes.length, out);
  for (const b of bytes) out.push(b);
}
function writeBool(field, value, out) { writeTag(field, 0, out); writeVarint(value ? 1 : 0, out); }
function writeInt32(field, value, out) { if (value === undefined) return; writeTag(field, 0, out); writeVarint(value, out); }
function writeDouble(field, value, out) {
  if (value === undefined) return;
  writeTag(field, 1, out);
  const b = Buffer.alloc(8); b.writeDoubleLE(value, 0);
  for (const x of b) out.push(x);
}
function writeMessage(field, bytes, out) {
  writeTag(field, 2, out); writeVarint(bytes.length, out);
  for (const b of bytes) out.push(b);
}

// ============================ protobuf reader ============================
class ProtoReader {
  constructor(bytes) { this.bytes = bytes; this.offset = 0; }
  get done() { return this.offset >= this.bytes.length; }
  readTag() { if (this.done) return undefined; const tag = this.readVarint(); return { field: tag >>> 3, wireType: tag & 0x7 }; }
  readVarint() {
    let shift = 0, result = 0;
    while (this.offset < this.bytes.length) {
      const byte = this.bytes[this.offset++];
      result |= (byte & 0x7f) << shift;
      if ((byte & 0x80) === 0) return result >>> 0;
      shift += 7;
    }
    return result >>> 0;
  }
  readString() { return Buffer.from(this.readBytes()).toString("utf8"); }
  readBytes() { const len = this.readVarint(); const start = this.offset; this.offset += len; return this.bytes.subarray(start, start + len); }
  readFloat() { const v = new DataView(this.bytes.buffer, this.bytes.byteOffset + this.offset, 4).getFloat32(0, true); this.offset += 4; return v; }
  skip(wireType) {
    if (wireType === 0) this.readVarint();
    else if (wireType === 1) this.offset += 8;
    else if (wireType === 2) this.readBytes();
    else if (wireType === 5) this.offset += 4;
    else this.offset = this.bytes.length;
  }
}

// ============================ path crypto ============================
class PathCipher {
  constructor(masterKeyRaw) {
    const masterKey = Buffer.from(masterKeyRaw, "base64url");
    this.macKey = createHash("sha256").update(masterKey).update(Buffer.from([0])).digest();
    this.encKey = createHash("sha256").update(masterKey).update(Buffer.from([1])).digest();
  }
  encrypt(value) {
    const mac = createHmac("sha256", this.macKey).update(value).digest().subarray(0, 6);
    const iv = Buffer.concat([mac, Buffer.alloc(10)]);
    const cipher = createCipheriv(AES_256_CTR, this.encKey, iv);
    const padded = `${value}${"\0".repeat((4 - value.length % 4) % 4)}`;
    const encrypted = Buffer.concat([cipher.update(padded, "utf8"), cipher.final()]);
    return Buffer.concat([mac, encrypted]).toString("base64url");
  }
  decrypt(value) {
    const bytes = Buffer.from(value, "base64url");
    if (bytes.length <= 6) return value;
    const mac = bytes.subarray(0, 6);
    const iv = Buffer.concat([mac, Buffer.alloc(10)]);
    const decipher = createDecipheriv(AES_256_CTR, this.encKey, iv);
    const decrypted = Buffer.concat([decipher.update(bytes.subarray(6)), decipher.final()]).toString("utf8");
    return decrypted.replace(/\0+$/g, "");
  }
}
function makePathCipher(key) { return key ? new PathCipher(key) : undefined; }
export function encryptCursorPath(value, key) {
  const cipher = makePathCipher(key);
  if (!cipher) return value;
  return value.split(PATH_SEPARATOR_PATTERN).map((p) => (PATH_SEPARATOR_PATTERN.test(p) || p === "" ? p : cipher.encrypt(p))).join("");
}
export function decryptCursorPath(value, key) {
  const cipher = makePathCipher(key);
  if (!cipher) return value;
  try {
    return value.split(PATH_SEPARATOR_PATTERN).map((p) => (PATH_SEPARATOR_PATTERN.test(p) || p === "" ? p : cipher.decrypt(p))).join("");
  } catch { return value; }
}

// ============================ keys / metadata ============================
function runSqlite(dbPath, sql) {
  if (!existsSync(dbPath)) return "";
  try { return execFileSync("sqlite3", [dbPath, sql], { encoding: "utf8", timeout: 1500, stdio: ["ignore", "pipe", "ignore"] }).trim(); }
  catch { return ""; }
}
export function readCursorGlobalServerKey() {
  const raw = runSqlite(CURSOR_GLOBAL_STATE_DB, "select value from ItemTable where key='cursorai/serverConfig' limit 1;");
  if (!raw) return undefined;
  try {
    const cfg = JSON.parse(raw);
    return cfg.indexingConfig?.defaultTeamPathEncryptionKey || cfg.indexingConfig?.defaultUserPathEncryptionKey;
  } catch { return undefined; }
}
function isLikelyAesPathKey(value) {
  if (typeof value !== "string" || value.length < 32) return false;
  try { return Buffer.from(value, "base64url").length === 32; } catch { return false; }
}

// Resolve (and persist) index metadata for a workspace. index + search MUST use the
// same key/seed/repoName/workspaceUri, so we cache it under ~/.cache/explore/repo42.
function metadataCachePath(workspacePath) {
  const id = createHash("sha256").update(resolve(workspacePath)).digest("hex").slice(0, 16);
  return join(homedir(), ".cache", "explore", "repo42", `${id}.json`);
}
export function resolveIndexMetadata(workspacePath, accessToken) {
  const root = resolve(workspacePath);
  const cachePath = metadataCachePath(root);
  let stored;
  try { if (existsSync(cachePath)) stored = JSON.parse(readFileSync(cachePath, "utf8")); } catch {}
  let pathEncryptionKey = stored?.pathEncryptionKey;
  if (!isLikelyAesPathKey(pathEncryptionKey)) {
    pathEncryptionKey = (isLikelyAesPathKey(readCursorGlobalServerKey()) && readCursorGlobalServerKey()) || randomBytes(32).toString("base64url");
  }
  const orthogonalTransformSeed = typeof stored?.orthogonalTransformSeed === "number"
    ? stored.orthogonalTransformSeed : Math.floor(Math.random() * Number.MAX_SAFE_INTEGER);
  const repoName = stored?.repoName || randomUUID();
  const workspaceUri = encryptCursorPath(pathToFileURL(root).toString(), pathEncryptionKey);
  const repoOwner = jwtSubject(accessToken) || stored?.repoOwner || "";
  const metadata = { pathEncryptionKey, orthogonalTransformSeed, repoName, repoOwner, workspaceUri };
  try { mkdirSync(dirname(cachePath), { recursive: true }); writeFileSync(cachePath, JSON.stringify(metadata, null, 2)); } catch {}
  return metadata;
}
export function jwtSubject(accessToken) {
  try {
    const payload = String(accessToken || "").split(".")[1];
    if (!payload) return undefined;
    const sub = JSON.parse(Buffer.from(payload, "base64url").toString("utf8")).sub;
    return typeof sub === "string" && sub.trim() ? sub : undefined;
  } catch { return undefined; }
}
function pathKeyHash(metadata) {
  return createHash("sha256").update(`${metadata.pathEncryptionKey}_PATH_KEY_HASH_SHA256`).digest("hex");
}

// ============================ repository context ============================
export function buildRepositoryContext(workspacePath, metadata) {
  const root = resolve(workspacePath);
  let remotes = [];
  try {
    const out = execFileSync("git", ["-C", root, "remote", "-v"], { encoding: "utf8", timeout: 2000, stdio: ["ignore", "pipe", "ignore"] });
    const seen = new Set();
    for (const line of out.split(/\r?\n/)) {
      const m = line.match(/^(\S+)\s+(\S+)\s+\(fetch\)/);
      if (m && !seen.has(m[1])) { seen.add(m[1]); remotes.push({ name: m[1], url: m[2] }); }
    }
  } catch {}
  return {
    workspacePath: root,
    relativeWorkspacePath: ".",
    repoName: metadata.repoName,
    repoOwner: metadata.repoOwner,
    remotes,
    isTracked: false,
    isLocal: true,
  };
}

// ============================ file enumeration / prep ============================
function shouldSkipDir(name) { return DEFAULT_IGNORES.has(name); }
function shouldSkipUploadPath(p) { return p.split(/[\\/]+/).some((part) => DEFAULT_IGNORES.has(part)); }
function shouldSkipUploadFile(p) {
  const name = basename(p).toLowerCase();
  if (SENSITIVE_FILENAMES.has(name)) return true;
  return name.endsWith(".pem") || name.endsWith(".key") || name.endsWith(".p12") || name.endsWith(".pfx");
}
function safeRealpath(p) { try { return realpathSync(p); } catch { return undefined; } }
function isWithinDirectory(root, candidate) { const rel = relative(root, candidate); return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel)); }
function listGitUploadCandidates(root) {
  try {
    const out = execFileSync("git", ["-C", root, "ls-files", "-co", "--exclude-standard"],
      { encoding: "utf8", timeout: 2500, stdio: ["ignore", "pipe", "ignore"], env: { ...process.env, GIT_TERMINAL_PROMPT: "0" } });
    return out.split(/\r?\n/).map((e) => e.trim()).filter(Boolean).sort((a, b) => a.localeCompare(b));
  } catch { return undefined; }
}
function collectFallbackCandidates(root, maxFiles) {
  const candidates = [];
  const walk = (dir) => {
    if (candidates.length >= maxFiles) return;
    let entries; try { entries = readdirSync(dir).sort((a, b) => a.localeCompare(b)); } catch { return; }
    for (const entry of entries) {
      if (candidates.length >= maxFiles) return;
      if (shouldSkipDir(entry)) continue;
      const full = join(dir, entry);
      let st; try { st = lstatSync(full); } catch { continue; }
      if (st.isSymbolicLink()) continue;
      if (st.isDirectory()) walk(full);
      else if (st.isFile()) candidates.push(relative(root, full));
    }
  };
  walk(root);
  return candidates;
}
function normalizeRelativePath(root, p) { const rel = relative(root, p).split(sep).join("/"); return rel ? `./${rel}` : "."; }
function ancestorPaths(relativePath) {
  const normalized = relativePath.replace(/^\.\//, "");
  const dir = dirname(normalized);
  if (!dir || dir === ".") return ["."];
  const parts = dir.split("/").filter(Boolean);
  const ancestors = [];
  for (let i = parts.length; i > 0; i -= 1) ancestors.push(`./${parts.slice(0, i).join("/")}`);
  ancestors.push(".");
  return ancestors;
}
export function collectUploadFiles(root) {
  const rootReal = safeRealpath(root);
  if (!rootReal) return [];
  const candidatePaths = listGitUploadCandidates(rootReal) ?? collectFallbackCandidates(rootReal, CONFIG.uploadMaxFiles);
  const files = [];
  for (const candidate of candidatePaths) {
    if (files.length >= CONFIG.uploadMaxFiles) break;
    if (!candidate || isAbsolute(candidate) || shouldSkipUploadPath(candidate) || shouldSkipUploadFile(candidate)) continue;
    const full = resolve(rootReal, candidate);
    let st; try { st = lstatSync(full); } catch { continue; }
    if (st.isSymbolicLink() || !st.isFile() || st.size > CONFIG.uploadMaxFileBytes) continue;
    const realPath = safeRealpath(full);
    if (!realPath || !isWithinDirectory(rootReal, realPath) || shouldSkipUploadFile(realPath)) continue;
    if (!TEXT_EXTENSIONS.has(extname(realPath).toLowerCase())) continue;
    try {
      const contents = readFileSync(realPath, "utf8");
      const relativePath = normalizeRelativePath(rootReal, realPath);
      files.push({ relativePath, contents, hash: createHash("sha256").update(contents).digest("hex"), ancestorPaths: ancestorPaths(relativePath) });
    } catch {}
  }
  return files.sort((a, b) => a.relativePath.localeCompare(b.relativePath));
}
export function computeRootHash(files) {
  const hash = createHash("sha256");
  for (const f of files) hash.update(f.relativePath).update("\0").update(f.hash).update("\n");
  return hash.digest("hex");
}
export function chunkUploadFiles(files) {
  const chunks = []; let current = []; let bytes = 0;
  for (const f of files) {
    const fb = Buffer.byteLength(f.contents, "utf8");
    if (current.length > 0 && bytes + fb > CONFIG.uploadMaxBatchBytes) { chunks.push(current); current = []; bytes = 0; }
    current.push(f); bytes += fb;
  }
  if (current.length > 0) chunks.push(current);
  return chunks;
}

// ============================ encoders ============================
function encodeRepositoryInfo(context, metadata, overrides = {}) {
  const out = [];
  writeString(1, context.relativeWorkspacePath || ".", out);
  for (const r of context.remotes) writeString(2, r.url, out);
  for (const r of context.remotes) writeString(3, r.name, out);
  writeString(4, metadata.repoName || context.repoName, out);
  writeString(5, metadata.repoOwner || context.repoOwner, out);
  writeBool(6, overrides.isTracked ?? context.isTracked, out);
  writeBool(7, overrides.isLocal ?? context.isLocal, out);
  writeInt32(8, overrides.numFiles, out);
  writeDouble(9, metadata.orthogonalTransformSeed, out);
  writeString(11, metadata.workspaceUri, out);
  return Uint8Array.from(out);
}
export function encodeSearchRepositoryRequest(query, context, metadata, topK = CONFIG.topK) {
  const out = [];
  writeString(1, query, out);
  writeMessage(2, encodeRepositoryInfo(context, metadata), out);
  writeInt32(3, topK, out);
  writeBool(5, true, out);
  return Uint8Array.from(out);
}
function encodeClientRepositoryInfo(metadata) { const out = []; writeDouble(1, metadata.orthogonalTransformSeed ?? 0, out); return Uint8Array.from(out); }
function encodePartialPathItem(relativePath, metadata, hash = "") {
  const out = []; writeString(1, encryptCursorPath(relativePath, metadata.pathEncryptionKey), out); writeString(2, hash, out); return Uint8Array.from(out);
}
function encodeUploadedLocalFile(file, metadata) {
  const fileMessage = [];
  writeString(1, encryptCursorPath(file.relativePath, metadata.pathEncryptionKey), fileMessage);
  writeString(2, file.contents, fileMessage);
  const localFile = [];
  writeMessage(1, Uint8Array.from(fileMessage), localFile);
  writeString(2, file.hash, localFile);
  writeString(3, file.relativePath, localFile);
  return Uint8Array.from(localFile);
}
function encodeFileUpdate(file, metadata) {
  const out = [];
  writeMessage(2, encodeUploadedLocalFile(file, metadata), out);
  for (const a of file.ancestorPaths) writeMessage(3, encodePartialPathItem(a, metadata), out);
  writeInt32(4, FAST_UPDATE_TYPE_ADD, out);
  return Uint8Array.from(out);
}
export function encodeFastRepoInitHandshakeV2Request(context, metadata, fileCount, rootHash) {
  const out = [];
  writeMessage(1, encodeRepositoryInfo(context, metadata, { isTracked: false, isLocal: true, numFiles: fileCount }), out);
  writeString(2, rootHash, out);
  writeInt32(3, SIMILARITY_METRIC_TYPE_SIMHASH, out);
  writeString(5, pathKeyHash(metadata), out);
  writeInt32(6, PATH_KEY_HASH_TYPE_SHA256, out);
  writeBool(7, false, out);
  return Uint8Array.from(out);
}
export function encodeFastUpdateFileV2Request(codebaseId, metadata, files) {
  const out = [];
  writeMessage(1, encodeClientRepositoryInfo(metadata), out);
  writeString(2, codebaseId, out);
  if (files.length === 1 && files[0]) {
    const file = files[0];
    writeMessage(4, encodeUploadedLocalFile(file, metadata), out);
    for (const a of file.ancestorPaths) writeMessage(5, encodePartialPathItem(a, metadata), out);
    writeInt32(6, FAST_UPDATE_TYPE_ADD, out);
  } else {
    writeInt32(6, FAST_UPDATE_TYPE_BATCH, out);
    for (const file of files) writeMessage(7, encodeFileUpdate(file, metadata), out);
  }
  return Uint8Array.from(out);
}
export function encodeEnsureIndexCreatedRequest(context, metadata) {
  const out = [];
  writeMessage(1, encodeRepositoryInfo(context, metadata, { isTracked: false, isLocal: true, numFiles: 0 }), out);
  return Uint8Array.from(out);
}
export function encodeFastRepoSyncCompleteRequest(codebases, metadata) {
  const out = [];
  for (const c of codebases) {
    const status = [];
    writeString(1, c.codebaseId, status);
    writeInt32(2, c.success ? SYNC_CODEBASE_STATUS_SUCCESS : SYNC_CODEBASE_STATUS_FAILURE, status);
    writeInt32(3, SIMILARITY_METRIC_TYPE_SIMHASH, status);
    writeString(5, pathKeyHash(metadata), status);
    writeInt32(6, PATH_KEY_HASH_TYPE_SHA256, status);
    writeInt32(7, c.failedUploadCount, status);
    writeInt32(8, 0, status);
    writeInt32(9, c.totalUploadCount, status);
    writeInt32(10, 0, status);
    writeInt32(11, 0, status);
    writeInt32(12, 0, status);
    writeBool(13, false, status);
    writeMessage(1, Uint8Array.from(status), out);
  }
  return Uint8Array.from(out);
}

// ============================ decoders ============================
export function decodeConnectUnaryBody(payload) {
  if (payload.length < 5) return payload;
  let offset = 0;
  while (offset + 5 <= payload.length) {
    const flags = payload[offset];
    const messageLength = new DataView(payload.buffer, payload.byteOffset + offset + 1, 4).getUint32(0, false);
    const frameEnd = offset + 5 + messageLength;
    if (frameEnd > payload.length) return payload;
    if ((flags & 0b0000_0010) === 0) return payload.subarray(offset + 5, frameEnd);
    offset = frameEnd;
  }
  return payload;
}
function decodeCursorPosition(bytes) {
  const r = new ProtoReader(bytes); const pos = {};
  while (!r.done) { const t = r.readTag(); if (!t) break; if (t.field === 1 && t.wireType === 0) pos.line = r.readVarint(); else r.skip(t.wireType); }
  return pos;
}
function decodeCursorRange(bytes) {
  const r = new ProtoReader(bytes); const range = {};
  while (!r.done) {
    const t = r.readTag(); if (!t) break;
    if (t.field === 1 && t.wireType === 2) range.startLine = decodeCursorPosition(r.readBytes()).line;
    else if (t.field === 2 && t.wireType === 2) range.endLine = decodeCursorPosition(r.readBytes()).line;
    else r.skip(t.wireType);
  }
  return range;
}
function decodeDetailedLine(bytes) {
  const r = new ProtoReader(bytes); const line = {};
  while (!r.done) {
    const t = r.readTag(); if (!t) break;
    if (t.field === 1 && t.wireType === 2) line.text = r.readString();
    else if (t.field === 2 && t.wireType === 5) line.lineNumber = r.readFloat();
    else r.skip(t.wireType);
  }
  return line;
}
function decodeCodeBlock(bytes, key) {
  const r = new ProtoReader(bytes); const detailedLines = [];
  let path = "", contents = "", startLine, endLine;
  while (!r.done) {
    const t = r.readTag(); if (!t) break;
    if (t.field === 1 && t.wireType === 2) path = r.readString();
    else if (t.field === 3 && t.wireType === 2) { const range = decodeCursorRange(r.readBytes()); startLine = range.startLine; endLine = range.endLine; }
    else if (t.field === 4 && t.wireType === 2) contents = r.readString();
    else if (t.field === 8 && t.wireType === 2) detailedLines.push(decodeDetailedLine(r.readBytes()));
    else r.skip(t.wireType);
  }
  if (!contents && detailedLines.length > 0) contents = detailedLines.map((l) => l.text ?? "").join("\n");
  if (startLine === undefined) startLine = detailedLines.find((l) => l.lineNumber !== undefined)?.lineNumber;
  if (endLine === undefined && startLine !== undefined && contents) endLine = startLine + Math.max(0, contents.split(/\r?\n/).length - 1);
  return { path: decryptCursorPath(path, key), contents, startLine, endLine };
}
function decodeCodeResult(bytes, key) {
  const r = new ProtoReader(bytes); let block, score = 0;
  while (!r.done) {
    const t = r.readTag(); if (!t) break;
    if (t.field === 1 && t.wireType === 2) block = decodeCodeBlock(r.readBytes(), key);
    else if (t.field === 2 && t.wireType === 5) score = r.readFloat();
    else r.skip(t.wireType);
  }
  if (!block?.path && !block?.contents) return undefined;
  return { ...block, score };
}
function decodeRepositoryCodebaseInfo(bytes) {
  const r = new ProtoReader(bytes); const c = { codebaseId: "", status: 0 };
  while (!r.done) {
    const t = r.readTag(); if (!t) break;
    if (t.field === 1 && t.wireType === 2) c.codebaseId = r.readString();
    else if (t.field === 2 && t.wireType === 0) c.status = r.readVarint();
    else r.skip(t.wireType);
  }
  return c;
}
export function decodeFastRepoInitHandshakeV2Response(payload) {
  const r = new ProtoReader(decodeConnectUnaryBody(payload)); const result = { status: 0, codebases: [] };
  while (!r.done) {
    const t = r.readTag(); if (!t) break;
    if (t.field === 1 && t.wireType === 0) result.status = r.readVarint();
    else if (t.field === 2 && t.wireType === 2) result.codebases.push(decodeRepositoryCodebaseInfo(r.readBytes()));
    else r.skip(t.wireType);
  }
  return result;
}
export function decodeFastUpdateFileV2ResponseStatus(payload) {
  const r = new ProtoReader(decodeConnectUnaryBody(payload));
  while (!r.done) { const t = r.readTag(); if (!t) break; if (t.field === 1 && t.wireType === 0) return r.readVarint(); r.skip(t.wireType); }
  return 0;
}
export function isFastUpdateFileV2Success(payload) { return decodeFastUpdateFileV2ResponseStatus(payload) === FAST_UPDATE_STATUS_SUCCESS; }
export function decodeSearchRepositoryResponse(payload, key) {
  const r = new ProtoReader(decodeConnectUnaryBody(payload)); const results = [];
  while (!r.done) {
    const t = r.readTag(); if (!t) break;
    if (t.field === 1 && t.wireType === 2) { const res = decodeCodeResult(r.readBytes(), key); if (res) results.push(res); }
    else r.skip(t.wireType);
  }
  return results;
}

// Connect JSON error body (e.g. "codebase not found") — repo42 returns these as the body.
export function parseConnectError(body) {
  if (body.length === 0 || body[0] !== 0x7b) return undefined;
  try { const p = JSON.parse(Buffer.from(body).toString("utf8")); if (typeof p?.code === "string") return p; } catch {}
  return undefined;
}
export function isCodebaseNotFound(error) {
  const detail = error?.details?.[0]?.debug?.details?.detail ?? "";
  if (typeof detail === "string" && detail.toLowerCase().includes("codebase not found")) return true;
  return error?.code === "invalid_argument" && /codebase\s+not\s+found/i.test(error?.message ?? "");
}

export const repoHeaders = () => ({
  "x-cursor-client-version": CURSOR_CLIENT_VERSION,
  "x-cursor-client-type": "ide",
  "x-cursor-client-os": process.platform,
  "x-cursor-client-arch": process.arch,
  "x-cursor-client-os-version": release(),
  "x-cursor-client-device-type": "desktop",
  "x-cursor-timezone": Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
});
