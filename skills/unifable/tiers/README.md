# unifable model tiers

unifable ships three tier files, one per model family. Load the file that matches the
model running the session (or the model pinned by the parent skill).

| Model | Tier file | Posture |
|---|---|---|
| Opus / Fable | `opus.md` | Full procedure — every section of SKILL.md applies |
| Sonnet | `sonnet.md` | Lean procedure — core invariants kept, heavy delegation stripped |
| Haiku | `haiku.md` | Minimal — evidence + verification only; mechanical tasks only |

## How to select

**Default.** The base `SKILL.md` targets the Opus/Fable tier. If no tier is specified,
treat SKILL.md as authoritative and ignore these files.

**Explicit selection.** When a host, parent skill, or user pins a model, append the
matching tier file's content to the active instruction set. The tier file is additive: it
narrows or expands specific sections of SKILL.md without replacing the rest.

**Runtime detection.** If the host exposes the current model identity (e.g. via a
`model` field in the session context), auto-select: `claude-opus-*` or `claude-fable-*`
→ opus.md; `claude-sonnet-*` → sonnet.md; `claude-haiku-*` → haiku.md.

## Escalation direction

Haiku → Sonnet → Opus. Each tier's file specifies its escalation signal. When a tier
cannot complete the task reliably, it names the gap and recommends the next tier up — it
does not attempt to paper over the limit.
