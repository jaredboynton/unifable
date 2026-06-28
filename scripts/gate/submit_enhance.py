"""submit_enhance.py -- repo-grounded prompt enhancement for the UserPromptSubmit gate.

Host-agnostic policy layer (no Claude/Codex host imports; stdlib only). The
heavy lifting (retrieve + mini-nav + full synth) runs in a Node subprocess
(skills/unitrace/scripts/enhance-prompt.mjs) that reuses the unitrace skill's
in-repo machinery. This module decides WHETHER to enhance, runs it with a hard
timeout, hard-gates the output (no repo-specific commands; char caps), and
fail-opens to the static baseline on any error.

Design (bench-decided 2026-06-27, /tmp/enhance-bench; see docs/evals/prompt-enhance.md):
  - Tier: Standard = retrieveCandidates seed -> 4 mini nav -> 1 full
    gpt-realtime-2 synth (reasoning OMITTED, the proven trace-submit config).
    At omitted reasoning, nav's pruning to ~5-7 windows is what lets the full
    synth score q=9 on real repos; the lite (no-nav) tier collapsed to q=3 on
    large repos at omitted reasoning.
  - Fire heuristic (pre-grade, cheap, on the operative prompt alone):
    enhance_enabled() AND no path/file token AND >= 20 words AND not
    looks_operational(). Path-grounded or operational prompts skip the
    subprocess entirely.
  - Use heuristic (post-grade): only inject when evidence_profile == "code"
    and mode in {"normal","deep"}; otherwise discard the enhancer output.
    The grade judge runs concurrently with the enhancer (see hooks/gate_prompt.py),
    so wall-clock = max(grade, enhance), not their sum.
  - Hard gates on the enhanced text: zero repo-specific commands (reject ->
    static fallback), char cap 1200, cited ranges filtered by the entrypoint.
  - Fail-open: any error / timeout / missing node / missing script -> None ->
    the static classify_task baseline is used. A gate that hard-locks a session
    on its own bug is worse than no gate.

Env knobs:
  UNIFABLE_PROMPT_ENHANCE             "0" disables (default "1")
  UNIFABLE_PROMPT_ENHANCE_TIMEOUT_MS  subprocess wall-clock cap (default 6000)
  UNIFABLE_PROMPT_ENHANCE_NAV         mini navigator count (default 4; entrypoint)
  UNIFABLE_PROMPT_ENHANCE_MODEL       synth model (default gpt-realtime-2; entrypoint)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# --- config -----------------------------------------------------------------

DEFAULT_TIMEOUT_MS = 6000
ENHANCED_CHAR_CAP = 1200
MIN_WORDS = 20

_CODE_EXT = (
    "py|js|mjs|cjs|ts|tsx|jsx|go|rs|java|rb|sh|bash|zsh|c|cc|cxx|cpp|h|hpp|hpp|cs|php|kt|kts|"
    "swift|ex|exs|heex|eex|erb|sql|yaml|yml|toml|json|tcl|lua|pl|pm|scala|clj|cljc|edn|elm|"
    "fs|hs|ml|nim|dart|proto|graphql|gql|vue|svelte|astro|njk|hbs|twig"
)
# A grounded path/file token in the operative prompt -> not under-specified.
_PATH_TOKEN_RE = re.compile(
    r"(?:`[^`]*[/.][^`]*`)"  # backtick-quoted path-ish
    r"|(?:@\S*[/\.]\S*)"  # @src/foo, @lib/bar.mjs
    r""
    r"|(?:\b[\w.\-]+/[\w./\-]+)"  # slash path: lib/foo/bar.mjs
    r"|(?:\b[\w.\-]+\.(?:" + _CODE_EXT + r")\b)"  # filename with code extension
    r"|(?:\b(?:Dockerfile|Makefile|justfile|Rakefile|Vagrantfile|Gemfile"
    r"|Procfile|Jenkinsfile|Brewfile|Containerfile|Taskfile)\b)",  # extensionless build/container files
    re.IGNORECASE,
)

# Unambiguously operational (non-code) tokens -> skip the enhancer to avoid
# wasting a ~4s subprocess on a research/drafting/CRM ask. Conservative set;
# the post-grade profile check is the real safety net.
_OPERATIONAL_KEYWORDS = (
    "slack",
    "salesforce",
    "gong",
    "jira",
    "confluence",
    "looker",
    "redash",
    "transcript",
    "renew",
    "renewal",
    "churn",
    "war room",
    "war-room",
    "account owner",
    "stakeholder",
    "executive sponsor",
    "exec sponsor",
    "cse engagement",
    "1:1",
    "one-on-one",
    "one on one",
    "morning digest",
    "case study",
    "case-study",
    "bandwidth",
    "escalat",
)
_OPERATIONAL_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _OPERATIONAL_KEYWORDS) + r")",
    re.IGNORECASE,
)

# Repo-specific build/test/lint commands are FORBIDDEN in the enhanced prompt
# (the synth is instructed to name only the CATEGORY of verification; this is
# the deterministic backstop). If any appears, reject the enhanced text and
# fall back to the static baseline.
_REPO_CMD_RE = re.compile(
    r"\b(?:"
    r"npm|yarn|pnpm|cargo|pytest|py\.test|just|make|go|dotnet|gradle|mvn|rake|tox|nox"
    r")\s+(?:test|build|lint|check|run|fmt|vet|clippy|typecheck|tsc|jest|vitest)"
    r"\b"
    r"|\bpytest\b"
    r"|\bgo\s+test\b"
    r"|\bnpm\s+(?:test|run\s+test|run\s+build)\b"
    r"|\bcargo\s+(?:test|build|clippy)\b"
    r"|\bjust\s+(?:test|build|check)\b"
    r"|\bmake\s+(?:test|build|check)\b",
    re.IGNORECASE,
)


# --- env / enable -----------------------------------------------------------


def enhance_enabled() -> bool:
    """True unless UNIFABLE_PROMPT_ENHANCE is exactly '0' (default on)."""
    return os.environ.get("UNIFABLE_PROMPT_ENHANCE", "1").strip() != "0"


def _timeout_ms() -> int:
    try:
        v = int(os.environ.get("UNIFABLE_PROMPT_ENHANCE_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
        return v if v > 0 else DEFAULT_TIMEOUT_MS
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_MS


# --- heuristics -------------------------------------------------------------


def has_path_token(text: str) -> bool:
    return bool(_PATH_TOKEN_RE.search(text or ""))


def looks_operational(text: str) -> bool:
    return bool(_OPERATIONAL_RE.search(text or ""))


def is_under_specified(text: str) -> bool:
    """Pre-grade fire heuristic: no path/file token AND substantive (>= MIN_WORDS)."""
    s = text or ""
    if has_path_token(s):
        return False
    return len(s.split()) >= MIN_WORDS


def fire_enhance(operative: str) -> bool:
    """Cheap pre-grade decision to launch the enhancer subprocess."""
    return enhance_enabled() and is_under_specified(operative) and not looks_operational(operative)


def should_use_enhance(enhanced: dict | None, evidence_profile: str, mode: str) -> bool:
    """Post-grade decision to actually inject the enhanced prompt."""
    if not enhanced or not enhanced.get("enhanced_prompt"):
        return False
    if evidence_profile != "code":
        return False
    return mode in ("normal", "deep")


# --- hard gates -------------------------------------------------------------


def contains_repo_cmd(text: str) -> bool:
    return bool(_REPO_CMD_RE.search(text or ""))


def gate_enhanced(enhanced: dict) -> dict | None:
    """Apply deterministic hard gates to the enhancer output; None on violation."""
    text = str(enhanced.get("enhanced_prompt") or "").strip()
    if not text:
        return None
    if contains_repo_cmd(text):
        return None
    cited = enhanced.get("cited_ranges") or []
    if not isinstance(cited, list):
        cited = []
    cited = [str(r) for r in cited if isinstance(r, (str, int, float))][:8]
    return {"enhanced_prompt": text[:ENHANCED_CHAR_CAP], "cited_ranges": cited}


# --- subprocess -------------------------------------------------------------


def _entrypoint_path() -> Path | None:
    """Resolve skills/unitrace/scripts/enhance-prompt.mjs relative to this file.

    This file lives at <plugin>/scripts/gate/submit_enhance.py, so the entrypoint
    is at <plugin>/skills/unitrace/scripts/enhance-prompt.mjs = parents[2]/...
    """
    try:
        p = Path(__file__).resolve().parents[2] / "skills" / "unitrace" / "scripts" / "enhance-prompt.mjs"
        return p if p.is_file() else None
    except Exception:
        return None


def run_enhancer(prompt: str, cwd: str, timeout_ms: int | None = None) -> dict | None:
    """Run the Node enhancer subprocess with a hard timeout; fail-open to None.

    Returns the gated {enhanced_prompt, cited_ranges} dict, or None on any
    error / timeout / missing dependency / hard-gate violation.
    """
    if not prompt or not cwd:
        return None
    node = shutil.which("node")
    if not node:
        return None
    script = _entrypoint_path()
    if not script:
        return None
    timeout = (timeout_ms if timeout_ms and timeout_ms > 0 else _timeout_ms()) / 1000.0
    env = dict(os.environ)
    env["UNITRACE_AST_SKIP_INSTALL"] = env.get("UNITRACE_AST_SKIP_INSTALL", "1")
    payload = json.dumps({"prompt": prompt, "cwd": cwd})
    try:
        proc = subprocess.run(
            [node, str(script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(Path(cwd).resolve()) if Path(cwd).exists() else None,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        obj = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or not obj.get("ok"):
        return None
    return gate_enhanced(obj)


# --- context composition ----------------------------------------------------


def build_enhanced_line(enhanced: dict) -> str:
    """The first context line injected ahead of the static classify_task block."""
    return str(enhanced.get("enhanced_prompt") or "").strip()


def enhance_or_none(prompt: str, cwd: str, evidence_profile: str, mode: str, operative: str | None = None) -> str | None:
    """End-to-end: fire (pre-grade) + run + use (post-grade) -> context line or None.

    Returns the enhanced-prompt context line to prepend, or None to use the
    static baseline. `operative` defaults to `prompt` when not provided.
    """
    op = operative if operative is not None else prompt
    if not fire_enhance(op):
        return None
    enhanced = run_enhancer(prompt, cwd)
    if not should_use_enhance(enhanced, evidence_profile, mode):
        return None
    line = build_enhanced_line(enhanced)
    return line or None
