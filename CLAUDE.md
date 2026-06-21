@AGENTS.md

## Claude Code

The prime directive in AGENTS.md applies to this plugin's own development: if a
behavior must happen, it ships as a hook in `hooks/hooks.json`, never as a skill
the model can skip. When adding enforcement, wire the hook first, then prove it
with a test under `tests/`.
