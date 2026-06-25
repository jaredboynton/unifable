# Isolated Cursor Harness — replace `cursor-agent` CLI in `trace.sh`

**Status:** planning → implementation
**Owner:** explore skill
**Goal:** Replace the `cursor-agent` CLI dependency inside `scripts/trace.sh` with a custom
**zero-dependency** Node `.mjs` harness that reimplements cursor-agent *ask mode*: it drives
Cursor's `AgentService/Run` directly over HTTP/2 Connect+protobuf, runs the client-side
read-only tool loop (read / grep / ls, optional semantic codebase search), produces trace
output equivalent to today's pipeline, and is faster + lighter than spawning `cursor-agent`.

All load-bearing facts below carry `file:line` citations from this exploration session.

---

## 1. Why this is possible (architecture ground truth)

Cursor's agent runs the **model loop server-side** but **executes tools client-side**. During a
`Run` stream the server emits `execServerMessage` frames carrying tool *arguments*; the client
must execute them locally and stream back `ExecClientMessage` *results*, or the stream stalls.

- Server message cases: `interactionUpdate` (text/thinking/token deltas), `execServerMessage`
  (tool exec), `kvServerMessage` (blob store), `conversationCheckpointUpdate`
  — `references/agent-events.md:5-14`, proxy.ts:1095-1116.
- Before any text, the server sends `requestContextArgs`; client must answer with
  `requestContextResult` (workspace env + project layout) or the stream hangs
  — `references/agent-events.md:39-41`, proxy.ts:1192-1201.
- The exec tool surface (server→client oneof) is grounded in
  `agent_pb.ts:6885-6997`: `shell_args=2, write_args=3, delete_args=4, grep_args=5,
  read_args=7, ls_args=8, diagnostics_args=9, request_context_args=10, mcp_args=11,
  shell_stream_args=14, background_shell_spawn_args=16, fetch_args=20, ...`.
- `SemSearchToolArgs` exists in the proto (`agent_pb.ts:416`) but is **not** in the client exec
  oneof, and `proxy.ts` has **no** codebase/semantic handling (grep for
  `codebase|semantic|repo42` over proxy.ts → 0 hits). Semantic search is resolved
  **server-side** against the repo42 index, primed by `repositoryInfo` we send in
  `RequestContext` (proxy.ts:733, `buildRepositoryIndexingInfo` proxy.ts:684-698).

**Key contrast with the cursor-api skill:** that skill's `exec_handlers.mjs` *rejects* every
native read/grep/ls/shell tool (`REJECT_REASON = "Tool not available… Use the MCP tools…"`,
`exec_handlers.mjs:33-34,102-145`) because it substitutes OpenCode's own MCP tools. Our harness
must do the **opposite**: implement the **success** path for read/grep/ls so the model can
actually explore the repo — exactly what `cursor-agent` does internally.

### Today's pipeline we are replacing
`trace.sh` builds a fixed prompt, runs `cursor-agent --print --trust --force
--disable-project-configs --sandbox disabled --exclude-tools
shellToolCall,writeShellStdinToolCall,editToolCall,applyAgentDiffToolCall,deleteToolCall
--mode ask --model composer-2.5-fast --workspace <ws> --output-format json`, then
`jq '.result'` → `expand_citations.py` → `out.md` (trace.sh:279-327, confirmed via explore
trace run 20260624T073513). The harness replaces *only* the `cursor-agent` invocation; run-dir
artifacts, hermetic HOME, citation hydration, and footer stay.

---

## 2. The zero-dependency constraint

The explore repo ships pure `.mjs`/`.sh`/`.py` with **no `package.json` and no `node_modules`**
(`scripts/` listing this session). The cursor-api helpers cannot be reused as-is: they
`import { create, toBinary } from "@bufbuild/protobuf"` and run under `node --import tsx`
(`run_request.mjs:1`, `exec_handlers.mjs:1`, `agent_stream.mjs:6`) — both are third-party
deps. Therefore the harness must encode/decode protobuf **without** bufbuild.

