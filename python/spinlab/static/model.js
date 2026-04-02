import { segmentName, formatTime, elapsedStr } from './format.js';
import { fetchJSON, postJSON } from './api.js';

const ALLOCATOR_COLORS = {
  greedy: '#4caf50',
  random: '#2196f3',
  round_robin: '#ff9800',
};
const ALLOCATOR_LABELS = {
  greedy: 'Greedy',
  random: 'Random',
  round_robin: 'Round Robin',
};
const ALLOCATOR_ORDER = ['greedy', 'random', 'round_robin'];

let _currentWeights = null;
let _tuningParams = null;

function renderWeightSlider(weights) {
  _currentWeights = { ...weights };
  const slider = document.getElementById('weight-slider');
  const legend = document.getElementById('weight-legend');
  if (!slider || !legend) return;

  slider.innerHTML = '';
  legend.innerHTML = '';

  const entries = ALLOCATOR_ORDER.filter(k => k in weights);

  // Segments
  entries.forEach(name => {
    const seg = document.createElement('div');
    seg.className = 'weight-segment';
    seg.style.flex = weights[name];
    seg.style.background = ALLOCATOR_COLORS[name] || '#666';
    seg.dataset.allocator = name;
    slider.appendChild(seg);
  });

  // Handles (between segments)
  const totalWidth = () => slider.getBoundingClientRect().width;
  for (let i = 0; i < entries.length - 1; i++) {
    const handle = document.createElement('div');
    handle.className = 'weight-handle';
    handle.dataset.index = i;
    _positionHandle(handle, entries, weights, slider);
    slider.appendChild(handle);

    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      handle.classList.add('dragging');
      const left = entries[i];
      const right = entries[i + 1];
      const startX = e.clientX;
      const startLeftW = weights[left];
      const startRightW = weights[right];
      const pxPerPercent = totalWidth() / 100;

      const onMove = (ev) => {
        const dx = ev.clientX - startX;
        const dp = Math.round(dx / pxPerPercent);
        const newLeft = Math.max(0, Math.min(startLeftW + startRightW, startLeftW + dp));
        const newRight = startLeftW + startRightW - newLeft;
        weights[left] = newLeft;
        weights[right] = newRight;
        _updateSliderVisuals(entries, weights, slider, legend);
      };
      const onUp = () => {
        handle.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        _currentWeights = { ...weights };
        _postWeights(weights);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }

  // Legend
  _renderLegend(entries, weights, legend);
}

function _positionHandle(handle, entries, weights, slider) {
  let cumulative = 0;
  const idx = parseInt(handle.dataset.index);
  for (let i = 0; i <= idx; i++) cumulative += weights[entries[i]];
  handle.style.left = cumulative + '%';
}

function _updateSliderVisuals(entries, weights, slider, legend) {
  const segments = slider.querySelectorAll('.weight-segment');
  entries.forEach((name, i) => {
    if (segments[i]) segments[i].style.flex = weights[name];
  });
  const handles = slider.querySelectorAll('.weight-handle');
  handles.forEach(h => _positionHandle(h, entries, weights, slider));
  _renderLegend(entries, weights, legend);
}

function _renderLegend(entries, weights, legend) {
  legend.innerHTML = '';
  entries.forEach(name => {
    const item = document.createElement('span');
    item.className = 'weight-legend-item';
    const dot = document.createElement('span');
    dot.className = 'weight-dot';
    dot.style.background = ALLOCATOR_COLORS[name] || '#666';
    item.appendChild(dot);
    item.appendChild(document.createTextNode(
      (ALLOCATOR_LABELS[name] || name) + ' ' + weights[name] + '%'
    ));
    legend.appendChild(item);
  });
}

async function _postWeights(weights) {
  await postJSON('/api/allocator-weights', weights);
}

export async function fetchModel() {
  const data = await fetchJSON('/api/model');
  if (data) updateModel(data);
}

function updateModel(data) {
  const body = document.getElementById('model-body');
  if (!data.segments || !data.segments.length) {
    body.innerHTML = '<tr><td colspan="6" class="dim">No game loaded</td></tr>';
    return;
  }
  body.innerHTML = '';
  data.segments.forEach(s => {
    const tr = document.createElement('tr');
    const sel = s.model_outputs[s.selected_model];

    tr.innerHTML =
      '<td>' + segmentName(s) + '</td>' +
      '<td>' + formatTime(sel ? sel.expected_time_ms : null) + '</td>' +
      '<td>' + (sel && sel.ms_per_attempt != null ? sel.ms_per_attempt.toFixed(1) + ' ms/att' : '\u2014') + '</td>' +
      '<td>' + formatTime(sel ? sel.floor_estimate_ms : null) + '</td>' +
      '<td>' + s.n_completed + '</td>' +
      '<td>' + formatTime(s.gold_ms) + '</td>';
    body.appendChild(tr);
  });
  const estSelect = document.getElementById('estimator-select');
  if (estSelect && data.estimators) {
    const current = data.estimator || estSelect.value;
    estSelect.innerHTML = '';
    data.estimators.forEach(e => {
      const opt = document.createElement('option');
      opt.value = e.name;
      opt.textContent = e.display_name;
      estSelect.appendChild(opt);
    });
    estSelect.value = current;
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
  const selOut = cs.model_outputs && cs.model_outputs[cs.selected_model];
  if (selOut) {
    const mpa = selOut.ms_per_attempt;
    insight.innerHTML = '<span>' + mpa.toFixed(1) + ' ms/att</span>';
  } else {
    insight.textContent = 'No data yet';
  }

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

  if (data.allocator_weights) {
    renderWeightSlider(data.allocator_weights);
  }
}

export function updatePracticeControls(data) {
  const startBtn = document.getElementById('btn-practice-start');
  const stopBtn = document.getElementById('btn-practice-stop');
  const isPracticing = data.mode === 'practice';
  const canStart = data.tcp_connected && data.game_id && data.mode === 'idle';
  startBtn.style.display = isPracticing ? 'none' : '';
  startBtn.disabled = !canStart;
  stopBtn.style.display = isPracticing ? '' : 'none';
}

async function fetchTuningParams() {
  const data = await fetchJSON('/api/estimator-params');
  if (!data) return;
  _tuningParams = data;
  renderTuningParams(data);
}

function renderTuningParams(data) {
  const container = document.getElementById('tuning-params');
  if (!container) return;
  container.innerHTML = '';
  if (!data.params || data.params.length === 0) {
    container.innerHTML = '<p class="tuning-empty">No tunable parameters</p>';
    return;
  }
  data.params.forEach(p => {
    const row = document.createElement('div');
    row.className = 'tuning-row';
    row.innerHTML =
      '<span class="tuning-label">' + p.display_name + '</span>' +
      '<input type="range" class="tuning-slider" ' +
        'data-param="' + p.name + '" ' +
        'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
        'value="' + p.value + '">' +
      '<input type="number" class="tuning-value" ' +
        'data-param="' + p.name + '" ' +
        'min="' + p.min + '" max="' + p.max + '" step="' + p.step + '" ' +
        'value="' + p.value + '">';
    container.appendChild(row);

    const slider = row.querySelector('.tuning-slider');
    const input = row.querySelector('.tuning-value');
    slider.addEventListener('input', () => { input.value = slider.value; });
    input.addEventListener('input', () => { slider.value = input.value; });
  });
}

function collectTuningParams() {
  const params = {};
  document.querySelectorAll('#tuning-params .tuning-slider').forEach(slider => {
    params[slider.dataset.param] = parseFloat(slider.value);
  });
  return params;
}

async function applyTuningParams() {
  const params = collectTuningParams();
  await postJSON('/api/estimator-params', { params });
  fetchModel();
}

async function resetTuningDefaults() {
  if (!_tuningParams) return;
  _tuningParams.params.forEach(p => {
    const slider = document.querySelector('.tuning-slider[data-param="' + p.name + '"]');
    const input = document.querySelector('.tuning-value[data-param="' + p.name + '"]');
    if (slider) slider.value = p.default;
    if (input) input.value = p.default;
  });
  await applyTuningParams();
}

export function initModelTab() {
  document.getElementById('estimator-select').addEventListener('change', async (e) => {
    await postJSON('/api/estimator', { name: e.target.value });
    fetchModel();
    fetchTuningParams();
  });
  document.getElementById('btn-practice-start').addEventListener('click', () =>
    postJSON('/api/practice/start'));
  document.getElementById('btn-practice-stop').addEventListener('click', () =>
    postJSON('/api/practice/stop'));

  const toggle = document.getElementById('tuning-toggle');
  const panel = document.getElementById('tuning-panel');
  const body = document.getElementById('tuning-body');
  if (toggle) {
    toggle.addEventListener('click', () => {
      panel.classList.toggle('collapsed');
      body.style.display = panel.classList.contains('collapsed') ? 'none' : '';
    });
  }
  document.getElementById('btn-tuning-apply')?.addEventListener('click', applyTuningParams);
  document.getElementById('btn-tuning-reset')?.addEventListener('click', resetTuningDefaults);

  fetchTuningParams();
}
