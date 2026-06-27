# .github - agent notes

## Scope

These rules apply to GitHub templates and repository automation metadata.

## Rules

- Keep issue and PR templates short, actionable, and consistent with the repo's
  evidence-gated development model.
- Do not add CI assumptions here unless a workflow file exists in this directory.
- Any release automation added here must match the release contract in the root
  `AGENTS.md`.

## Verification

- Template-only changes: inspect rendered YAML/Markdown shape.
- Workflow changes: validate with the nearest GitHub Actions linter or `gh`
  command before release.