**Precedent that this is tractable:** `cursor-index-wire.ts` already hand-rolls a minimal
protobuf codec (`writeString`, `writeMessage`, `writeInt32`, `writeDouble`, a `reader.readBytes`
loop — `cursor-index-wire.ts:395-475,625-726`) with **zero** protobuf deps. We follow the same
pattern, scoped to the Run subset. The protobuf wire format is fully specified (tag =
`(field<<3)|wire`, wire types 0/1/2/5, length-delimited strings/bytes/sub-messages, maps as
repeated key/value entries, unknown fields skipped by wire type) — see prior art
https://protobuf.dev/programming-guides/encoding/ . Connect framing (`[1b flags][4b BE len]
[payload]`, end-stream flag in bit 2 carrying a JSON trailer) — see prior art
https://connectrpc.com/docs/protocol/ .

---

## 3. Protobuf strategy — frontier decision (T3 vs T4)

| Approach | What ships | Pros | Cons | Verdict |
|---|---|---|---|---|
| **T3 hand-rolled codec** | ~300-line pure-JS `proto.mjs` covering the Run subset | truly zero-dep, tiny, no build step, matches `cursor-index-wire.ts` precedent | must transcribe field numbers from `agent_pb.ts`; risk of wire mistakes → mitigated by oracle test | **RECOMMENDED** |
| **T4 build-time bundle** | esbuild-bundled `agent_pb` + bufbuild into one `.mjs` | reuses generated schema, no manual field numbers | adds a build toolchain + ~hundreds of KB vendored generated code into a "skill script"; bufbuild is still vendored code; heavier; foreign to repo conventions | reject unless T3 wire-validation fails |

**Decision basis:** the Run subset is small and the field numbers are mechanically available as
`@generated from field: <type> <name> = <N>;` comments throughout `agent_pb.ts` (e.g.
`agent_pb.ts:72-97` for grep fields, `6885-6997` for the exec oneof). Hand-rolling (T3) keeps the
artifact a single dependency-free file consistent with the repo. T4 is the fallback only if the
wire format proves too intricate to transcribe reliably.

### Judge frontiers folded in
- **T1 (event-streaming tool loop):** the harness is inherently streaming — it parses Connect
  frames incrementally (`createConnectFrameParser`, connect.mjs:20-37) and replies to exec frames
  as they arrive, holding only the growing text buffer + blob map in memory. Adopt this shape.
- **T2 (deterministic replay):** persist every raw server frame and every client reply to the
  run dir (`frames.ndjson`). A `--replay <dir>` mode re-feeds recorded server frames to the
  decoder/tool loop with the network stubbed, giving a deterministic equivalence test with no
  token spend. Adopt as the primary validation gate.

---

## 4. Harness design

### 4.1 File layout (all under `scripts/`, all zero-dep `.mjs`)
```
scripts/cursor-harness.mjs        # entry: args, auth, run dir, drives the loop, prints result
scripts/lib/hproto.mjs            # hand-rolled protobuf reader/writer (wire primitives)
scripts/lib/hcursor-proto.mjs     # encode/decode of the Run subset messages (field numbers here)
scripts/lib/hcursor-h2.mjs        # in-process node:http2 Connect client (no subprocess bridge)
scripts/lib/htools.mjs            # read-only tool executors: read, grep, ls (+ glob)
scripts/lib/hindex.mjs            # OPTIONAL phase 2: repo42 cloud index + SearchRepositoryV2
scripts/test/proto-oracle.test.mjs   # byte-equality vs @bufbuild oracle (dev-only, may use tsx)
scripts/test/harness-replay.test.mjs # deterministic replay equivalence
```

### 4.2 Transport
Use `node:http2` **in-process** (no `h2-bridge.mjs` subprocess). The bridge exists because
Bun/Python/Rust can't do h2 reliably (`h2-bridge-protocol.md:6-10`); pure Node can. Headers
match h2-bridge.mjs:111-129: `:method POST`, `:path /agent.v1.AgentService/Run`, `authorization
Bearer <token>`, `content-type application/connect+proto`, `te trailers`, `x-ghost-mode true`,
`x-cursor-client-type cli`, `x-cursor-client-version cli-2026.01.09-231024f`, `x-request-id
<uuid>`, `connect-protocol-version 1`. Connect framing: `[1b flags][4b BE len][payload]`,
end-stream flag `0b10` carries a JSON error trailer (connect.mjs:3-37, stream_parse.mjs:22-35).
Heartbeat `clientHeartbeat` frame every 5s (run_agent_once.mjs:30-32). Idle timeout 120s after
first activity (h2-bridge.mjs:93-98).

### 4.3 Auth
Read `accessToken` from `~/.cursor/auth.json` (hermetic-home symlinks it, hermetic-home.sh:44-50;
bench reads it the same way, bench-trace-sandbox.sh:42-44). Honor `CURSOR_AUTH_TOKEN` /
`CURSOR_ACCESS_TOKEN` if set. JWT refresh is out of scope v1 (assume valid token, same as the CLI
path the bench uses).

### 4.4 Run request
Mirror `buildCursorRequest` (proxy.ts:905-962) / `run_request.mjs:15-64`:
`AgentClientMessage{ runRequest = AgentRunRequest{ conversationState (empty: rootPromptMessages =
[system blob], turns=[]), action.userMessageAction.userMessage{text,messageId}, modelDetails{
modelId,displayModelId,displayName}, conversationId } }`. System prompt = ask-mode tracing prompt
(see 4.7). The system message is stored as a blob; `rootPromptMessagesJson` holds its sha256 id
(blob_store.mjs:7-11, proxy.ts:784-822). The server fetches it back via `kvServerMessage.getBlobArgs`.

### 4.5 Server-message handling (the loop)
Decode each frame to `AgentServerMessage` and dispatch (proxy.ts:1095-1116):
- `interactionUpdate` → append `textDelta.text`; capture `thinkingDelta` separately
  (exec_handlers.mjs:36-45).
- `kvServerMessage.getBlobArgs/setBlobArgs` → serve/store from the in-memory blob map
  (proxy.ts:1157-1182).
- `execServerMessage` → see 4.6.
- `conversationCheckpointUpdate` → optional token accounting (proxy.ts:1108-1115).
- end-stream → parse JSON error trailer; resolve.

### 4.6 Tool executors (read-only success path — the core new work)
Reply per exec case with `ExecClientMessage{ id, execId, message:{ <result> } }`
(proxy.ts:1339-1353). Differences from proxy.ts: implement **success** for read/grep/ls.

| exec case | our behavior |
|---|---|
| `requestContextArgs` | success `RequestContextResult` with minimal `RequestContext` (env+projectLayout), mirroring `buildMinimalRequestContext` (request_context.mjs:12-56) + repositoryInfo for indexing |
| `readArgs` | **read file** (offset/limit honored), return `ReadResult` success with contents |
| `grepArgs` | **run ripgrep-equivalent** (spawn `rg` read-only, or JS scan), return `GrepResult` success: files/matches/total_files/truncated (`agent_pb.ts:72-97`) |
| `lsArgs` | **list dir**, return `LsResult` success (tree/entries) |
| `shell*Args`, `writeArgs`, `deleteArgs`, `editArgs`, `applyAgentDiff*`, `writeShellStdinArgs`, `backgroundShellSpawnArgs` | **reject** (read-only; mirrors `--exclude-tools`) using the rejected/error shapes proxy.ts:1223-1300 |
| `mcpArgs` | reject/no-op (no MCP servers) |
| `fetchArgs` | reject (no network egress for tools) |
| `diagnosticsArgs` | empty success (proxy.ts:1309-1312) |

All file access is confined to the workspace root (path containment check like
cursor-index.ts:88-91) → satisfies T2 read-only sandbox policy. `grep` may shell out to `rg`
read-only; if forbidden, fall back to a pure-JS recursive scan reusing the ignore set in
cursor-index.ts:38-50.

### 4.7 System prompt (behavioral fidelity)
To reproduce ask-mode trace quality + the `## Flow / ## Code references / ## Key files` structure
that `expand_citations.py` / trace.sh completion detection expect, the harness sends a tracing
system prompt. Source options (exploration step): (a) extract cursor-agent's ask system prompt
from the bundled CLI JS (`~/.local/share/cursor-agent/versions/<v>/*.index.js` — confirmed
greppable, references RepositoryService/SemSearch this session); (b) author one matching the
current `trace.sh` instruction block (trace.sh:291-299) plus the citation format. Start with (b),
optionally refine with (a).

