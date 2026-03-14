import { splitName, formatTime } from './format.js';
import { fetchJSON, postJSON } from './api.js';

export async function fetchModel() {
  const data = await fetchJSON('/api/model');
  if (data) updateModel(data);
}

function updateModel(data) {
  const body = document.getElementById('model-body');
  if (!data.splits || !data.splits.length) {
    body.innerHTML = '<tr><td colspan="7" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = '';
  data.splits.forEach(s => {
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
      '<td>' + splitName(s) + '</td>' +
      '<td>' + (s.mu !== null ? s.mu.toFixed(1) : '\u2014') + '</td>' +
      '<td class="drift-' + driftClass + '">' + arrow + ' ' +
        (s.drift !== null ? Math.abs(s.drift).toFixed(2) : '\u2014') + '</td>' +
      '<td>' + confCell + '</td>' +
      '<td>' + (s.marginal_return ? s.marginal_return.toFixed(4) : '\u2014') + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + (s.gold_ms !== null ? formatTime(s.gold_ms) : '\u2014') + '</td>';
    body.appendChild(tr);
  });
  if (data.estimator) {
    document.getElementById('estimator-select').value = data.estimator;
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
