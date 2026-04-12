import type { SegmentLike } from "./types";

export function shortEndpoint(type: string, ordinal: number): string {
  return type === "entrance" ? "start" : type === "goal" ? "goal" : "cp" + ordinal;
}

export function segmentName(s: SegmentLike): string {
  if (s.description) return s.description;
  return "L" + s.level_number + " " + shortEndpoint(s.start_type, s.start_ordinal) + " → " + shortEndpoint(s.end_type, s.end_ordinal);
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