### 4.8 Semantic codebase search (phased)
- **Phase 1 (ship first):** no client indexing. read/grep/ls already let the model trace any
  repo. If the repo is already indexed by Cursor IDE, server-side semantic search still works
  because we forward `repositoryInfo` (remoteUrls + keys) in RequestContext.
- **Phase 2 (optional parity):** replicate cursor-agent local indexing via `hindex.mjs`:
  RPCs `FastRepoInitHandshakeV2`, `FastUpdateFileV2`, `FastRepoSyncComplete`, `EnsureIndexCreated`
  (`cursor-index-cloud.ts:28-31`), search `SearchRepositoryV2` topK 10 (config.ts:53-56). Path
  privacy via AES-256-CTR keyed by `pathEncryptionKey`+`orthogonalTransformSeed`
  (cursor-index-wire.ts:90-118); keys reusable from Cursor IDE state
  (`state.vscdb`/`workspaceStorage`, cursor-index-wire.ts:67-68) or freshly generated. Embeddings
  are **server-side** (we upload contents+hashes). This is documented now; built only if Phase 1
  trace quality is insufficient.

### 4.9 trace.sh integration
Add `EXPLORE_TRANSPORT=harness` branch alongside `cli`/`acp` (trace.sh:311-331): invoke
`node scripts/cursor-harness.mjs --prompt-file … --out … --raw … --err … --workspace … --model …`
under hermetic HOME, then the existing `expand_citations.py` + `print_done` path is unchanged.
Default transport stays `cli` until the harness is validated; flip default after benchmarks pass.

