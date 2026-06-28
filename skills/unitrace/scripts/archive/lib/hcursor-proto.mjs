// Zero-dependency encode/decode for the Cursor AgentService/Run message subset.
// Field numbers transcribed from bufbuild-generated agent_pb.ts (agent.v1), via
// scripts/test/extract-proto-fields.mjs. Validated byte-for-byte against
// @bufbuild/protobuf in scripts/test/proto-oracle.test.mjs.
//
// Envelopes:
//   AgentClientMessage  oneof: run_request=1 exec_client_message=2 kv_client_message=3 client_heartbeat=7
//   AgentServerMessage  oneof: interaction_update=1 exec_server_message=2 conversation_checkpoint_update=3 kv_server_message=4
//   AgentRunRequest     conversation_state=1 action=2 model_details=3 conversation_id=5 custom_system_prompt=8
//   ConversationStateStructure  root_prompt_messages_json=1(repeated bytes) turns=8 token_details=5
//   ConversationAction  user_message_action=1
//   UserMessageAction   user_message=1 request_context=2
//   UserMessage         text=1 message_id=2
//   ModelDetails        model_id=1 display_model_id=3 display_name=4
//   ExecServerMessage   id=1 exec_id=15 ; args oneof shell=2 write=3 delete=4 grep=5 read=7 ls=8 diagnostics=9 request_context=10 mcp=11 shell_stream=14 bg_shell=16 fetch=20
//   ExecClientMessage   id=1 exec_id=15 ; result oneof shell=2 write=3 delete=4 grep=5 read=7 ls=8 diagnostics=9 request_context=10 mcp=11 bg_shell=16 fetch=20
//   KvServerMessage     id=1 get_blob_args=2 set_blob_args=3
//   KvClientMessage     id=1 get_blob_result=2 set_blob_result=3
//   InteractionUpdate   text_delta=1 thinking_delta=4 token_delta=8
import os from "node:os";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { Writer, Reader, forEachField, WIRE } from "./hproto.mjs";

const enc = new TextEncoder();

// google.protobuf.Value codec (struct.proto well-known type). Cursor encodes MCP
// tool input_schema and MCP arg values as Value, not raw JSON (proxy.ts:621,633).
//   Value: null=1 number=2(double) string=3 bool=4 struct=5 list=6
//   Struct: fields=1 map<string,Value> ; ListValue: values=1 repeated Value
export function encodeValue(v) {
  const w = new Writer();
  if (v === null || v === undefined) { w.tag(1, WIRE.VARINT); w._push(Buffer.from([0])); }
  else if (typeof v === "string") { w.bytes(3, Buffer.from(v, "utf8")); }
  else if (typeof v === "number") { w.double(2, v); }
  else if (typeof v === "boolean") { w.tag(4, WIRE.VARINT); w._push(Buffer.from([v ? 1 : 0])); }
  else if (Array.isArray(v)) {
    const lv = new Writer();
    for (const el of v) lv.message(1, encodeValue(el));
    w.message(6, lv);
  } else if (typeof v === "object") {
    const st = new Writer();
    for (const [k, val] of Object.entries(v)) {
      st.message(1, new Writer().bytes(1, Buffer.from(k, "utf8")).message(2, encodeValue(val)));
    }
    w.message(5, st);
  }
  return w.finish();
}

export function decodeValue(buf) {
  let out;
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.VARINT) { r.varint(); out = null; return true; }
    if (field === 2 && wire === WIRE.I64) { out = r.double(); return true; }
    if (field === 3 && wire === WIRE.LEN) { out = r.string(); return true; }
    if (field === 4 && wire === WIRE.VARINT) { out = r.varint() !== 0; return true; }
    if (field === 5 && wire === WIRE.LEN) {
      const st = r.bytes(); const o = {};
      forEachField(st, (f2, w2, r2) => {
        if (f2 === 1 && w2 === WIRE.LEN) {
          const e = r2.bytes(); let k = ""; let val;
          forEachField(e, (f3, w3, r3) => {
            if (f3 === 1 && w3 === WIRE.LEN) { k = r3.string(); return true; }
            if (f3 === 2 && w3 === WIRE.LEN) { val = decodeValue(r3.bytes()); return true; }
            return false;
          });
          o[k] = val; return true;
        }
        return false;
      });
      out = o; return true;
    }
    if (field === 6 && wire === WIRE.LEN) {
      const lv = r.bytes(); const arr = [];
      forEachField(lv, (f2, w2, r2) => {
        if (f2 === 1 && w2 === WIRE.LEN) { arr.push(decodeValue(r2.bytes())); return true; }
        return false;
      });
      out = arr; return true;
    }
    return false;
  });
  return out;
}

