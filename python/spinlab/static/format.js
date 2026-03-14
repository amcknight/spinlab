export function splitName(s) {
  if (s.description) return s.description;
  let name = 'L' + (s.level_number != null ? s.level_number : '?');
  if (s.goal && s.goal !== 'normal') name += ' (' + s.goal + ')';
  return name;
}

export function formatTime(ms) {
  if (ms == null) return '\u2014';
  const s = ms / 1000;
  return s.toFixed(1) + 's';
}

export function elapsedStr(startedAt) {
  if (!startedAt) return '';
  const start = new Date(startedAt);
  if (!Number.isFinite(start.getTime())) return '0:00';
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m + ':' + String(s).padStart(2, '0');
}
