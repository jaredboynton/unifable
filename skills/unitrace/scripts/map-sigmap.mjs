// map-sigmap.mjs — SigMap-style signature map (TF-IDF file ranking).

import {
  charBudgetFromTokens,
  fitRankedToBudget,
  formatMapLine,
  listRepoFiles,
  readRepoFile,
  renderMapHeader,
  tokenizeQuery,
} from "./map-lib.mjs";
import {
  extractAstSignatures,
  shouldUseAstForFile,
} from "./map-ast-extract.mjs";
import path from "node:path";

const EXTRACTORS = [
  {
    exts: [".py"],
    patterns: [
      /^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(/gm,
      /^\s*class\s+([A-Za-z_]\w*)\s*[:(]/gm,
    ],
    kind: "py",
  },
  {
    exts: [".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"],
    patterns: [
      /^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(/gm,
      /^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)\s*[<{]/gm,
      /^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(/gm,
      /^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\b/gm,
    ],
    kind: "js",
  },
  {
    exts: [".go"],
    patterns: [
      /^\s*func\s+(?:\([^)]*\)\s+)?([A-Za-z_]\w*)\s*\(/gm,
      /^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)\b/gm,
    ],
    kind: "go",
  },
  {
    exts: [".rs"],
    patterns: [
      /^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*[<(]/gm,
      /^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)\b/gm,
    ],
    kind: "rs",
  },
  {
    exts: [".sh", ".bash"],
    patterns: [
      /^\s*([A-Za-z_]\w*)\s*\(\)\s*\{/gm,
      /^\s*function\s+([A-Za-z_]\w*)\s*\(/gm,
    ],
    kind: "sh",
  },
];

function lineNumberAt(content, index) {
  return content.slice(0, index).split(/\r?\n/).length;
}

export function extractSignaturesRegex(relPath, content) {
  const ext = relPath.slice(relPath.lastIndexOf(".")).toLowerCase();
  const rules = EXTRACTORS.filter((r) => r.exts.includes(ext));
  if (!rules.length) return [];

  const sigs = [];
  for (const rule of rules) {
    for (const pattern of rule.patterns) {
      pattern.lastIndex = 0;
      let match;
      while ((match = pattern.exec(content)) !== null) {
        const name = match[1];
        if (!name) continue;
        const line = lineNumberAt(content, match.index);
        sigs.push({ name, line, kind: "def", lang: rule.kind });
      }
    }
  }

  sigs.sort((a, b) => a.line - b.line || a.name.localeCompare(b.name));
  const seen = new Set();
  return sigs.filter((s) => {
    const key = `${s.line}:${s.name}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function extractSignatures(relPath, content, options = {}) {
  const { repoRoot = null, fileCount = 0, binary = null } = options;

  if (shouldUseAstForFile(relPath, { fileCount })) {
    if (!repoRoot) return [];
    const absPath = path.isAbsolute(relPath) ? relPath : path.resolve(repoRoot, relPath);
    const astSigs = extractAstSignatures(absPath, {
      binary: options.binary !== undefined ? options.binary : undefined,
    });
    if (astSigs.length) return astSigs;
    if ((process.env.UNITRACE_MAP_AST ?? "auto") === "1") {
      return extractSignaturesRegex(relPath, content);
    }
    return [];
  }

  return extractSignaturesRegex(relPath, content);
}

export function buildSigmapIndex(repoRoot, files) {
  const docs = [];
  const extractOpts = { repoRoot, fileCount: files.length };
  for (const rel of files) {
    const content = readRepoFile(repoRoot, rel);
    if (content == null) continue;
    const signatures = extractSignatures(rel, content, extractOpts);
    if (!signatures.length) continue;
    const terms = new Set();
    for (const sig of signatures) {
      terms.add(sig.name.toLowerCase());
      for (const part of rel.toLowerCase().split(/[/._-]+/)) {
        if (part.length >= 2) terms.add(part);
      }
    }
    docs.push({ rel, signatures, terms: [...terms], termFreq: countTerms([...terms]) });
  }
  return docs;
}

function countTerms(terms) {
  const tf = new Map();
  for (const t of terms) tf.set(t, (tf.get(t) || 0) + 1);
  return tf;
}

export function scoreSigmapDocs(query, docs) {
  const qTerms = tokenizeQuery(query);
  if (!qTerms.length) {
    return docs.map((d) => ({ ...d, score: d.signatures.length }));
  }

  const df = new Map();
  for (const doc of docs) {
    for (const term of new Set(doc.terms)) {
      df.set(term, (df.get(term) || 0) + 1);
    }
  }
  const n = docs.length || 1;

  return docs
    .map((doc) => {
      let score = 0;
      for (const qt of qTerms) {
        const tf = doc.termFreq.get(qt) || 0;
        if (!tf) continue;
        const idf = Math.log(1 + n / (1 + (df.get(qt) || 0)));
        score += tf * idf;
      }
      if (score === 0) score = doc.signatures.length * 0.01;
      return { ...doc, score };
    })
    .sort((a, b) => b.score - a.score || a.rel.localeCompare(b.rel));
}

function renderSigmapSlice(docs) {
  const lines = [renderMapHeader("sigmap")];
  for (const doc of docs) {
    lines.push(`## ${doc.rel}`);
    for (const sig of doc.signatures.slice(0, 12)) {
      lines.push(formatMapLine(doc.rel, sig.line, sig.line, sig.name, sig.kind));
    }
  }
  return lines.join("\n");
}

export function generateSigmapMap(repoRoot, query, options = {}) {
  const budgetChars = options.budgetChars ?? charBudgetFromTokens(options.budgetTokens ?? 1024);
  const files = options.files ?? listRepoFiles(repoRoot);
  const docs = buildSigmapIndex(repoRoot, files);
  const ranked = scoreSigmapDocs(query, docs);
  return fitRankedToBudget(ranked, renderSigmapSlice, budgetChars);
}