// ---------- blob helpers ----------
export function blobKeyHex(blobId) {
  return Buffer.from(blobId).toString("hex");
}
export function storeBlob(blobStore, data) {
  const id = Buffer.from(createHash("sha256").update(data).digest());
  blobStore.set(blobKeyHex(id), Buffer.from(data));
  return id;
}

// ---------- encode: run request ----------
export function encodeRunRequest({ modelId, systemPrompt, userText, conversationId = randomUUID(), blobStore }) {
  const sysBlobId = storeBlob(blobStore, enc.encode(JSON.stringify({ role: "system", content: systemPrompt })));

  const conversationState = new Writer().bytes(1, sysBlobId); // root_prompt_messages_json (repeated, one entry)

  const userMessage = new Writer().string(1, userText).string(2, randomUUID());
  const userMessageAction = new Writer().message(1, userMessage);
  const action = new Writer().message(1, userMessageAction); // user_message_action=1

  const modelDetails = new Writer().string(1, modelId).string(3, modelId).string(4, modelId);

  const runRequest = new Writer()
    .message(1, conversationState)
    .message(2, action)
    .message(3, modelDetails)
    .string(5, conversationId);

  return new Writer().message(1, runRequest).finish(); // AgentClientMessage.run_request
}

export function encodeHeartbeat() {
  return new Writer().message(7, new Writer()).finish(); // client_heartbeat (empty msg)
}

// ---------- encode: kv responses ----------
export function encodeGetBlobResult(id, blobDataOrNull) {
  const result = new Writer();
  if (blobDataOrNull) result.bytes(1, blobDataOrNull); // GetBlobResult.blob_data=1
  const kv = new Writer().uint(1, id).message(2, result); // get_blob_result=2
  return new Writer().message(3, kv).finish(); // kv_client_message=3
}
export function encodeSetBlobResult(id) {
  const kv = new Writer().uint(1, id).message(3, new Writer()); // set_blob_result=3 (empty)
  return new Writer().message(3, kv).finish();
}

// ---------- encode: exec client message ----------
function execEnvelope(id, execId, resultField, resultWriter) {
  const exec = new Writer().uint(1, id).message(resultField, resultWriter).string(15, execId);
  return new Writer().message(2, exec).finish(); // exec_client_message=2
}

