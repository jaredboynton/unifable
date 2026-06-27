# hooks - agent notes

## Scope

These rules apply to Claude/Codex hook entrypoints and hook wiring.

## Rules

- Hooks are the load-bearing enforcement layer. If behavior must happen, wire it
  through `hooks/hooks.json` or the appropriate host hook entrypoint.
- Hook scripts must fail open on malformed input or internal errors unless a
  user-facing safety cap explicitly handles the block.
- Keep host-specific IO and output-shape handling here; host-agnostic policy
  belongs in `scripts/gate/`.
- Hook output must match the host contract exactly. Avoid extra narration.

## Verification

- Run targeted hook tests plus `python3 -m py_compile` on touched hook files.
- For output-shape changes, regenerate and check `docs/generated/`.
