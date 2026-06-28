// bench-scorer-common.mjs — shared stats/helpers for explore bench scorers.

export function normalizePath(p) {
  return (p || "").replace(/\\/g, "/").replace(/^\.\/+/, "");
}

export function isNonEmpty(text) {
  return /\S/.test(text || "");
}

export function countChars(text) {
  return (text || "").length;
}

export function countMarkdownHeadings(text, maxLevel = 4) {
  const headings = { h1: 0, h2: 0, h3: 0, h4: 0, total: 0 };
  for (let level = 1; level <= maxLevel; level += 1) {
    const re = new RegExp(`^#{${level}}\\s+`, "gm");
    const n = (text.match(re) || []).length;
    headings[`h${level}`] = n;
    headings.total += n;
  }
  return headings;
}

export function median(nums) {
  const arr = [...nums].sort((a, b) => a - b);
  if (!arr.length) return 0;
  const mid = Math.floor(arr.length / 2);
  return arr.length % 2 ? arr[mid] : (arr[mid - 1] + arr[mid]) / 2;
}

export function percentile(nums, p) {
  const arr = [...nums].sort((a, b) => a - b);
  if (!arr.length) return 0;
  const idx = Math.min(arr.length - 1, Math.floor(p * arr.length));
  return arr[idx];
}