// RequestContext (minimal, mirrors request_context.mjs which is known to start the stream).
function buildRequestContext(workspacePath, repoInfo) {
  const projectFolder = join(workspacePath, ".cursor-agent");
  const env = new Writer()
    .string(1, `${os.type()} ${os.release()}`)       // os_version
    .string(2, workspacePath)                          // workspace_paths (repeated, one)
    .string(3, process.env.SHELL || "/bin/zsh")        // shell
    .bool(5, false)                                    // sandbox_enabled
    .string(7, join(projectFolder, "terminals"))
    .string(8, join(projectFolder, "shared-notes"))
    .string(9, join(projectFolder, "conversation-notes"))
    .string(10, Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC")
    .string(11, projectFolder)
    .string(12, join(projectFolder, "transcripts"));

  const projectLayout = new Writer()
    .string(1, workspacePath)   // abs_path
    .bool(4, true)              // children_were_processed
    .uint(6, 0);               // num_files

  const mcpInstr = new Writer()
    .string(1, "explore-harness")
    .string(2, [
      `The current workspace root is ${workspacePath}. Trace and cite real files.`,
      "A fast semantic codebase_search tool is available and indexed for this workspace.",
      "Prefer codebase_search to locate relevant code by meaning; use read/grep/ls to confirm and cite exact lines.",
    ].join("\n"));

  const ctx = new Writer()
    .message(4, env)            // env=4
    .message(7, codebaseSearchToolDef()); // tools=7 (repeated McpToolDefinition)
  if (process.env.UNITRACE_BATCH_READ !== "0") ctx.message(7, batchReadToolDef()); // batch multi-file read (opt-out: UNITRACE_BATCH_READ=0)
  ctx
    .message(13, projectLayout) // project_layouts=13 (repeated, one)
    .message(14, mcpInstr);     // mcp_instructions=14 (repeated, one)

  if (repoInfo) ctx.message(6, repoInfo); // repository_info=6 (repeated)
  return ctx;
}

export const CODEBASE_SEARCH_TOOL = "codebase_search";
// McpToolDefinition: name=1, description=2, input_schema=3(bytes Value), provider_identifier=4, tool_name=5
// input_schema is a protobuf google.protobuf.Value of the JSON schema (proxy.ts:693), NOT raw JSON.
function codebaseSearchToolDef() {
  const schema = {
    type: "object",
    properties: {
      query: { type: "string", description: "Natural-language description of the code/behavior to find" },
      target_directories: { type: "array", items: { type: "string" }, description: "Optional workspace-relative dirs to scope the search" },
    },
    required: ["query"],
  };
  return new Writer()
    .string(1, CODEBASE_SEARCH_TOOL)
    .string(2, "Semantic search over the workspace. Returns the most relevant code with startLine:endLine:path citations. Use this first to locate where behavior lives.")
    .bytes(3, encodeValue(schema))
    .string(4, "explore-harness")
    .string(5, CODEBASE_SEARCH_TOOL);
}

export const BATCH_READ_TOOL = "batch_read";
// One call that reads MANY files, so the model collapses N serial read turns into
// one tool call (the harness can't make composer emit native parallel tool calls;
// this gives it an explicit batch primitive instead -- codex code-mode style).
function batchReadToolDef() {
  const schema = {
    type: "object",
    properties: {
      paths: { type: "array", items: { type: "string" }, description: "Workspace-relative file paths to read together in one call" },
    },
    required: ["paths"],
  };
  return new Writer()
    .string(1, BATCH_READ_TOOL)
    .string(2, "Read MULTIPLE files in ONE call. Pass every path you need as `paths`; returns each file's full contents under a `===== path =====` header. ALWAYS prefer this over calling `read` repeatedly -- it collapses many reads into a single step and is far faster.")
    .bytes(3, encodeValue(schema))
    .string(4, "explore-harness")
    .string(5, BATCH_READ_TOOL);
}

// McpArgs: name=1, args=2 map<string,bytes>, tool_call_id=3, tool_name=5
export function decodeMcpArgs(buf) {
  const out = { name: "", toolName: "", toolCallId: "", args: {} };
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { out.name = r.string(); return true; }
    if (field === 3 && wire === WIRE.LEN) { out.toolCallId = r.string(); return true; }
    if (field === 5 && wire === WIRE.LEN) { out.toolName = r.string(); return true; }
    if (field === 2 && wire === WIRE.LEN) { // map entry: key=1 string, value=2 bytes
      const entry = r.bytes();
      let k = "", v = null;
      forEachField(entry, (f2, w2, r2) => {
        if (f2 === 1 && w2 === WIRE.LEN) { k = r2.string(); return true; }
        if (f2 === 2 && w2 === WIRE.LEN) { v = Buffer.from(r2.bytes()); return true; }
        return false;
      });
      if (k) out.args[k] = v;
      return true;
    }
    return false;
  });
  return out;
}

// McpResult.success (field 1) -> McpSuccess{ content=[McpToolResultContentItem{text=McpTextContent}], is_error }
export function encodeMcpTextSuccess(id, execId, text, isError = false) {
  const textContent = new Writer().string(1, text);            // McpTextContent.text=1
  const item = new Writer().message(1, textContent);           // McpToolResultContentItem.text=1
  const success = new Writer().message(1, item).bool(2, isError); // McpSuccess.content=1, is_error=2
  const result = new Writer().message(1, success);             // McpResult.success=1
  return execEnvelope(id, execId, 11, result);                 // mcp_result=11
}

