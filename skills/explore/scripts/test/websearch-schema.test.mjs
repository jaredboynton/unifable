import test from "node:test";
import assert from "node:assert/strict";
import {
  SUBMIT_WEBSEARCH_POINTER_NAME,
  validateWebsearchPointer,
  websearchPointerSchema,
} from "../lib/websearch-schema.mjs";

const fetchLog = [
  { fetchIndex: 0, url: "https://a.example", excerpts: ["excerpt a"] },
  { fetchIndex: 1, url: "https://b.example", excerpts: ["excerpt b", "excerpt b2"] },
];

test("websearchPointerSchema maxItems matches fetch log size", () => {
  const small = websearchPointerSchema({ fetchLog: [{ excerpts: ["a"] }] });
  assert.equal(small.properties.citation_refs.maxItems, 1);
  const large = websearchPointerSchema({
    fetchLog: Array.from({ length: 15 }, (_, i) => ({ excerpts: [`e${i}`] })),
  });
  assert.equal(large.properties.citation_refs.maxItems, 15);
});

test("validateWebsearchPointer accepts valid pointer", () => {
  const pointer = {
    executive_summary: "Summary.",
    in_scope_findings: "Findings.",
    adjacent_out_of_scope: "Adjacent.",
    prior_art: "Prior.",
    gaps_risks: "Gaps.",
    recommended_next_steps: "Next steps.",
    citation_refs: [{ url_index: 1, excerpt_index: 1, rationale: "second excerpt" }],
  };
  assert.equal(validateWebsearchPointer(pointer, fetchLog), null);
});

test("validateWebsearchPointer rejects bad excerpt_index", () => {
  const pointer = {
    executive_summary: "Summary.",
    in_scope_findings: "Findings.",
    adjacent_out_of_scope: "",
    prior_art: "",
    gaps_risks: "",
    recommended_next_steps: "Next.",
    citation_refs: [{ url_index: 0, excerpt_index: 5, rationale: "bad" }],
  };
  assert.match(validateWebsearchPointer(pointer, fetchLog), /excerpt_index/);
});

test("submit schema name constant", () => {
  assert.equal(SUBMIT_WEBSEARCH_POINTER_NAME, "submit_websearch_pointer");
});
