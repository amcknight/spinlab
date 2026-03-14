import { splitName, formatTime, elapsedStr } from './format.js';

export function renderDisconnected() {
  hide('mode-idle', 'mode-reference', 'mode-practice');
  show('mode-disconnected');
}

export function renderIdle(data) {
  hide('mode-disconnected', 'mode-reference', 'mode-practice');
  show('mode-idle');
  updateGameName(data);
  const btn = document.getElementById('btn-practice-start');
  if (btn) {
    const hasSplits = data.game_id != null;
    btn.disabled = !hasSplits;
    btn.title = hasSplits ? '' : 'No splits available — complete a reference run first';
  }
}

export function renderReference(data) {
  hide('mode-disconnected', 'mode-idle', 'mode-practice');
  show('mode-reference');
  updateGameName(data);
  document.getElementById('ref-sections').textContent =
    'Sections: ' + (data.sections_captured || 0);
}

export function renderPractice(data) {
  hide('mode-disconnected', 'mode-idle', 'mode-reference');
  show('mode-practice');
  updateGameName(data);

  const cs = data.current_split;
  if (cs) {
    document.getElementById('current-goal').textContent = splitName(cs);
    document.getElementById('current-attempts').textContent =
      'Attempt ' + (cs.attempt_count || 0);

    const insight = document.getElementById('insight');
    if (cs.drift_info) {
      const arrow = cs.drift_info.drift < 0 ? '\u2193' : cs.drift_info.drift > 0 ? '\u2191' : '\u2192';
      const rate = Math.abs(cs.drift_info.drift).toFixed(2);
      insight.innerHTML =
        '<span class="drift-' + cs.drift_info.label + '">' +
        arrow + ' ' + rate + ' s/run</span>' +
        ' <span class="dim">(' + cs.drift_info.confidence + ')</span>';
    } else {
      insight.textContent = 'No data yet';
    }
  }

  const queue = document.getElementById('queue');
  queue.innerHTML = '';
  (data.queue || []).forEach(q => {
    const li = document.createElement('li');
    li.textContent = splitName(q);
    queue.appendChild(li);
  });

  const recent = document.getElementById('recent');
  recent.innerHTML = '';
  (data.recent || []).forEach(r => {
    const li = document.createElement('li');
    const time = formatTime(r.time_ms);
    const refTime = r.reference_time_ms ? formatTime(r.reference_time_ms) : '\u2014';
    const cls = r.reference_time_ms && r.time_ms <= r.reference_time_ms ? 'ahead' : 'behind';
    li.innerHTML = '<span class="' + cls + '">' + time + '</span> / ' + refTime +
      ' <span class="dim">' + splitName(r) + '</span>';
    recent.appendChild(li);
  });

  const stats = document.getElementById('session-stats');
  if (data.session) {
    stats.textContent = (data.session.splits_completed || 0) + '/' +
      (data.session.splits_attempted || 0) + ' cleared | ' +
      elapsedStr(data.session.started_at);
  }

  if (data.allocator) {
    document.getElementById('allocator-select').value = data.allocator;
  }

  if (data.session && data.session.started_at) {
    document.getElementById('session-timer').textContent = elapsedStr(data.session.started_at);
  }
}

function updateGameName(data) {
  const el = document.getElementById('game-name');
  el.textContent = data.game_name || '';
}

function show(...ids) { ids.forEach(id => document.getElementById(id).style.display = 'block'); }
function hide(...ids) { ids.forEach(id => document.getElementById(id).style.display = 'none'); }
