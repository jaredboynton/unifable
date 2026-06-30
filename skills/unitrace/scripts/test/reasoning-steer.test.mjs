import test from "node:test";
import assert from "node:assert/strict";
import {
  REALTIME_REASONING_STEER,
  withReasoningSteer,
  shouldSteerForEffort,
  DEFAULT_UNITRACE_REASONING_EFFORT,
  DEFAULT_SUBMIT_REASONING_EFFORT,
  realtimeReasoningConfig,
} from "../lib/realtime_client.mjs";

test("withReasoningSteer prepends steer line", () => {
  const out = withReasoningSteer("QUESTION: fix the hook");
  assert.ok(out.startsWith(REALTIME_REASONING_STEER));
  assert.ok(out.includes("QUESTION: fix the hook"));
});

test("withReasoningSteer is idempotent", () => {
  const once = withReasoningSteer("hello");
  const twice = withReasoningSteer(once);
  assert.equal(twice, once);
});

test("withReasoningSteer disabled returns input unchanged", () => {
  assert.equal(withReasoningSteer("hello", false), "hello");
});

test("withReasoningSteer steers for low/none/minimal efforts", () => {
  for (const e of ["low", "none", "minimal", "off", ""]) {
    assert.ok(withReasoningSteer("x", e).startsWith(REALTIME_REASONING_STEER), `effort=${e || "(empty)"}`);
  }
});

test("withReasoningSteer passthrough at medium/high/xhigh", () => {
  for (const e of ["medium", "high", "xhigh"]) {
    assert.equal(withReasoningSteer("x", e), "x", `effort=${e}`);
  }
});

test("shouldSteerForEffort matches policy", () => {
  for (const e of ["low", "none", "minimal", "off", ""]) assert.ok(shouldSteerForEffort(e), e);
  for (const e of ["medium", "high", "xhigh"]) assert.ok(!shouldSteerForEffort(e), e);
});

test("default reasoning efforts match policy", () => {
  assert.equal(DEFAULT_UNITRACE_REASONING_EFFORT, "none");
  assert.equal(DEFAULT_SUBMIT_REASONING_EFFORT, "low");
});

test("realtimeReasoningConfig omits for explore default", () => {
  assert.deepEqual(realtimeReasoningConfig(DEFAULT_UNITRACE_REASONING_EFFORT), {});
});

test("realtimeReasoningConfig sets low for submit default", () => {
  assert.deepEqual(realtimeReasoningConfig(DEFAULT_SUBMIT_REASONING_EFFORT), {
    reasoning: { effort: "low" },
  });
});
