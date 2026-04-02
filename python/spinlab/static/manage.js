import { segmentName, formatTime } from './format.js';
import { fetchJSON, postJSON } from './api.js';

let lastState = null;

export async function fetchManage() {
  const refsData = await fetchJSON('/api/references');
  if (!refsData) return;
  const refs = refsData.references || [];

  // During reference/replay, show segments from the live capture run
  let segments = [];
  const captureId = lastState?.capture_run_id;
  if (captureId) {
    const segmentsData = await fetchJSON('/api/references/' + captureId + '/segments');
    segments = segmentsData?.segments || [];
  } else {
    const active = refs.find(r => r.active);
    if (active) {
      const segmentsData = await fetchJSON('/api/references/' + active.id + '/segments');
      segments = segmentsData?.segments || [];
    }
  }
  updateManage(refs, segments);
}

function updateManage(refs, segments) {
  const sel = document.getElementById('ref-select');
  const btnStart = document.getElementById('btn-ref-start');
  const btnReplay = document.getElementById('btn-replay');
  const draftPrompt = document.getElementById('draft-prompt');

  // Lock controls during active capture/replay or draft pending
  const busy = lastState && (lastState.mode === 'reference' || lastState.mode === 'replay');
  const hasDraft = lastState?.draft != null;

  sel.disabled = busy || hasDraft;
  btnStart.disabled = busy || hasDraft || !lastState?.tcp_connected;
  document.getElementById('btn-ref-rename').disabled = busy || hasDraft;
  document.getElementById('btn-ref-delete').disabled = busy || hasDraft;

  // Draft prompt
  if (hasDraft) {
    draftPrompt.style.display = '';
    document.getElementById('draft-summary').textContent =
      '\u2713 Captured ' + lastState.draft.segments_captured + ' segments';
  } else {
    draftPrompt.style.display = 'none';
  }

  // Populate dropdown
  sel.innerHTML = '';
  if (!refs.length) {
    const opt = document.createElement('option');
    opt.textContent = 'No references';
    opt.disabled = true;
    sel.appendChild(opt);
    document.getElementById('segment-body').innerHTML = '';
    btnReplay.disabled = true;
    return;
  }
  refs.forEach(r => {
    const opt = document.createElement('option');
    opt.value = r.id;
    opt.textContent = r.name + (r.active ? ' \u25cf' : '');
    if (r.active) opt.selected = true;
    sel.appendChild(opt);
  });

  // Replay button — enabled if selected ref has spinrec
  const selectedRef = refs.find(r => r.id === sel.value);
  btnReplay.disabled = busy || hasDraft || !selectedRef?.has_spinrec || !lastState?.tcp_connected;

  // Segments table
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
      '<td>' + (s.start_type === 'entrance' ? 'start' : 'cp' + s.start_ordinal) +
        ' \u2192 ' + (s.end_type === 'goal' ? 'goal' : 'cp' + s.end_ordinal) + '</td>' +
      '<td>' + stateCell + '</td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
}

export function updateManageState(data) {
  lastState = data;
}

export function initManageTab() {
  // Segment name editing (event delegation)
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

  // Segment delete and fill-gap (event delegation)
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

  // Reference dropdown change
  document.getElementById('ref-select').addEventListener('change', async (e) => {
    await postJSON('/api/references/' + e.target.value + '/activate');
    fetchManage();
  });

  // Rename
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

  // Delete
  document.getElementById('btn-ref-delete').addEventListener('click', async () => {
    if (!confirm('Delete this reference and all its segments?')) return;
    const sel = document.getElementById('ref-select');
    await fetchJSON('/api/references/' + sel.value, { method: 'DELETE' });
    fetchManage();
  });

  // Start reference run
  document.getElementById('btn-ref-start').addEventListener('click', () =>
    postJSON('/api/reference/start'));

  // Replay
  document.getElementById('btn-replay').addEventListener('click', async () => {
    const sel = document.getElementById('ref-select');
    await postJSON('/api/replay/start', { ref_id: sel.value });
  });

  // Draft save
  document.getElementById('btn-draft-save').addEventListener('click', async () => {
    const name = document.getElementById('draft-name').value.trim();
    if (!name) { document.getElementById('draft-name').focus(); return; }
    await postJSON('/api/references/draft/save', { name });
    document.getElementById('draft-name').value = '';
    fetchManage();
  });

  // Draft discard
  document.getElementById('btn-draft-discard').addEventListener('click', async () => {
    if (!confirm('Discard this capture? This cannot be undone.')) return;
    await postJSON('/api/references/draft/discard');
    fetchManage();
  });

  // Reset
  document.getElementById('btn-reset').addEventListener('click', async () => {
    if (!confirm('Clear all session data? This cannot be undone.')) return;
    const data = await postJSON('/api/reset');
    document.getElementById('reset-status').textContent =
      data?.status === 'ok' ? 'Data cleared.' : 'Error clearing data.';
  });
}
