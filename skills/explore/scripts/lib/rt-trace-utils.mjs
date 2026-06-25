// Shared prompt parsing helpers for trace-rt.

export function extractQuestion(prompt) {
  const marker = "QUESTION:";
  const idx = prompt.lastIndexOf(marker);
  if (idx === -1) return prompt.trim();
  return prompt.slice(idx + marker.length).trim();
}

export function extractMapBlock(prompt) {
  if (!prompt || typeof prompt !== "string") return "";
  const m = prompt.match(/\nREPO MAP:\n([\s\S]*?)\nQUESTION:/);
  return m ? m[1].trim() : "";
}

export function compactMapBlock(mapBlock, maxLines = 12) {
  if (!mapBlock) return "";
  const lines = [];
  for (const line of mapBlock.split("\n")) {
    const t = line.trim();
    if (!t || t.startsWith("#") || t.startsWith("<") || t.startsWith("##")) continue;
    const m = t.match(/^([^\s:]+:\d+(?:-\d+)?)/);
    if (m) lines.push(m[1]);
    else if (t.includes("/")) lines.push(t.split(/\s+/)[0].replace(/:$/, ""));
    if (lines.length >= maxLines) break;
  }
  return lines.join("\n");
}

export function questionNeedsComparison(question) {
  return /\b(vs|versus|compare|comparison|difference|contrast|differ)\b/i.test(String(question || ""));
}
