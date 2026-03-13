const POLL_MS = 1000;

// === Tab switching ===
document.querySelectorAll('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'model') fetchModel();
    if (btn.dataset.tab === 'manage') fetchManage();
  });
});

// === Allocator switch ===
document.getElementById('allocator-select').addEventListener('change', async (e) => {
  await fetch('/api/allocator', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: e.target.value }),
  });
});

// === Estimator switch ===
document.getElementById('estimator-select').addEventListener('change', async (e) => {
  await fetch('/api/estimator', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: e.target.value }),
  });
  fetchModel();
});

// === Live tab polling ===
async function poll() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    updateLive(data);
  } catch (_) {}
  setTimeout(poll, POLL_MS);
}

function updateLive(data) {
  const disconnected = document.getElementById('mode-disconnected');
  const idle = document.getElementById('mode-idle');
  const ref = document.getElementById('mode-reference');
  const practice = document.getElementById('mode-practice');

  // Hide all first
  disconnected.style.display = 'none';
  idle.style.display = 'none';
  ref.style.display = 'none';
  practice.style.display = 'none';

  const gameName = document.getElementById('game-name');
  if (data.game_name) {
    gameName.textContent = data.game_name;
  } else {
    gameName.textContent = '';
  }

  if (!data.tcp_connected) {
    disconnected.style.display = 'block';
    return;
  }

  if (data.mode === 'practice') {
    practice.style.display = 'block';

    const cs = data.current_split;
    if (cs) {
      document.getElementById('current-goal').textContent = splitName(cs);
      document.getElementById('current-attempts').textContent =
        'Attempt ' + (cs.attempt_count || 0);

      const insight = document.getElementById('insight');
      if (cs.drift_info) {
        const arrow = cs.drift_info.drift < 0 ? '↓' : cs.drift_info.drift > 0 ? '↑' : '→';
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
      const refTime = r.reference_time_ms ? formatTime(r.reference_time_ms) : '—';
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

  } else if (data.mode === 'reference') {
    ref.style.display = 'block';
    document.getElementById('ref-sections').textContent =
      'Sections: ' + (data.sections_captured || 0);

  } else {
    // idle
    idle.style.display = 'block';
  }

  if (data.session && data.session.started_at) {
    document.getElementById('session-timer').textContent = elapsedStr(data.session.started_at);
  }
}

// === Model tab ===
async function fetchModel() {
  try {
    const res = await fetch('/api/model');
    const data = await res.json();
    updateModel(data);
  } catch (_) {}
}

function updateModel(data) {
  const body = document.getElementById('model-body');
  body.innerHTML = '';
  (data.splits || []).forEach(s => {
    const tr = document.createElement('tr');
    const driftClass = s.drift_info?.label || 'flat';
    const arrow = s.drift !== null
      ? (s.drift < 0 ? '↓' : s.drift > 0 ? '↑' : '→')
      : '—';
    tr.className = 'drift-row-' + driftClass;
    // Quantitative confidence: show CI range
    let confCell = '—';
    if (s.drift_info && s.drift_info.ci_lower != null) {
      const lo = s.drift_info.ci_lower.toFixed(2);
      const hi = s.drift_info.ci_upper.toFixed(2);
      confCell = '<span class="dim">[' + lo + ', ' + hi + ']</span>';
    }
    tr.innerHTML =
      '<td>' + splitName(s) + '</td>' +
      '<td>' + (s.mu !== null ? s.mu.toFixed(1) : '—') + '</td>' +
      '<td class="drift-' + driftClass + '">' + arrow + ' ' +
        (s.drift !== null ? Math.abs(s.drift).toFixed(2) : '—') + '</td>' +
      '<td>' + confCell + '</td>' +
      '<td>' + (s.marginal_return ? s.marginal_return.toFixed(4) : '—') + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + (s.gold_ms !== null ? formatTime(s.gold_ms) : '—') + '</td>';
    body.appendChild(tr);
  });

  if (data.estimator) {
    document.getElementById('estimator-select').value = data.estimator;
  }
}

// === Utilities ===
function splitName(s) {
  if (s.description) return s.description;
  // Build from level number + goal when description is empty
  let name = 'L' + (s.level_number != null ? s.level_number : '?');
  if (s.goal && s.goal !== 'normal') name += ' (' + s.goal + ')';
  return name;
}

function formatTime(ms) {
  if (ms == null) return '—';
  const s = ms / 1000;
  return s.toFixed(1) + 's';
}

function elapsedStr(startedAt) {
  if (!startedAt) return '';
  const start = new Date(startedAt.endsWith('Z') ? startedAt : startedAt + 'Z');
  const diff = Math.floor((Date.now() - start.getTime()) / 1000);
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m + ':' + String(s).padStart(2, '0');
}

// === Manage tab ===
async function fetchManage() {
  try {
    const refsRes = await fetch('/api/references');
    const refsData = await refsRes.json();
    const refs = refsData.references || [];
    const active = refs.find(r => r.active);
    let splits = [];
    if (active) {
      const splitsRes = await fetch('/api/references/' + active.id + '/splits');
      const splitsData = await splitsRes.json();
      splits = splitsData.splits || [];
    }
    updateManage(refs, splits);
  } catch (_) {}
}

function updateManage(refs, splits) {
  const sel = document.getElementById('ref-select');
  sel.innerHTML = '';
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
        'data-id="' + s.id + '" data-field="description"></td>' +
      '<td>' + s.level_number + '</td>' +
      '<td>' + s.goal + '</td>' +
      '<td>' + (s.reference_time_ms ? formatTime(s.reference_time_ms) : '\u2014') + '</td>' +
      '<td><button class="btn-x" data-id="' + s.id + '">\u2715</button></td>';
    body.appendChild(tr);
  });
}

