#!/usr/bin/env node
import process from "node:process";

const DENIED_TOOL_PATTERNS = [
  /terminal/i,
  /shell/i,
  /bash/i,
  /zsh/i,
  /powershell/i,
  /cmd(?:\.exe)?/i,
  /write_shell_stdin/i,
  /(?:^|[^a-z])write(?:[^a-z]|$)/i,
  /edit/i,
  /multi[_-]?edit/i,
  /apply_agent_diff/i,
  /delete/i,
  /update_project/i,
  /mcp_auth/i,
];

const MCP_TOOL_PATTERNS = [
  /\bmcp\b/i,
  /\bcall_mcp_tool\b/i,
];

function readStdin() {
  return new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      input += chunk;
    });
    process.stdin.on("end", () => resolve(input));
    process.stdin.on("error", reject);
  });
}

function lowerJson(value) {
  try {
    return JSON.stringify(value || {}).toLowerCase();
  } catch {
    return "";
  }
}

function toolIdentity(payload) {
  const toolName = String(payload.tool_name || payload.toolName || payload.name || "");
  const input = payload.tool_input || payload.toolInput || payload.input || {};
  return {
    toolName,
    input,
    combined: `${toolName}\n${lowerJson(input)}`,
  };
}

function isAllowedSearchMcp({ toolName, input, combined }) {
  const text = combined.toLowerCase();
  if (/^list mcp (resources|tools|prompts)$/i.test(String(toolName || "")) && Object.keys(input || {}).length === 0) {
    return true;
  }
  const explicitTool = String(
    input.name ||
    input.tool ||
    input.tool_name ||
    input.toolName ||
    input.mcp_tool_name ||
    input.mcpToolName ||
    ""
  ).toLowerCase();
  const server = String(
    input.server ||
    input.server_name ||
    input.serverName ||
    input.mcp_server ||
    input.mcpServer ||
    ""
  ).toLowerCase();
  const namedSearch = explicitTool === "search_code" || /\bsearch_code\b/.test(text);
  const namedServer = !server || server === "explore-search" || /\bexplore-search\b/.test(text);
  return namedSearch && namedServer && !/\bmcp_auth\b/i.test(toolName);
}

function decision(payload) {
  const identity = toolIdentity(payload);
  if (identity.input && typeof identity.input.command === "string") {
    return {
      permission: "deny",
      user_message: "Trace harness blocked command execution.",
      agent_message: "This trace session is read-only: do not run shell commands or scripts. Use native read/search/codebase tools and search_code only.",
    };
  }
  const isMcp = MCP_TOOL_PATTERNS.some((pattern) => pattern.test(identity.combined));
  if (isMcp) {
    if (/\bmcp_auth\b/i.test(identity.combined)) {
      return {
        permission: "deny",
        user_message: "Trace harness blocked MCP authentication.",
        agent_message: "MCP authentication is disabled in trace sessions. Use only explore-search/search_code if an MCP lookup is needed.",
      };
    }
    if (isAllowedSearchMcp(identity)) {
      return { permission: "allow" };
    }
    return {
      permission: "deny",
      user_message: "Trace harness blocked this MCP call. Only explore-search/search_code is allowed.",
      agent_message: "Use native read/search/codebase tools or the explore-search search_code MCP tool only.",
    };
  }

  const denied = DENIED_TOOL_PATTERNS.find((pattern) => pattern.test(identity.combined));
  if (denied) {
    return {
      permission: "deny",
      user_message: "Trace harness blocked a non-read-only tool call.",
      agent_message: "This trace session is read-only: no terminal, shell, write, edit, delete, or diff tools. Use native read/search/codebase tools and search_code only.",
    };
  }

  return { permission: "allow" };
}

try {
  const input = await readStdin();
  const payload = input.trim() ? JSON.parse(input) : {};
  process.stdout.write(`${JSON.stringify(decision(payload))}\n`);
} catch (error) {
  process.stdout.write(JSON.stringify({
    permission: "deny",
    user_message: "Trace harness hook failed closed.",
    agent_message: `Trace preToolUse hook could not parse/evaluate the tool call: ${error.message}`,
  }) + "\n");
}
