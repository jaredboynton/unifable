#!/usr/bin/env node
// gemini-trace.mjs — Gemini CLI headless trace driver for explore trace-gm.
//
// Spawns `gemini -p ... --approval-mode plan` in the workspace, parses json/text
// output, enforces timeout. Zero npm dependencies; Node 18+.

import { spawn, spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const DEFAULT_MODEL = "gemini-3.1-flash-lite";
const DEFAULT_TIMEOUT_SEC = 600;

export class GeminiTraceError extends Error {
  constructor(message, { stderrTail, timedOut, exitCode } = {}) {
    super(message);
    this.name = "GeminiTraceError";
    this.stderrTail = stderrTail;
    this.timedOut = timedOut;
    this.exitCode = exitCode;
  }
}

export function hasContent(text) {
  return /\S/.test(text || "");
}

export function extractGeminiOutput(rawStdout, outputFormat = "json") {
  const raw = (rawStdout || "").trim();
  if (!raw) return "";

  if (outputFormat === "text") {
    return raw;
  }

  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed.response === "string" && hasContent(parsed.response)) {
      return parsed.response.trim();
    }
    if (typeof parsed.text === "string" && hasContent(parsed.text)) {
      return parsed.text.trim();
    }
    if (typeof parsed.output === "string" && hasContent(parsed.output)) {
      return parsed.output.trim();
    }
  } catch {
    // Fall through: some CLI builds emit plain text even with -o json.
  }

  return raw;
}

function resolveGeminiBin(override) {
  if (override) return override;
  const which = spawnSync("sh", ["-c", "command -v gemini"], { encoding: "utf8" });
  const bin = (which.stdout || "").trim();
  if (which.status !== 0 || !bin) {
    throw new GeminiTraceError("gemini CLI not found on PATH (install Gemini CLI)");
  }
  return bin;
}

function killProcessGroup(pid, signal) {
  try {
    process.kill(-pid, signal);
  } catch {
    try {
      process.kill(pid, signal);
    } catch {
      /* ignore */
    }
  }
}

function runWithTimeout(cmd, args, { cwd, timeoutSec }) {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, {
      cwd,
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: process.env,
    });

    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr?.on("data", (chunk) => {
      stderr += chunk;
    });

    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      killProcessGroup(child.pid, "SIGTERM");
      setTimeout(() => killProcessGroup(child.pid, "SIGKILL"), 2000);
    }, timeoutSec * 1000);

    const onSignal = () => {
      killProcessGroup(child.pid, "SIGTERM");
    };
    process.on("SIGINT", onSignal);
    process.on("SIGTERM", onSignal);

    const finish = (result) => {
      clearTimeout(timer);
      process.off("SIGINT", onSignal);
      process.off("SIGTERM", onSignal);
      resolve(result);
    };

    child.on("error", (err) => {
      finish({ code: 127, stdout, stderr: `${stderr}${err.message}`, timedOut });
    });

    child.on("close", (code) => {
      finish({ code: code ?? 1, stdout, stderr, timedOut });
    });
  });
}

function argValue(name, fallback) {
  const i = process.argv.indexOf(name);
  return i === -1 ? fallback : process.argv[i + 1];
}

function usage() {
  process.stderr.write(
    "usage: gemini-trace.mjs --prompt-file <path> --workspace <path> --out <path> --raw <path> --err <path> [--model <model>]\n" +
      "env: UNITRACE_GM_BIN, UNITRACE_GM_MODEL, UNITRACE_GM_TIMEOUT, UNITRACE_GM_OUTPUT_FORMAT\n",
  );
  process.exit(2);
}

export async function runGeminiTrace({
  prompt,
  workspace,
  geminiBin,
  model,
  timeoutSec,
  outputFormat,
} = {}) {
  const bin = resolveGeminiBin(geminiBin || process.env.UNITRACE_GM_BIN);
  const modelSlug = model ?? process.env.UNITRACE_GM_MODEL ?? DEFAULT_MODEL;
  const format = outputFormat ?? process.env.UNITRACE_GM_OUTPUT_FORMAT ?? "json";
  const timeout = Number(timeoutSec ?? process.env.UNITRACE_GM_TIMEOUT ?? DEFAULT_TIMEOUT_SEC);

  const args = [
    "-p",
    prompt,
    "-m",
    modelSlug,
    "--approval-mode",
    "plan",
    "--skip-trust",
    "-o",
    format,
  ];

  const { stdout, stderr, timedOut, code } = await runWithTimeout(bin, args, {
    cwd: workspace,
    timeoutSec: timeout,
  });

  const answer = extractGeminiOutput(stdout, format);
  if (!hasContent(answer)) {
    throw new GeminiTraceError("gemini produced no output", {
      stderrTail: stderr.trim().split("\n").slice(-15).join("\n"),
      timedOut,
      exitCode: code,
    });
  }

  if (code !== 0) {
    throw new GeminiTraceError(`gemini exited with status ${code}`, {
      stderrTail: stderr.trim().split("\n").slice(-15).join("\n"),
      timedOut,
      exitCode: code,
    });
  }

  return { answer, rawStdout: stdout, stderr, model: modelSlug, outputFormat: format };
}

async function main() {
  const promptFile = argValue("--prompt-file");
  const workspace = argValue("--workspace");
  const outPath = argValue("--out");
  const rawPath = argValue("--raw");
  const errPath = argValue("--err");
  const model = argValue("--model", process.env.UNITRACE_GM_MODEL ?? DEFAULT_MODEL);

  if (!promptFile || !workspace || !outPath || !rawPath || !errPath) {
    usage();
  }

  const prompt = readFileSync(promptFile, "utf8");
  try {
    const { answer, rawStdout, stderr } = await runGeminiTrace({
      prompt,
      workspace,
      model,
    });
    writeFileSync(outPath, answer.endsWith("\n") ? answer : `${answer}\n`);
    writeFileSync(rawPath, rawStdout);
    if (stderr.trim()) {
      writeFileSync(errPath, stderr);
    } else {
      writeFileSync(errPath, "");
    }
  } catch (e) {
    const msg = e?.message || String(e);
    const tail = e?.stderrTail ? `\n${e.stderrTail}` : "";
    writeFileSync(errPath, `${msg}${tail}\n`);
    process.exit(1);
  }
}

const isMain = process.argv[1] === fileURLToPath(import.meta.url);
if (isMain) {
  main().catch((e) => {
    process.stderr.write(`gemini-trace fatal: ${e?.message || e}\n`);
    process.exit(1);
  });
}
