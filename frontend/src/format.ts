import type { SegmentLike } from "./types";

export function segmentName(s: SegmentLike): string {
  if (s.description) return s.description;
  const start = s.start_type === "entrance" ? "start" : "cp" + s.start_ordinal;
  const end = s.end_type === "goal" ? "goal" : "cp" + s.end_ordinal;
  return "L" + s.level_number + " " + start + " → " + end;
}

export function formatTime(ms: number | null | undefined): string {
  if (ms == null) return "—";
  const s = ms / 1000;
  return s.toFixed(1) + "s";
}

export function elapsedStr(startedAt: string | null | undefined): string {
  if (!startedAt) return "";
  const start = new Date(startedAt);
  if (!Number.isFinite(start.getTime())) return "0:00";
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m + ":" + String(s).padStart(2, "0");
}

export function formatSavings(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  const sign = ms >= 0 ? "+" : "-";
  const s = Math.abs(ms) / 1000;
  return sign + s.toFixed(1) + "s";
}
