// T3 gate: validate the hand-rolled codec against @bufbuild/protobuf.
// Run from the cursor-api scripts dir (which has @bufbuild/protobuf + tsx + the proto):
//   cd ~/.claude/skills/cursor-api/scripts
//   node --import tsx <explore>/scripts/test/proto-oracle.test.mjs
// Strategy:
//   - byte-equality for messages whose field order is deterministic (no random ids)
//   - cross round-trips: mine->bufbuild-decode and bufbuild->mine-decode (wire interop)
// Absolute paths so this dev-only test resolves bufbuild + the generated proto
// from the cursor-api skill without polluting the zero-dep explore repo.
// bufbuild ESM entry from its package.json exports["."].import = ./dist/esm/index.js
import test from "node:test";
import os from "node:os";
import { join } from "node:path";

const CURSOR_API = "/Users/jaredboynton/.claude/skills/cursor-api/scripts";

let create, toBinary, fromBinary, toJson, fromJson, ValueSchema;
let AgentClientMessageSchema, AgentServerMessageSchema, ExecServerMessageSchema;
let KvServerMessageSchema, InteractionUpdateSchema, TextDeltaUpdateSchema;
let ReadArgsSchema, GetBlobArgsSchema, RequestContextSchema, RequestContextEnvSchema;
let LsDirectoryTreeNodeSchema, McpInstructionsSchema, RequestContextResultSchema;
let RequestContextSuccessSchema, ExecClientMessageSchema, McpArgsSchema, LsArgsSchema;

let depsAvailable = false;
try {
  ({ create, toBinary, fromBinary, toJson, fromJson } = await import(`${CURSOR_API}/node_modules/@bufbuild/protobuf/dist/esm/index.js`));
  ({ ValueSchema } = await import(`${CURSOR_API}/node_modules/@bufbuild/protobuf/dist/esm/wkt/index.js`));
  ({
    AgentClientMessageSchema,
    AgentServerMessageSchema,
    ExecServerMessageSchema,
    KvServerMessageSchema,
    InteractionUpdateSchema,
    TextDeltaUpdateSchema,
    ReadArgsSchema,
    GetBlobArgsSchema,
    RequestContextSchema,
    RequestContextEnvSchema,
    LsDirectoryTreeNodeSchema,
    McpInstructionsSchema,
    RequestContextResultSchema,
    RequestContextSuccessSchema,
    ExecClientMessageSchema,
    McpArgsSchema,
    LsArgsSchema,
  } = await import(`${CURSOR_API}/lib/proto/agent_pb.ts`));
  depsAvailable = true;
} catch (err) {
  test("proto-oracle (deps unavailable)", { skip: `cursor-api deps missing: ${err.message}` }, () => {});
}

