// T12 gate: prove the zero-dep repo42 codec is byte-identical to the reference
// cursor-oauth-opencode/src/cursor-index-wire.ts. Golden vectors were generated
// once from the reference (via tsx) with the fixed inputs below; this test runs
// with plain `node` (no .ts import) and asserts our output matches byte-for-byte.
import {
  encryptCursorPath, decryptCursorPath,
  encodeSearchRepositoryRequest, encodeFastRepoInitHandshakeV2Request,
  encodeFastUpdateFileV2Request, encodeEnsureIndexCreatedRequest, encodeFastRepoSyncCompleteRequest,
} from "../lib/hcursor-index.mjs";

const key = Buffer.alloc(32, 7).toString("base64url");
const metadata = { pathEncryptionKey: key, orthogonalTransformSeed: 123456, repoName: "test-repo", repoOwner: "owner-1", workspaceUri: "fixed-workspace-uri" };
const context = { workspacePath: "/tmp/ws", relativeWorkspacePath: ".", repoName: "test-repo", repoOwner: "owner-1", remotes: [{ name: "origin", url: "https://example.com/x/y.git" }], isTracked: false, isLocal: true };
const f1 = { relativePath: "./a.ts", contents: "hello world", hash: "h1", ancestorPaths: ["."] };
const f2 = { relativePath: "./src/b.ts", contents: "second file", hash: "h2", ancestorPaths: ["./src", "."] };

// Golden vectors from the reference (cursor-index-wire.ts), fixed inputs above.
const GOLDEN = {
  encPath: "./FrQYg42GkEjgYg/MsQmL1hBezK7tw.z7zvu4oAuYPrRw",
  search: "0a1266696e6420746865206175746820666c6f77125e0a012e121b68747470733a2f2f6578616d706c652e636f6d2f782f792e6769741a066f726967696e2209746573742d7265706f2a076f776e65722d313000380149000000000024fe405a1366697865642d776f726b73706163652d757269180a2801",
  handshake: "0a600a012e121b68747470733a2f2f6578616d706c652e636f6d2f782f792e6769741a066f726967696e2209746573742d7265706f2a076f776e65722d3130003801400349000000000024fe405a1366697865642d776f726b73706163652d757269120c726f6f74686173682d61626318012a403965323964663837656532613134396161373439653662313635353336666430663631613539333836353334393561663839376661623533643565396161306430013800",
  updateSingle: "0a0909000000000024fe40120463622d31223c0a2e0a1f2e2f794361566b6d6751533376464d512e7a377a7675346f41755950725277120b68656c6c6f20776f726c64120268311a062e2f612e74732a030a012e3001",
  updateBatch: "0a0909000000000024fe40120463622d3130043a45123c0a2e0a1f2e2f794361566b6d6751533376464d512e7a377a7675346f41755950725277120b68656c6c6f20776f726c64120268311a062e2f612e74731a030a012e20013a6c124f0a3d0a2e2e2f46725159673432476b456a6759672f667a31305a356f554643767a48412e7a377a7675346f41755950725277120b7365636f6e642066696c65120268321a0a2e2f7372632f622e74731a120a102e2f46725159673432476b456a6759671a030a012e2001",
  ensure: "0a600a012e121b68747470733a2f2f6578616d706c652e636f6d2f782f792e6769741a066f726967696e2209746573742d7265706f2a076f776e65722d3130003801400049000000000024fe405a1366697865642d776f726b73706163652d757269",
  sync: "0a5c0a0463622d31100118012a403965323964663837656532613134396161373439653662313635353336666430663631613539333836353334393561663839376661623533643565396161306430013800400048035000580060006800",
};

let pass = 0, fail = 0;
const hx = (u8) => Buffer.from(u8).toString("hex");
const eq = (name, got, want) => { if (got === want) { pass++; } else { fail++; console.error(`FAIL ${name}:\n  got : ${got}\n  want: ${want}`); } };

eq("encryptCursorPath", encryptCursorPath("./src/foo.ts", key), GOLDEN.encPath);
eq("decrypt-roundtrip", decryptCursorPath(encryptCursorPath("./src/foo.ts", key), key), "./src/foo.ts");
eq("search", hx(encodeSearchRepositoryRequest("find the auth flow", context, metadata, 10)), GOLDEN.search);
eq("handshake", hx(encodeFastRepoInitHandshakeV2Request(context, metadata, 3, "roothash-abc")), GOLDEN.handshake);
eq("updateSingle", hx(encodeFastUpdateFileV2Request("cb-1", metadata, [f1])), GOLDEN.updateSingle);
eq("updateBatch", hx(encodeFastUpdateFileV2Request("cb-1", metadata, [f1, f2])), GOLDEN.updateBatch);
eq("ensure", hx(encodeEnsureIndexCreatedRequest(context, metadata)), GOLDEN.ensure);
eq("sync", hx(encodeFastRepoSyncCompleteRequest([{ codebaseId: "cb-1", success: true, failedUploadCount: 0, totalUploadCount: 3 }], metadata)), GOLDEN.sync);

console.error(`${pass} passed, ${fail} failed`);
console.log(fail === 0 ? "OK: repo42 codec byte-identical to cursor-oauth-opencode reference" : "FAILED");
process.exit(fail === 0 ? 0 : 1);