export function encodeRequestContextResult(id, execId, workspacePath, repoInfo) {
  const success = new Writer().message(1, buildRequestContext(workspacePath, repoInfo)); // RequestContextSuccess.request_context=1
  const result = new Writer().message(1, success); // RequestContextResult.success=1
  return execEnvelope(id, execId, 10, result);
}

export function encodeReadSuccess(id, execId, { path, content, totalLines, fileSize, truncated }) {
  const success = new Writer()
    .string(1, path)
    .string(2, content)        // content (oneof output=2)
    .uint(3, totalLines)       // total_lines
    .uint(4, fileSize)         // file_size
    .bool(6, !!truncated);     // truncated
  const result = new Writer().message(1, success); // ReadResult.success=1
  return execEnvelope(id, execId, 7, result);
}
export function encodeReadRejected(id, execId, path, reason) {
  const rej = new Writer().string(1, path).string(2, reason);
  const result = new Writer().message(3, rej); // ReadResult.rejected=3
  return execEnvelope(id, execId, 7, result);
}

// grep success in "content" mode: workspace_results map<path, GrepUnionResult{content}>
export function encodeGrepContentSuccess(id, execId, { pattern, path, fileMatches, clientTruncated, ripgrepTruncated }) {
  const contentResult = new Writer();
  for (const fm of fileMatches) {
    const fileMatch = new Writer().string(1, fm.file);
    for (const m of fm.matches) {
      const cm = new Writer().uint(1, m.lineNumber).string(2, m.content);
      fileMatch.message(2, cm); // GrepFileMatch.matches=2 (repeated)
    }
    contentResult.message(1, fileMatch); // GrepContentResult.matches=1 (repeated)
  }
  contentResult.bool(4, !!clientTruncated).bool(5, !!ripgrepTruncated);
  const union = new Writer().message(3, contentResult); // GrepUnionResult.content=3
  const entry = new Writer().string(1, path).message(2, union); // map entry key=1 value=2
  const success = new Writer()
    .string(1, pattern)
    .string(2, path)
    .string(3, "content")     // output_mode
    .message(4, entry);       // workspace_results=4 (repeated map entry)
  const result = new Writer().message(1, success); // GrepResult.success=1
  return execEnvelope(id, execId, 5, result);
}
export function encodeGrepError(id, execId, message) {
  const err = new Writer().string(1, message);
  const result = new Writer().message(2, err); // GrepResult.error=2
  return execEnvelope(id, execId, 5, result);
}

// ls success: LsResult.success -> LsSuccess.directory_tree_root -> LsDirectoryTreeNode
export function encodeLsSuccess(id, execId, { absPath, dirs, files }) {
  const node = new Writer().string(1, absPath);
  for (const d of dirs) {
    node.message(2, new Writer().string(1, d).bool(4, false).uint(6, 0)); // children_dirs (node)
  }
  for (const f of files) {
    node.message(3, new Writer().string(1, f)); // children_files (File.name=1)
  }
  node.bool(4, true).uint(6, files.length); // children_were_processed, num_files
  const success = new Writer().message(1, node); // LsSuccess.directory_tree_root=1
  const result = new Writer().message(1, success); // LsResult.success=1
  return execEnvelope(id, execId, 8, result);
}
export function encodeLsRejected(id, execId, path, reason) {
  const rej = new Writer().string(1, path).string(2, reason);
  const result = new Writer().message(3, rej); // LsResult.rejected=3
  return execEnvelope(id, execId, 8, result);
}

// ShellResult (shell_result = field 2). ShellSuccess: command1 wd2 exit3 stdout5 stderr6 time7.
export function encodeShellResultSuccess(id, execId, r) {
  const succ = new Writer()
    .string(1, r.command).string(2, r.workingDirectory)
    .uint(3, r.exitCode).string(5, r.stdout).string(6, r.stderr).uint(7, r.executionTime || 0);
  const result = new Writer().message(1, succ); // ShellResult.success=1
  return execEnvelope(id, execId, 2, result);
}
export function encodeShellResultRejected(id, execId, command, wd, reason) {
  const rej = new Writer().string(1, command).string(2, wd).string(3, reason).bool(4, true); // is_readonly=true
  const result = new Writer().message(4, rej); // ShellResult.rejected=4
  return execEnvelope(id, execId, 2, result);
}

