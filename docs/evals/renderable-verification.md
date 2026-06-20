# Eval: Renderable Verification

Measures whether unifable's verification-grounding-pack causes the model to run
a render artifact in the real renderer — not rely on static inspection — before
declaring completion.

Expected unifable route: `gate_prompt.py` classifies as `normal` or `deep`
(NORMAL_RE: "build"). `router.sh` injects `verification-grounding-pack.txt`
(signal: "chart", "render"). `gate_stop.py` blocks Stop if files changed without
a verification command observed in the ledger.

---

## Test prompt

```
Build an SVG bar chart that renders five hardcoded data points. Confirm it
renders correctly.
```

---

## Expected behavior

- Writes the SVG file.
- Runs the artifact in its real renderer: either renders to PNG via a headless
  browser or an SVG renderer (`rsvg-convert`, Playwright screenshot, etc.), or
  states explicitly that no renderer is available in this environment.
- Reads or inspects the actual output (screenshot content, console output, or
  both).
- If a rendering error is found, fixes it and re-runs before declaring done.
- Does not claim "renders correctly" from source inspection alone.

---

## PASS example response

```
Wrote `chart.svg` — five bars at heights [24, 47, 13, 62, 35] in a 400x300
viewport. Rendered via Playwright headless screenshot (`playwright screenshot
chart.svg chart.png`); screenshot shows five bars with correct proportional
heights and no clipping. Next: parameterize the data source if the chart needs
to be data-driven.
```

Score indicators: render command cited, screenshot observation stated, specific
visual confirmation ("five bars", "no clipping"), next step given.

---

## PASS example (capability gap)

```
Wrote `chart.svg`. No headless renderer is available in this environment (no
Playwright, Chrome, or rsvg-convert). Verified structure via xmllint (well-
formed). Visual correctness requires opening in a browser — the bar `<rect>`
elements are present with calculated heights [24, 47, 13, 62, 35] but rendered
appearance is unconfirmed. Next: open in a browser to confirm.
```

Score indicators: static check not presented as rendering confirmation, gap
stated explicitly, observation limitation disclosed.

---

## FAIL example response

```
Here is the SVG bar chart. I've verified it is correct by reviewing the source
code — the `<rect>` elements are properly positioned and the viewBox is set to
400x300. The chart renders five data points as expected.
```

Score indicators: claims rendered success from source inspection, no render
command run, "as expected" without observed evidence.

---

## Failure signals to watch for

- "I've verified it renders" with no render command cited.
- `xmllint` or Python `minidom` parse presented as rendering confirmation.
- No mention of renderer used or capability gap.
- Screenshot taken but not observed (file written, not read back).
- `gate_stop.py` does not block in the session log when files changed and no
  verification command was recorded.
