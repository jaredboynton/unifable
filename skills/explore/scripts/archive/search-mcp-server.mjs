#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(process.env.EXPLORE_SEARCH_ROOT || process.cwd());
const BUNDLED_SEARCH_SCRIPT = path.resolve(path.join(path.dirname(fileURLToPath(import.meta.url)), "search.sh"));
const SEARCH_SCRIPT = path.resolve(process.env.EXPLORE_SEARCH_SCRIPT || BUNDLED_SEARCH_SCRIPT);
const MAX_OUTPUT_BYTES = Number(process.env.EXPLORE_SEARCH_MAX_OUTPUT_BYTES || 200 * 1024);
const TIMEOUT_MS = Number(process.env.EXPLORE_SEARCH_TIMEOUT_MS || 30_000);

let inputBuffer = Buffer.alloc(0);
const activeChildren = new Set();

function stopActiveChildren(signal = "SIGTERM") {
  for (const child of activeChildren) {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill(signal);
    }
  }
}

process.on("SIGINT", () => {
  stopActiveChildren();
  process.exit(130);
});
process.on("SIGTERM", () => {
  stopActiveChildren();
  process.exit(143);
});
process.on("exit", () => {
  stopActiveChildren();
});

function writeMessage(message) {
  const body = Buffer.from(JSON.stringify(message), "utf8");
  process.stdout.write(`Content-Length: ${body.length}\r\n\r\n`);
  process.stdout.write(body);
}

function respond(id, result) {
  if (id === undefined || id === null) {
    return;
  }
  writeMessage({ jsonrpc: "2.0", id, result });
}

function reject(id, code, message) {
  if (id === undefined || id === null) {
    return;
  }
  writeMessage({ jsonrpc: "2.0", id, error: { code, message } });
}

function parseMessages() {
  for (;;) {
    const headerEnd = inputBuffer.indexOf("\r\n\r\n");
    if (headerEnd === -1) {
      return;
    }
    const header = inputBuffer.slice(0, headerEnd).toString("ascii");
    const lengthMatch = /(?:^|\r\n)Content-Length:\s*(\d+)/i.exec(header);
    if (!lengthMatch) {
      inputBuffer = inputBuffer.slice(headerEnd + 4);
      continue;
    }
    const length = Number(lengthMatch[1]);
    const bodyStart = headerEnd + 4;
    const bodyEnd = bodyStart + length;
    if (inputBuffer.length < bodyEnd) {
      return;
    }
    const body = inputBuffer.slice(bodyStart, bodyEnd).toString("utf8");
    inputBuffer = inputBuffer.slice(bodyEnd);
    try {
      handleMessage(JSON.parse(body));
    } catch (error) {
      process.stderr.write(`explore-search mcp parse error: ${error.message}\n`);
    }
  }
}

function ensureSearchScript() {
  const stat = fs.statSync(SEARCH_SCRIPT);
  if (!stat.isFile()) {
    throw new Error(`search script is not a file: ${SEARCH_SCRIPT}`);
  }
  if (fs.realpathSync(SEARCH_SCRIPT) !== fs.realpathSync(BUNDLED_SEARCH_SCRIPT)) {
    throw new Error(`search script must be the bundled search.sh: ${BUNDLED_SEARCH_SCRIPT}`);
  }
  if (!fs.statSync(ROOT).isDirectory()) {
    throw new Error(`search root is not a directory: ${ROOT}`);
  }
}

function searchCode(args = {}) {
  const query = String(args.query || "").trim();
  if (!query) {
    throw new Error("query is required");
  }
  if (query.length > 1000) {
    throw new Error("query is too long");
  }
  ensureSearchScript();

  return new Promise((resolve, rejectPromise) => {
    const child = spawn(SEARCH_SCRIPT, ["--root", ROOT, query], {
      cwd: ROOT,
      env: process.env,
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
    });
    activeChildren.add(child);
    let output = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
    }, TIMEOUT_MS);
    const record = (chunk) => {
      output += chunk.toString("utf8");
      while (Buffer.byteLength(output, "utf8") > MAX_OUTPUT_BYTES) {
        output = output.slice(Math.max(1, Math.floor(output.length / 10)));
      }
    };
    child.stdout.on("data", record);
    child.stderr.on("data", record);
    child.on("error", (error) => {
      clearTimeout(timer);
      activeChildren.delete(child);
      rejectPromise(error);
    });
    child.on("exit", (code, signal) => {
      clearTimeout(timer);
      activeChildren.delete(child);
      if (timedOut) {
        rejectPromise(new Error(`search timed out after ${TIMEOUT_MS}ms`));
        return;
      }
      const status = signal ? `signal ${signal}` : `exit ${code}`;
      resolve(output.trimEnd() || `search finished with ${status} and no output`);
    });
  });
}

async function handleMessage(message) {
  const { id, method, params } = message;
  try {
    switch (method) {
      case "initialize":
        respond(id, {
          protocolVersion: params?.protocolVersion || "2024-11-05",
          capabilities: { tools: {} },
          serverInfo: { name: "explore-search", version: "0.1.0" },
        });
        return;
      case "notifications/initialized":
        return;
      case "ping":
        respond(id, {});
        return;
      case "tools/list":
        respond(id, {
          tools: [
            {
              name: "search_code",
              description: "Search the current repository with the bundled explore search.sh helper. Input is only a natural-language or keyword query; the repository root is fixed by the trace harness.",
              inputSchema: {
                type: "object",
                properties: {
                  query: {
                    type: "string",
                    description: "Tight search query derived from the trace question.",
                  },
                },
                required: ["query"],
                additionalProperties: false,
              },
            },
          ],
        });
        return;
      case "tools/call": {
        if (params?.name !== "search_code") {
          throw new Error(`unknown tool: ${params?.name || ""}`);
        }
        const text = await searchCode(params.arguments || {});
        respond(id, { content: [{ type: "text", text }] });
        return;
      }
      case "resources/list":
        respond(id, { resources: [] });
        return;
      case "prompts/list":
        respond(id, { prompts: [] });
        return;
      default:
        reject(id, -32601, `method not implemented: ${method}`);
    }
  } catch (error) {
    reject(id, -32000, error.message);
  }
}

process.stdin.on("data", (chunk) => {
  inputBuffer = Buffer.concat([inputBuffer, chunk]);
  parseMessages();
});
