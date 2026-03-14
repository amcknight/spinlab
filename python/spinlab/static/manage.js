import { splitName, formatTime } from './format.js';
import { fetchJSON, postJSON } from './api.js';

export async function fetchManage() {
  const refsData = await fetchJSON('/api/references');
  if (!refsData) return;
  const refs = refsData.references || [];
  if (!refs.length) {
    updateManage([], []);
    return;
  }
  const active = refs.find(r => r.active);
  let splits = [];
  if (active) {
    const splitsData = await fetchJSON('/api/references/' + active.id + '/splits');
    splits = splitsData?.splits || [];
  }
  updateManage(refs, splits);
}

function updateManage(refs, splits) {
  const sel = document.getElementById('ref-select');
  sel.innerHTML = '';
  if (!refs.length) {
    const opt = document.createElement('option');
    opt.textContent = 'No game loaded';
    opt.disabled = true;
    sel.appendChild(opt);
    document.getElementById('split-body').innerHTML = '';
    return;
  }
  refs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? ' \u25cf' : '');
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  const body = document.getElementById('split-body');
  body.innerHTML = '';
  splits.forEach(s => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td><input class="split-name-input" value="' + (s.description || '') + '" ' +
        'placeholder="' + splitName(s) + '" ' +
        'data-id="' + s.id + '" data-field="description"></td>' +
      '<td>' + s.level_number + '</td>' +
      '<td>' + s.goal + '</td>' +
      '<td>' + (s.reference_time_ms ? formatTime(s.reference_time_ms) : '\u2014') + '</td>' +
      '<td><input type="checkbox" class="split-toggle" data-id="' + s.id + '" ' +
        'data-field="end_on_goal" ' + (s.end_on_goal ? 'checked' : '') +
        ' title="End practice on goal (uncheck for death-after-goal levels)"></td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
}

export function initManageTab() {
  document.getElementById('split-body').addEventListener('focusout', async (e) => {
    if (!e.target.classList.contains('split-name-input')) return;
    const id = e.target.dataset.id;
    const field = e.target.dataset.field;
    const value = e.target.value;
    await fetchJSON('/api/splits/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value }),
    });
  });

  document.getElementById('split-body').addEventListener('change', async (e) => {
    if (!e.target.classList.contains('split-toggle')) return;
    const id = e.target.dataset.id;
    const field = e.target.dataset.field;
    const value = e.target.checked;
    await postJSON('/api/splits/' + id, { [field]: value });
  });

  document.getElementById('split-body').addEventListener('click', async (e) => {
    if (!e.target.classList.contains('btn-x')) return;
    if (!confirm('Remove this split?')) return;
    await fetchJSON('/api/splits/' + e.target.dataset.id, { method: 'DELETE' });
    fetchManage();
  });

  document.getElementById('ref-select').addEventListener('change', async (e) => {
    await postJSON('/api/references/' + e.target.value + '/activate');
    fetchManage();
  });

  document.getElementById('btn-ref-rename').addEventListener('click', async () => {
    const sel = document.getElementById('ref-select');
    const name = prompt('New name:', sel.options[sel.selectedIndex]?.text.replace(' \u25cf', ''));
    if (!name) return;
    await fetchJSON('/api/references/' + sel.value, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    fetchManage();
  });

  document.getElementById('btn-ref-delete').addEventListener('click', async () => {
    if (!confirm('Delete this reference and all its splits?')) return;
    const sel = document.getElementById('ref-select');
    await fetchJSON('/api/references/' + sel.value, { method: 'DELETE' });
    fetchManage();
  });

  document.getElementById('btn-reset').addEventListener('click', async () => {
    if (!confirm('Clear all session data? This cannot be undone.')) return;
    const data = await postJSON('/api/reset');
    document.getElementById('reset-status').textContent =
      data?.status === 'ok' ? 'Data cleared.' : 'Error clearing data.';
  });
}
