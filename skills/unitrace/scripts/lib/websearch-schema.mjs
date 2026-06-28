// websearch-schema.mjs — pointer submit schema + validation for RT websearch.
export const SUBMIT_WEBSEARCH_POINTER_NAME = "submit_websearch_pointer";

function isNonEmptyString(v) {
  return typeof v === "string" && v.trim().length > 0;
}

export function websearchPointerSchema({ fetchLog = [] } = {}) {
  const fetchIndices = fetchLog.map((_, i) => i);
  const urlIndexSchema = { type: "integer", minimum: 0 };
  if (fetchIndices.length) {
    urlIndexSchema.enum = fetchIndices;
    urlIndexSchema.description = `Must be 0..${fetchIndices.length - 1} from FETCH INDEX.`;
  }

  return {
    type: "object",
    additionalProperties: false,
    required: [
      "executive_summary",
      "in_scope_findings",
      "adjacent_out_of_scope",
      "prior_art",
      "gaps_risks",
      "recommended_next_steps",
      "citation_refs",
    ],
    properties: {
      executive_summary: { type: "string" },
      in_scope_findings: { type: "string" },
      adjacent_out_of_scope: { type: "string" },
      prior_art: { type: "string" },
      gaps_risks: { type: "string" },
      recommended_next_steps: { type: "string" },
      citation_refs: {
        type: "array",
        minItems: 1,
        ...(fetchLog.length ? { maxItems: fetchLog.length } : {}),
        items: {
          type: "object",
          additionalProperties: false,
          required: ["url_index", "excerpt_index", "rationale"],
          properties: {
            url_index: urlIndexSchema,
            excerpt_index: { type: "integer", minimum: 0 },
            rationale: { type: "string" },
          },
        },
      },
    },
  };
}

export function validateWebsearchPointer(obj, fetchLog = []) {
  if (!obj || typeof obj !== "object" || Array.isArray(obj)) return "result is not an object";
  if (!isNonEmptyString(obj.executive_summary)) return "executive_summary missing";
  if (!isNonEmptyString(obj.in_scope_findings)) return "in_scope_findings missing";
  if (typeof obj.adjacent_out_of_scope !== "string") return "adjacent_out_of_scope missing";
  if (typeof obj.prior_art !== "string") return "prior_art missing";
  if (typeof obj.gaps_risks !== "string") return "gaps_risks missing";
  if (!isNonEmptyString(obj.recommended_next_steps)) return "recommended_next_steps missing";
  if (!Array.isArray(obj.citation_refs) || !obj.citation_refs.length) return "citation_refs empty";

  for (const cite of obj.citation_refs) {
    if (!cite || typeof cite !== "object") return "invalid citation_refs entry";
    const ui = cite.url_index;
    const ei = cite.excerpt_index;
    if (!Number.isInteger(ui) || ui < 0 || ui >= fetchLog.length) {
      return `invalid url_index ${ui} (fetch log size ${fetchLog.length})`;
    }
    const excerpts = fetchLog[ui]?.excerpts || [];
    if (!Number.isInteger(ei) || ei < 0 || ei >= excerpts.length) {
      return `invalid excerpt_index ${ei} for url_index ${ui}`;
    }
    if (!isNonEmptyString(cite.rationale)) return "citation_refs rationale missing";
    if (/https?:\/\//i.test(cite.rationale)) return "citation_refs must not contain raw URLs";
  }

  for (const field of [
    obj.executive_summary,
    obj.in_scope_findings,
    obj.adjacent_out_of_scope,
    obj.prior_art,
    obj.gaps_risks,
    obj.recommended_next_steps,
  ]) {
    if (/https?:\/\//i.test(String(field || ""))) {
      return "section prose must not contain raw URLs; use citation_refs only";
    }
  }

  return null;
}
