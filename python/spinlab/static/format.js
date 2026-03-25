export function segmentName(s) {
  if (s.description) return s.description;
  const start = s.start_type === 'entrance' ? 'entrance' : s.start_type + '.' + s.start_ordinal;
  const end = s.end_type === 'goal' ? 'goal' : s.end_type + '.' + s.end_ordinal;
  return 'L' + s.level_number + ' ' + start + ' \u2192 ' + end;
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