// Inline edit: blur saves
document.getElementById('split-body').addEventListener('focusout', async (e) => {
  if (!e.target.classList.contains('split-name-input')) return;
  const id = e.target.dataset.id;
  const field = e.target.dataset.field;
  const value = e.target.value;
  await fetch('/api/splits/' + id, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [field]: value }),
  });
});

// Delete split
document.getElementById('split-body').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('btn-x')) return;
  if (!confirm('Remove this split?')) return;
  const id = e.target.dataset.id;
  await fetch('/api/splits/' + id, { method: 'DELETE' });
  fetchManage();
});

// Activate reference on change
document.getElementById('ref-select').addEventListener('change', async (e) => {
  await fetch('/api/references/' + e.target.value + '/activate', { method: 'POST' });
  fetchManage();
});

// Rename reference
document.getElementById('btn-ref-rename').addEventListener('click', async () => {
  const sel = document.getElementById('ref-select');
  const name = prompt('New name:', sel.options[sel.selectedIndex]?.text.replace(' \u25cf', ''));
  if (!name) return;
  await fetch('/api/references/' + sel.value, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
  fetchManage();
});

// Delete reference
document.getElementById('btn-ref-delete').addEventListener('click', async () => {
  if (!confirm('Delete this reference and all its splits?')) return;
  const sel = document.getElementById('ref-select');
  await fetch('/api/references/' + sel.value, { method: 'DELETE' });
  fetchManage();
});

// === Reset button ===
document.getElementById('btn-reset').addEventListener('click', async () => {
  if (!confirm('Clear all session data? This cannot be undone.')) return;
  try {
    const res = await fetch('/api/reset', { method: 'POST' });
    const data = await res.json();
    document.getElementById('reset-status').textContent =
      data.status === 'ok' ? 'Data cleared.' : 'Error clearing data.';
  } catch (_) {
    document.getElementById('reset-status').textContent = 'Error clearing data.';
  }
});

// === Mode control buttons ===
document.getElementById('btn-launch-emu')?.addEventListener('click', async () => {
  const res = await fetch('/api/emulator/launch', { method: 'POST' });
  const data = await res.json();
  if (data.status === 'error') alert(data.message);
});

document.getElementById('btn-ref-start')?.addEventListener('click', async () => {
  await fetch('/api/reference/start', { method: 'POST' });
});

document.getElementById('btn-ref-stop')?.addEventListener('click', async () => {
  await fetch('/api/reference/stop', { method: 'POST' });
});

document.getElementById('btn-practice-start')?.addEventListener('click', async () => {
  await fetch('/api/practice/start', { method: 'POST' });
});

document.getElementById('btn-practice-stop')?.addEventListener('click', async () => {
  await fetch('/api/practice/stop', { method: 'POST' });
});

// === Init ===
poll();