if (depsAvailable) {
  const HARNESS = new URL("file:///Users/jaredboynton/.agents/skills/explore/scripts/lib/hcursor-proto.mjs");
  const H = await import(HARNESS.href);
  // The full harness (handleMessage tool loop). main() is guarded, so importing is side-effect-free.
  const HARNESS_MAIN = new URL("file:///Users/jaredboynton/.agents/skills/explore/scripts/cursor-harness.mjs");
  const HM = await import(HARNESS_MAIN.href);

  let pass = 0;
  let fail = 0;
  const eq = (name, a, b) => {
    if (a === b) { pass++; }
    else { fail++; console.error(`FAIL ${name}: ${JSON.stringify(a)} !== ${JSON.stringify(b)}`); }
  };
  const bytesEq = (name, a, b) => {
    const ba = Buffer.from(a), bb = Buffer.from(b);
    if (ba.equals(bb)) { pass++; console.error(`ok   ${name} (byte-identical, ${ba.length}B)`); }
    else { fail++; console.error(`FAIL ${name}: bytes differ\n  mine: ${ba.toString("hex")}\n  buf : ${bb.toString("hex")}`); }
  };

  // ---- Test A: my read-success -> bufbuild decode (exec result interop) ----
  {
    const myBytes = H.encodeReadSuccess(7, "exec-abc", {
      path: "src/foo.ts", content: "line1\nline2\n", totalLines: 2, fileSize: 12, truncated: false,
    });
    const msg = fromBinary(AgentClientMessageSchema, myBytes);
    eq("A.case", msg.message?.case, "execClientMessage");
    const exec = msg.message.value;
    eq("A.id", exec.id, 7);
    eq("A.execId", exec.execId, "exec-abc");
    eq("A.result.case", exec.message?.case, "readResult");
    const rr = exec.message.value;
    eq("A.read.case", rr.result?.case, "success");
    eq("A.read.path", rr.result.value.path, "src/foo.ts");
    eq("A.read.content", rr.result.value.output?.value ?? rr.result.value.content, "line1\nline2\n");
    eq("A.read.totalLines", rr.result.value.totalLines, 2);
  }

  // ---- Test B: bufbuild textDelta -> my decode ----
  {
    const sv = create(AgentServerMessageSchema, {
      message: { case: "interactionUpdate", value: create(InteractionUpdateSchema, {
        message: { case: "textDelta", value: create(TextDeltaUpdateSchema, { text: "hello world" }) },
      }) },
    });
    const bytes = toBinary(AgentServerMessageSchema, sv);
    const dec = H.decodeServerMessage(bytes);
    eq("B.case", dec.case, "interactionUpdate");
    const iu = H.decodeInteractionUpdate(dec.value);
    eq("B.text.case", iu.case, "text");
    eq("B.text", iu.text, "hello world");
  }

  // ---- Test C: bufbuild readArgs -> my decode ----
  {
    const sv = create(ExecServerMessageSchema, {
      id: 42, execId: "e9",
      message: { case: "readArgs", value: create(ReadArgsSchema, { path: "a/b/c.rs", toolCallId: "tc1" }) },
    });
    const wrapped = create(AgentServerMessageSchema, { message: { case: "execServerMessage", value: sv } });
    const bytes = toBinary(AgentServerMessageSchema, wrapped);
    const dec = H.decodeServerMessage(bytes);
    eq("C.case", dec.case, "execServerMessage");
    const ex = H.decodeExecServerMessage(dec.value);
    eq("C.id", ex.id, 42);
    eq("C.execId", ex.execId, "e9");
    eq("C.argcase", ex.case, "readArgs");
    eq("C.path", H.decodeReadArgs(ex.args).path, "a/b/c.rs");
  }

  // ---- Test D: bufbuild getBlobArgs -> my decode ----
  {
    const blobId = new Uint8Array([1, 2, 3, 4, 250, 200]);
    const kv = create(KvServerMessageSchema, { id: 5, message: { case: "getBlobArgs", value: create(GetBlobArgsSchema, { blobId }) } });
    const wrapped = create(AgentServerMessageSchema, { message: { case: "kvServerMessage", value: kv } });
    const bytes = toBinary(AgentServerMessageSchema, wrapped);
    const dec = H.decodeServerMessage(bytes);
    eq("D.case", dec.case, "kvServerMessage");
    const km = H.decodeKvServerMessage(dec.value);
    eq("D.kvcase", km.case, "getBlobArgs");
    eq("D.id", km.id, 5);
    eq("D.blobId", Buffer.from(km.blobId).toString("hex"), Buffer.from(blobId).toString("hex"));
  }

  // ---- Test E1: bufbuild mcpArgs (codebase_search) -> my decode ----
  {
    const sv = create(ExecServerMessageSchema, {
      id: 9, execId: "mcp1",
      message: { case: "mcpArgs", value: create(McpArgsSchema, {
        name: "codebase_search", toolName: "codebase_search", toolCallId: "tc-9",
        args: { query: Buffer.from('"where is auth handled"', "utf8"), target_directories: Buffer.from('["src"]', "utf8") },
      }) },
    });
    const wrapped = create(AgentServerMessageSchema, { message: { case: "execServerMessage", value: sv } });
    const dec = H.decodeServerMessage(toBinary(AgentServerMessageSchema, wrapped));
    const ex = H.decodeExecServerMessage(dec.value);
    eq("E1.argcase", ex.case, "mcpArgs");
    const m = H.decodeMcpArgs(ex.args);
    eq("E1.name", m.name, "codebase_search");
    eq("E1.toolName", m.toolName, "codebase_search");
    eq("E1.query", Buffer.from(m.args.query).toString("utf8"), '"where is auth handled"');
    eq("E1.dirs", Buffer.from(m.args.target_directories).toString("utf8"), '["src"]');
  }

  // ---- Test E2: my mcp text success -> bufbuild decode ----
  {
    const myBytes = H.encodeMcpTextSuccess(9, "mcp1", "result body here", false);
    const msg = fromBinary(AgentClientMessageSchema, myBytes);
    eq("E2.case", msg.message?.case, "execClientMessage");
    const rr = msg.message.value.message;
    eq("E2.result", rr?.case, "mcpResult");
    eq("E2.success", rr.value.result?.case, "success");
    eq("E2.text", rr.value.result.value.content[0].content.value.text, "result body here");
  }

  // ---- Test E3: my requestContextResult registers the codebase_search tool ----
  {
    const myBytes = H.encodeRequestContextResult(3, "ctx1", "/tmp/ws", null);
    const msg = fromBinary(AgentClientMessageSchema, myBytes);
    const ctx = msg.message.value.message.value.result.value.requestContext;
    eq("E3.toolName", ctx.tools[0]?.name, "codebase_search");
    eq("E3.providerId", ctx.tools[0]?.providerIdentifier, "explore-harness");
    // input_schema is a protobuf google.protobuf.Value (proxy.ts:693), decode via bufbuild ValueSchema
    const schema = toJson(ValueSchema, fromBinary(ValueSchema, ctx.tools[0].inputSchema));
    eq("E3.schemaHasQuery", !!schema.properties?.query, true);
    eq("E3.schemaRequired", schema.required?.[0], "query");
    eq("E3.envWorkspace", ctx.env.workspacePaths[0], "/tmp/ws");
  }

  // ---- Test F1: my encodeValue -> bufbuild ValueSchema decode (schema object round-trip) ----
  {
    const obj = {
      type: "object",
      properties: { query: { type: "string", description: "d" }, n: { type: "array", items: { type: "string" } } },
      required: ["query"],
    };
    const mine = H.encodeValue(obj);
    const back = toJson(ValueSchema, fromBinary(ValueSchema, mine));
    eq("F1.type", back.type, "object");
    eq("F1.propQuery", back.properties?.query?.type, "string");
    eq("F1.nestedItems", back.properties?.n?.items?.type, "string");
    eq("F1.required", back.required?.[0], "query");
    // byte-identical to bufbuild's own encoding of the same JSON
    const oracle = toBinary(ValueSchema, fromJson(ValueSchema, obj));
    bytesEq("F1.bytes", mine, oracle);
  }

  // ---- Test F2: bufbuild Value -> my decodeValue (string/number/bool/list) ----
  {
    eq("F2.string", H.decodeValue(toBinary(ValueSchema, fromJson(ValueSchema, "where is auth"))), "where is auth");
    eq("F2.number", H.decodeValue(toBinary(ValueSchema, fromJson(ValueSchema, 42))), 42);
    eq("F2.bool", H.decodeValue(toBinary(ValueSchema, fromJson(ValueSchema, true))), true);
    const list = H.decodeValue(toBinary(ValueSchema, fromJson(ValueSchema, ["src", "lib"])));
    eq("F2.list", Array.isArray(list) && list.join(","), "src,lib");
    const obj = H.decodeValue(toBinary(ValueSchema, fromJson(ValueSchema, { a: "b" })));
    eq("F2.struct", obj?.a, "b");
  }

  // ---- Test G: lsArgs -> harness handleMessage -> lsResult success (T7 round-trip) ----
  {
    const { mkdtempSync, writeFileSync, mkdirSync } = await import("node:fs");
    const tmp = mkdtempSync(join(os.tmpdir(), "harness-ls-"));
    writeFileSync(join(tmp, "alpha.txt"), "a");
    writeFileSync(join(tmp, "beta.txt"), "b");
    mkdirSync(join(tmp, "subdir"));
    const sv = create(ExecServerMessageSchema, {
      id: 77, execId: "ls-1",
      message: { case: "lsArgs", value: create(LsArgsSchema, { path: tmp, toolCallId: "tc-ls" }) },
    });
    const wrapped = create(AgentServerMessageSchema, { message: { case: "execServerMessage", value: sv } });
    const bytes = toBinary(AgentServerMessageSchema, wrapped);
    const ctx = { workspace: tmp, blobStore: new Map(), toolLog: [] };
    const out = HM.handleMessage(Buffer.from(bytes), ctx);
    eq("G.replies", out.replies?.length, 1);
    const reply = fromBinary(AgentClientMessageSchema, out.replies[0]);
    eq("G.case", reply.message?.case, "execClientMessage");
    eq("G.execId", reply.message.value.execId, "ls-1");
    const lsr = reply.message.value.message;
    eq("G.result", lsr?.case, "lsResult");
    eq("G.success", lsr.value.result?.case, "success");
    const node = lsr.value.result.value.directoryTreeRoot;
    const files = (node?.childrenFiles || []).map((f) => f.name).sort();
    eq("G.files", files.join(","), "alpha.txt,beta.txt");
    eq("G.toolAck", String(ctx.toolLog[0] || "").startsWith("ls "), true);
  }

  console.error(`\n${pass} passed, ${fail} failed`);
  process.exit(fail === 0 ? 0 : 1);
}