---

## 5. Exploration steps (what's verified vs what remains)

Verified this session (cited above): server-side loop + client tool exec; exec oneof field
numbers; requestContext requirement; kv-blob handshake; Connect framing; auth path; indexing RPCs
+ keying + CLI parity; zero-dep precedent; benchmark harness shape.

Remaining to close during implementation:
1. **Full field-number table** for the encode/decode subset (extract from `agent_pb.ts` `@generated
   from field` comments). Capture in `hcursor-proto.mjs` header comment.
2. **read/grep/ls success message shapes** — exact field numbers for `ReadToolResult` success
   (content/line fields), `GrepToolSuccess`, `LsSuccess` (tree vs entries). Extract from proto.
3. **Live probe**: does a real `Run` actually emit `read_args/grep_args/ls_args` for a tracing
   prompt in `ask` mode, or does it lean on server-side semantic search? Record frames to
   `frames.ndjson` on the first live run and inspect. (Drives whether Phase 2 is needed.)
4. **System prompt** that yields the structured citation output (4.7).
5. Confirm `composer-2.5-fast` accepts this client-tool configuration (vs needing a capability
   flag to advertise which tools the client can run).

---

## 6. Validation steps (deterministic gates)

1. **Proto oracle (T3 gate):** `scripts/test/proto-oracle.test.mjs` encodes the same
   `AgentClientMessage` (run request, and each ExecClientMessage result) with both the
   hand-rolled codec and `@bufbuild/protobuf` (dev-only, tsx allowed) and asserts
   **byte-identical** output. Also round-trips: decode a captured real server frame with both,
   assert structural equality. This is the wire-correctness gate.
2. **Replay equivalence (T2 gate):** record frames from one live run; `--replay` re-runs the
   decoder + tool loop offline and reproduces the same `out.md` deterministically.
3. **Functional trace:** `EXPLORE_TRANSPORT=harness scripts/trace.sh "<q>"` returns non-empty
   markdown with ≥1 `startLine:endLine:path` citation, **no `cursor-agent` process spawned**
   (assert via process list), **no `node_modules`** present.
