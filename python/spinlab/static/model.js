import { segmentName, formatTime, elapsedStr } from './format.js';
import { fetchJSON, postJSON } from './api.js';

export async function fetchModel() {
  const data = await fetchJSON('/api/model');
  if (data) updateModel(data);
}

function updateModel(data) {
  const body = document.getElementById('model-body');
  if (!data.segments || !data.segments.length) {
    body.innerHTML = '<tr><td colspan="7" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = '';
  data.segments.forEach(s => {
    const tr = document.createElement('tr');
    const driftClass = s.drift_info?.label || 'flat';
    const arrow = s.drift !== null
      ? (s.drift < 0 ? '\u2193' : s.drift > 0 ? '\u2191' : '\u2192')
      : '\u2014';
    tr.className = 'drift-row-' + driftClass;
    let confCell = '\u2014';
    if (s.drift_info && s.drift_info.ci_lower != null) {
      const lo = s.drift_info.ci_lower.toFixed(2);
      const hi = s.drift_info.ci_upper.toFixed(2);
      confCell = '<span class="dim">[' + lo + ', ' + hi + ']</span>';
    }
    tr.innerHTML =
      '<td>' + segmentName(s) + '</td>' +
      '<td>' + (s.mu !== null ? s.mu.toFixed(1) : '\u2014') + '</td>' +
      '<td class="drift-' + driftClass + '">' + arrow + ' ' +
        (s.drift !== null ? Math.abs(s.drift).toFixed(2) : '\u2014') + '</td>' +
      '<td>' + confCell + '</td>' +
      '<td>' + (s.marginal_return ? s.marginal_return.toFixed(4) : '\u2014') + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + formatTime(s.gold_ms) + '</td>';
    body.appendChild(tr);
  });
  if (data.estimator) {
    document.getElementById('estimator-select').value = data.estimator;
  }
}

export function updatePracticeCard(data) {
  const card = document.getElementById('practice-card');
  if (data.mode !== 'practice' || !data.current_segment) {
    card.style.display = 'none';
    return;
  }
  card.style.display = '';

  const cs = data.current_segment;
  document.getElementById('current-goal').textContent = segmentName(cs);
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

  const queue = document.getElementById('queue');
  queue.innerHTML = '';
  (data.queue || []).forEach(q => {
    const li = document.createElement('li');
    li.textContent = segmentName(q);
    queue.appendChild(li);
  });

  const recent = document.getElementById('recent');
  recent.innerHTML = '';
  (data.recent || []).forEach(r => {
    const li = document.createElement('li');
    const time = formatTime(r.time_ms);
    const cls = r.completed ? 'ahead' : 'behind';
    li.innerHTML = '<span class="' + cls + '">' + time + '</span>' +
      ' <span class="dim">' + segmentName(r) + '</span>';
    recent.appendChild(li);
  });

  const stats = document.getElementById('session-stats');
  if (data.session) {
    stats.textContent = (data.session.segments_completed || 0) + '/' +
      (data.session.segments_attempted || 0) + ' cleared | ' +
      elapsedStr(data.session.started_at);
  }

  if (data.allocator) {
    document.getElementById('allocator-select').value = data.allocator;
  }
}

export function initModelTab() {
  document.getElementById('allocator-select').addEventListener('change', async (e) => {
    await postJSON('/api/allocator', { name: e.target.value });
  });
  document.getElementById('estimator-select').addEventListener('change', async (e) => {
    await postJSON('/api/estimator', { name: e.target.value });
    fetchModel();
  });
}