// ShellStream (shell_stream = field 14); one event per frame (oneof event).
function shellStreamFrame(id, execId, eventField, eventWriter) {
  const stream = new Writer().message(eventField, eventWriter);
  return execEnvelope(id, execId, 14, stream);
}
export const encodeShellStreamStart = (id, execId) => shellStreamFrame(id, execId, 4, new Writer());
export const encodeShellStreamStdout = (id, execId, data) => shellStreamFrame(id, execId, 1, new Writer().string(1, data));
export const encodeShellStreamStderr = (id, execId, data) => shellStreamFrame(id, execId, 2, new Writer().string(1, data));
export const encodeShellStreamExit = (id, execId, code, cwd) => shellStreamFrame(id, execId, 3, new Writer().uint(1, code).string(2, cwd));
export const encodeShellStreamRejected = (id, execId, command, wd, reason) =>
  shellStreamFrame(id, execId, 5, new Writer().string(1, command).string(2, wd).string(3, reason).bool(4, true));

// BackgroundShellSpawnResult (field 16): rejected=3.
export function encodeBackgroundShellRejected(id, execId, command, wd, reason) {
  const rej = new Writer().string(1, command).string(2, wd).string(3, reason).bool(4, true);
  const result = new Writer().message(3, rej); // BackgroundShellSpawnResult.rejected=3
  return execEnvelope(id, execId, 16, result);
}

export function decodeShellArgs(buf) {
  const a = { command: "", workingDirectory: "", timeout: 0, simpleCommands: [], hasOutputRedirect: false };
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { a.command = r.string(); return true; }
    if (field === 2 && wire === WIRE.LEN) { a.workingDirectory = r.string(); return true; }
    if (field === 3 && wire === WIRE.VARINT) { a.timeout = r.varint(); return true; }
    if (field === 5 && wire === WIRE.LEN) { a.simpleCommands.push(r.string()); return true; }
    if (field === 7 && wire === WIRE.VARINT) { a.hasOutputRedirect = r.varint() !== 0; return true; }
    return false;
  });
  return a;
}
export function encodeDiagnosticsResult(id, execId) {
  return execEnvelope(id, execId, 9, new Writer()); // diagnostics_result=9 (empty)
}
export function encodeMcpEmptyResult(id, execId) {
  return execEnvelope(id, execId, 11, new Writer()); // mcp_result=11 (empty)
}
export function encodeRawRejectResult(id, execId, field) {
  // generic empty result for an unknown/rejected exec field (best-effort keepalive)
  return execEnvelope(id, execId, field, new Writer());
}

// ---------- decode: server message ----------
export function decodeServerMessage(buf) {
  let out = { case: "unknown", value: null };
  forEachField(buf, (field, wire, r) => {
    if (wire !== WIRE.LEN) return false;
    const sub = r.bytes();
    if (field === 1) out = { case: "interactionUpdate", value: sub };
    else if (field === 2) out = { case: "execServerMessage", value: sub };
    else if (field === 3) out = { case: "conversationCheckpointUpdate", value: sub };
    else if (field === 4) out = { case: "kvServerMessage", value: sub };
    return true;
  });
  return out;
}

export function decodeInteractionUpdate(buf) {
  let out = { case: "other" };
  forEachField(buf, (field, wire, r) => {
    if (wire !== WIRE.LEN) return false;
    const sub = r.bytes();
    if (field === 1) out = { case: "text", text: decodeTextField(sub) };
    else if (field === 4) out = { case: "thinking", text: decodeTextField(sub) };
    else if (field === 8) out = { case: "token", tokens: decodeTokenField(sub) };
    return true;
  });
  return out;
}
function decodeTextField(buf) {
  let text = "";
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { text = r.string(); return true; }
    return false;
  });
  return text;
}
function decodeTokenField(buf) {
  let tokens = 0;
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.VARINT) { tokens = r.varint(); return true; }
    return false;
  });
  return tokens;
}

