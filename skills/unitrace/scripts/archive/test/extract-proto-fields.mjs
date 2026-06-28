#!/usr/bin/env node
// Dev-only: extract protobuf field numbers from a bufbuild-generated *_pb.ts file.
// Parses `@generated from message agent.v1.X` headers and the subsequent
// `@generated from field: <type> <name> = <N>;` / `@generated from oneof ...`
// comments, emitting a compact per-message field table. Not shipped with the harness.
import { readFileSync } from "node:fs";

const src = process.argv[2]
  || "/tmp/cursor-oauth-opencode/src/proto/agent_pb.ts";
const text = readFileSync(src, "utf8");
const lines = text.split(/\r?\n/);

const messages = new Map(); // name -> { fields: [{name,num,type}], oneofs: [] }
let cur = null;
let curOneof = null;

const msgRe = /@generated from message (agent\.v1\.[A-Za-z0-9_]+)/;
const oneofRe = /@generated from oneof (agent\.v1\.[A-Za-z0-9_]+)\.([A-Za-z0-9_]+)/;
const fieldRe = /@generated from field: (.+?) ([A-Za-z0-9_]+) = (\d+);/;

for (const line of lines) {
  const m = msgRe.exec(line);
  if (m) {
    cur = m[1].replace(/^agent\.v1\./, "");
    curOneof = null;
    if (!messages.has(cur)) messages.set(cur, { fields: [], oneofs: new Map() });
    continue;
  }
  const o = oneofRe.exec(line);
  if (o) {
    curOneof = o[2];
    continue;
  }
  const f = fieldRe.exec(line);
  if (f && cur) {
    const type = f[1].replace(/agent\.v1\./g, "");
    const name = f[2];
    const num = Number(f[3]);
    messages.get(cur).fields.push({ name, num, type, oneof: curOneof });
  }
}

const names = [...messages.keys()].sort();
let out = `# proto field tables from ${src}\n# ${names.length} messages\n\n`;
for (const name of names) {
  const { fields } = messages.get(name);
  out += `${name}\n`;
  for (const fld of fields) {
    const oneofTag = fld.oneof ? ` [oneof ${fld.oneof}]` : "";
    out += `  ${String(fld.num).padStart(3)} ${fld.name} : ${fld.type}${oneofTag}\n`;
  }
  out += "\n";
}
process.stdout.write(out);