4. **Equivalence vs cursor-agent:** run the same question through both transports; compare that
   both cite overlapping key files (semantic equivalence, not byte equality).
5. **Existing suites:** `scripts/test-trace-inputs.sh` and friends still pass with the harness
   transport.

---

## 7. Benchmark plan

The bench README has **no results table yet** (`benchmarks/2026-06-24-trace-sandbox/README.md` is
instructions only). So:
1. **Generate the cursor-agent baseline** first: `EXPLORE_BENCH_RUNS=5
   scripts/bench-trace-sandbox.sh` (cli transport) → record median/mean wall-clock + out_bytes.
2. **Add a harness mode** to the bench (or a sibling `bench-trace-transport.sh`) that runs the
   same query via `EXPLORE_TRANSPORT=harness`, interleaved with cli to reduce ordering bias
   (same structure as bench-trace-sandbox.sh:90-95).
3. Compare: wall-clock median (expect harness faster — no CLI/Node-process spawn, no
   project-config scan), peak memory, output completeness (cited files overlap).
4. Write the results table into `benchmarks/2026-06-24-trace-sandbox/README.md` (and/or a new
   dated dir) with both transports side by side.

Success = harness median wall-clock ≤ cursor-agent median, with trace quality (cited files)
equivalent, zero dependencies, read-only guarantees intact.

---

## 8. Skill-update deliverable (cursor-api)

New facts discovered this session to fold into `~/.claude/skills/cursor-api`:
- **repo-index.md / new reference:** the full local-indexing flow — RPCs
  `FastRepoInitHandshakeV2/FastUpdateFileV2/FastRepoSyncComplete/EnsureIndexCreated` +
  `SearchRepositoryV2` (topK 10), AES-256-CTR path encryption (mac-derived IV, 6-byte tag),
  `orthogonalTransformSeed`, key reuse from Cursor IDE `state.vscdb`, server-side embeddings,
  codebase status codes (cursor-index-wire.ts:74-80). Source files cited above.
- **transport-matrix.md:** note `cursor-agent` CLI is shipped as bundled JS chunks (not a
  compiled binary) under `~/.local/share/cursor-agent/versions/<v>/`, and references the same
  `agent.v1`/`aiserver.v1` protocols (confirmed grep this session).
- **agent-events.md:** document the **client tool-execution success path** (read/grep/ls), not
  just the reject path, since that's required to actually use Cursor's native tools.
- Add a pointer to this harness as a worked example of a zero-dep, in-process (no h2-bridge) Run
  client with a real tool loop.

---

## 9. Phased roadmap (status)

- **P0 — codec foundation [DONE]:** `hproto.mjs` + `hcursor-proto.mjs` (incl. a
  `google.protobuf.Value` codec); `scripts/test/proto-oracle.test.mjs` passes 51/51,
  `encodeValue` byte-identical to `@bufbuild` `toBinary(ValueSchema)`. Run via
  `scripts/test/run-oracle.sh`.
- **P1 — transport + minimal loop [DONE]:** `hcursor-h2.mjs` + requestContext + kv-blob +
  textDelta assembly; live Run returns text.
- **P2 — read-only tools [DONE]:** read/grep/ls + `codebase_search` MCP (→ `search.sh`);
  functional traces with `startLine:endLine:path` citations. shell-stream is rejected (success
  reply hangs the model in a single-turn Run; see §6 note).
- **P3 — trace.sh integration + replay [DONE]:** `EXPLORE_TRANSPORT=harness` branch;
  `--replay` reproduces a recorded run byte-for-byte; `--self-test` replays a bundled fixture
  offline.
- **P4 — benchmark [DONE]:** `scripts/bench-trace-transport.sh` →
  `benchmarks/2026-06-24-trace-transport/`. Harness median 62.69s vs cli 36.39s (~72% slower,
  more reliable, equivalent citations). **Default stays `cli`** — the harness is lighter
  (zero-dep, no CLI) but not faster, because `codebase_search` uses a local Cerebras loop vs
  cursor-agent's server-side `sem_search` index.
