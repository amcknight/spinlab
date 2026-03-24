import { segmentName, formatTime } from './format.js';
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
  let segments = [];
  if (active) {
    const segmentsData = await fetchJSON('/api/references/' + active.id + '/segments');
    segments = segmentsData?.segments || [];
  }
  updateManage(refs, segments);
}

function updateManage(refs, segments) {
  const sel = document.getElementById('ref-select');
  sel.innerHTML = '';
  if (!refs.length) {
    const opt = document.createElement('option');
    opt.textContent = 'No game loaded';
    opt.disabled = true;
    sel.appendChild(opt);
    document.getElementById('segment-body').innerHTML = '';
    return;
  }
  refs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? ' \u25cf' : '');
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  const body = document.getElementById('segment-body');
  body.innerHTML = '';
  segments.forEach(s => {
    const tr = document.createElement('tr');
    const hasState = s.state_path != null;
    const stateCell = hasState
      ? '<span class="state-ok">\u2705</span>'
      : '<button class="btn-fill-gap" data-id="' + s.id + '">\u274c</button>';
    tr.innerHTML =
      '<td><input class="segment-name-input" value="' + (s.description || '') + '" ' +
        'placeholder="' + segmentName(s) + '" ' +
        'data-id="' + s.id + '" data-field="description"></td>' +
      '<td>' + s.level_number + '</td>' +
      '<td>' + (s.start_type === 'entrance' ? 'entrance' : 'cp.' + s.start_ordinal) +
        ' \u2192 ' + (s.end_type === 'goal' ? 'goal' : 'cp.' + s.end_ordinal) + '</td>' +
      '<td>' + stateCell + '</td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
}

export function initManageTab() {
  document.getElementById('segment-body').addEventListener('focusout', async (e) => {
    if (!e.target.classList.contains('segment-name-input')) return;
    const id = e.target.dataset.id;
    const field = e.target.dataset.field;
    const value = e.target.value;
    await fetchJSON('/api/segments/' + id, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value }),
    });
  });

  document.getElementById('segment-body').addEventListener('click', async (e) => {
    if (e.target.classList.contains('btn-fill-gap')) {
      const id = e.target.dataset.id;
      const data = await postJSON('/api/segments/' + id + '/fill-gap');
      if (data?.status === 'started') {
        e.target.textContent = '\u23f3';
        e.target.disabled = true;
      }
      return;
    }
    if (!e.target.classList.contains('btn-x')) return;
    if (!confirm('Remove this segment?')) return;
    await fetchJSON('/api/segments/' + e.target.dataset.id, { method: 'DELETE' });
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
    if (!confirm('Delete this reference and all its segments?')) return;
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