export function decodeExecServerMessage(buf) {
  const out = { id: 0, execId: "", case: "unknown", args: null };
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.VARINT) { out.id = r.varint(); return true; }
    if (field === 15 && wire === WIRE.LEN) { out.execId = r.string(); return true; }
    if (wire !== WIRE.LEN) return false;
    const sub = r.bytes();
    const map = {
      2: "shellArgs", 3: "writeArgs", 4: "deleteArgs", 5: "grepArgs", 7: "readArgs",
      8: "lsArgs", 9: "diagnosticsArgs", 10: "requestContextArgs", 11: "mcpArgs",
      14: "shellStreamArgs", 16: "backgroundShellSpawnArgs", 20: "fetchArgs",
      17: "listMcpResourcesExecArgs", 18: "readMcpResourceExecArgs", 21: "recordScreenArgs",
      22: "computerUseArgs", 23: "writeShellStdinArgs",
    };
    if (map[field]) { out.case = map[field]; out.args = sub; out.field = field; }
    return true;
  });
  return out;
}

export function decodeReadArgs(buf) {
  let path = "";
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { path = r.string(); return true; }
    return false;
  });
  return { path };
}
export function decodeLsArgs(buf) {
  let path = "";
  const ignore = [];
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { path = r.string(); return true; }
    if (field === 2 && wire === WIRE.LEN) { ignore.push(r.string()); return true; }
    return false;
  });
  return { path, ignore };
}
export function decodeGrepArgs(buf) {
  const a = { pattern: "", path: "", glob: "", outputMode: "", caseInsensitive: false, type: "", headLimit: 0 };
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { a.pattern = r.string(); return true; }
    if (field === 2 && wire === WIRE.LEN) { a.path = r.string(); return true; }
    if (field === 3 && wire === WIRE.LEN) { a.glob = r.string(); return true; }
    if (field === 4 && wire === WIRE.LEN) { a.outputMode = r.string(); return true; }
    if (field === 8 && wire === WIRE.VARINT) { a.caseInsensitive = r.varint() !== 0; return true; }
    if (field === 9 && wire === WIRE.LEN) { a.type = r.string(); return true; }
    if (field === 10 && wire === WIRE.VARINT) { a.headLimit = r.varint(); return true; }
    return false;
  });
  return a;
}

export function decodeKvServerMessage(buf) {
  const out = { id: 0, case: "unknown", blobId: null, blobData: null };
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.VARINT) { out.id = r.varint(); return true; }
    if (wire !== WIRE.LEN) return false;
    const sub = r.bytes();
    if (field === 2) { out.case = "getBlobArgs"; out.blobId = decodeBlobId(sub); }
    else if (field === 3) { out.case = "setBlobArgs"; const s = decodeSetBlobArgs(sub); out.blobId = s.blobId; out.blobData = s.blobData; }
    return true;
  });
  return out;
}
function decodeBlobId(buf) {
  let id = null;
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { id = Buffer.from(r.bytes()); return true; }
    return false;
  });
  return id;
}
function decodeSetBlobArgs(buf) {
  let blobId = null;
  let blobData = null;
  forEachField(buf, (field, wire, r) => {
    if (field === 1 && wire === WIRE.LEN) { blobId = Buffer.from(r.bytes()); return true; }
    if (field === 2 && wire === WIRE.LEN) { blobData = Buffer.from(r.bytes()); return true; }
    return false;
  });
  return { blobId, blobData };
}

export function decodeCheckpointUsedTokens(buf) {
  let used = 0;
  forEachField(buf, (field, wire, r) => {
    if (field === 5 && wire === WIRE.LEN) { // token_details
      const sub = r.bytes();
      forEachField(sub, (f2, w2, r2) => {
        if (f2 === 1 && w2 === WIRE.VARINT) { used = r2.varint(); return true; }
        return false;
      });
      return true;
    }
    return false;
  });
  return used;
}