- **P5 — skill updates [DONE]:** cursor-api `agent-events.md` (client tool-success path +
  shellStream caveat + custom-MCP `google.protobuf.Value` schema) and `transport-matrix.md`
  (CLI = bundled JS chunks; harness as worked example). *Remaining:* full local-indexing
  reference (RPCs + AES path encryption) in `repo-index.md`.
- **P6 — server-side semantic indexing [DONE]:** zero-dep repo42 client
  (`scripts/lib/hcursor-index.mjs` + `scripts/cursor-index.mjs`) indexes the workspace via the
  Fast repo RPCs and queries `SearchRepositoryV2` (sub-second). Codec byte-identical to the
  cursor-oauth-opencode reference (`scripts/test/index-codec.test.mjs`, 8/8). `EXPLORE_INDEX=repo42`
  routes harness `codebase_search` to the server index (fallback: `search.sh`). AES-256-CTR path
  encryption, IDE headers, in-process unary HTTP/2 (no h2-bridge). Excerpts enriched from local
  disk (the fast index returns path+range+score only). See cursor-api `references/repo-index.md`.

## 9b. Internal tool mapping (transparent to the model)

The model's full tool set is the `ToolCall` oneof (agent_pb.ts): shell, delete, glob, grep,
read, edit, ls, read_lints, mcp, **sem_search**, create_plan, web_search, task,
list_mcp_resources, read_mcp_resource, apply_agent_diff, ask_question, fetch, switch_mode,
exa_search. But only a subset arrives as **client-exec** `ExecServerMessage`
(agent_pb.ts ExecServerMessage oneof, verified this session): `shell_args=2, write=3, delete=4,
grep=5, read=7, ls=8, diagnostics=9, request_context=10, mcp=11, shell_stream=14, bg_shell=16,
list_mcp_resources=17, read_mcp_resource=18, fetch=20, record_screen=21, computer_use=22,
write_shell_stdin=23`. **`sem_search`/`glob`/`web_search`/`exa` are NOT client-exec** — they are
server-side (sem_search uses the repo42 index we don't have) or interaction-only. The only
client-side extensibility point is `mcp_args` (11).

Three options were evaluated (user request): (1) expose real fast tools; (2) route to search.sh;
(3) remap tools the model thinks it has. Chosen **combination**:

| Model tool | Internal mapping |
|---|---|
| read / grep / ls | native local executors (`htools.mjs`), workspace-confined — fast, zero-dep |
| shell / shell_stream | read-only local exec with an allowlist mirroring unifable's `bash_classify.py` (blocks command/process substitution, dangerous env vars, sudo, mutating cmds). Non-streaming `shellArgs` works; streaming `shellStream` has an extra completion handshake still open (currently rejected so the model falls back) |
| **sem_search (server-side, no index)** | can't intercept directly → instead register a custom **`codebase_search` MCP tool** in `RequestContext.tools` and route `mcp_args` for it to **search.sh** (Cerebras gpt-oss-120b). Gives the model real semantic search with no repo42 index. |
| write / delete / fetch / bg-shell | rejected (read-only) with the correct per-tool result type |
| any other exec (mcp-resources, record/computer-use, write-shell-stdin) | empty result at the same field — CRITICAL: the model batches tool calls, so any unanswered exec stalls the whole stream |

This realizes options 1+2+3: native fast tools (1), search.sh via MCP (2), and a remap spine that
intercepts every exec and maps it to the best local action (3). The model never learns of the
remap — it just sees a working `codebase_search` tool plus its native read/grep/ls.

## 10. Risks / open questions
- Model may not drive native read/grep/ls without a capability advertisement → close via live
  probe (5.3); if so, find how the CLI advertises tools in the bundled JS and replicate.
- Wire-format transcription errors → mitigated by the byte-equality oracle (6.1).
- Token/JWT expiry mid-run → v1 assumes valid token; add refresh later if needed.
- Semantic-search parity needs Phase 2; Phase 1 may already match for read/grep-driven tracing.
