// bench-scorer.mjs — scoring helpers for search precision bench.

export function normalizePath(p) {
  return (p || "").replace(/\\/g, "/").replace(/^\.\/+/, "");
}

export function parseSearchJsonOutput(text) {
  try {
    const data = JSON.parse(text);
    if (Array.isArray(data)) return data;
  } catch {
    /* fall through */
  }
  return [];
}

export function pathsFromRefs(refs) {
  const paths = [];
  const seen = new Set();
  for (const ref of refs || []) {
    const p = normalizePath(ref.path);
    if (!p || seen.has(p)) continue;
    seen.add(p);
    paths.push(p);
  }
  return paths;
}

export function hitAtK(paths, expectedPath, k) {
  const exp = normalizePath(expectedPath);
  const top = paths.slice(0, k);
  return top.some((p) => p === exp || p.endsWith(`/${exp}`) || exp.endsWith(`/${p}`));
}

export function lineIou(returned, expected) {
  if (!returned?.startLine || !returned?.endLine || !expected?.startLine || !expected?.endLine) {
    return returned?.path && normalizePath(returned.path) === normalizePath(expected.path) ? 0.5 : 0;
  }
  const a0 = returned.startLine;
  const a1 = returned.endLine;
  const b0 = expected.startLine;
  const b1 = expected.endLine;
  const inter = Math.max(0, Math.min(a1, b1) - Math.max(a0, b0) + 1);
  const union = Math.max(a1, b1) - Math.min(a0, b0) + 1;
  return union > 0 ? inter / union : 0;
}

export function scoreSearchResult(refs, expect) {
  const paths = pathsFromRefs(refs);
  const matching = (refs || []).find((r) => {
    const p = normalizePath(r.path);
    const e = normalizePath(expect.path);
    return p === e || p.endsWith(`/${e}`) || e.endsWith(`/${p}`);
  });
  return {
    hit1: hitAtK(paths, expect.path, 1),
    hit5: hitAtK(paths, expect.path, 5),
    lineIou: lineIou(matching, expect),
    empty: !refs?.length,
    paths,
  };
}

export function aggregateScores(rows) {
  const n = rows.length || 1;
  return {
    count: rows.length,
    hit1: rows.filter((r) => r.hit1).length / n,
    hit5: rows.filter((r) => r.hit5).length / n,
    avgLineIou: rows.reduce((s, r) => s + r.lineIou, 0) / n,
    emptyRate: rows.filter((r) => r.empty).length / n,
    medianMapMs: median(rows.map((r) => r.mapMs || 0)),
    medianSearchMs: median(rows.map((r) => r.searchMs || 0)),
    medianTotalMs: median(rows.map((r) => (r.mapMs || 0) + (r.searchMs || 0))),
  };
}

function median(nums) {
  const arr = [...nums].sort((a, b) => a - b);
  if (!arr.length) return 0;
  const mid = Math.floor(arr.length / 2);
  return arr.length % 2 ? arr[mid] : (arr[mid - 1] + arr[mid]) / 2;
}

export function recommendMode(summaryByMode) {
  const modes = Object.keys(summaryByMode);
  const baseline = summaryByMode.none?.hit1 ?? 0;
  let bestSingle = { mode: "none", hit1: baseline };
  for (const mode of ["pagerank", "sigmap"]) {
    const hit1 = summaryByMode[mode]?.hit1 ?? 0;
    if (hit1 > bestSingle.hit1) bestSingle = { mode, hit1 };
  }
  const tandemHit = summaryByMode.tandem?.hit1 ?? 0;
  const bestSingleHit = bestSingle.hit1;
  const tandemTotal = summaryByMode.tandem?.medianTotalMs ?? Infinity;
  const bestSingleTotal = summaryByMode[bestSingle.mode]?.medianTotalMs ?? Infinity;

  if (tandemHit >= bestSingleHit + 0.03 && tandemTotal <= bestSingleTotal * 2) {
    return { pick: "tandem", reason: "tandem hit@1 +3pp within 2x latency" };
  }
  if (bestSingleHit >= baseline + 0.02) {
    return { pick: bestSingle.mode, reason: `best single mode beat none by ${((bestSingleHit - baseline) * 100).toFixed(1)}pp hit@1` };
  }
  return { pick: "none", reason: "no mode beat none by >= 2pp hit@1" };
}
