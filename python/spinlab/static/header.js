import { fetchJSON, postJSON } from './api.js';

let allRoms = [];
let launchedRom = null;
let popoverOpen = false;

export async function loadRomList() {
  const data = await fetchJSON('/api/roms');
  if (data?.roms) allRoms = data.roms;
}

export function updateHeader(data) {
  // Game name
  const gameEl = document.getElementById('game-name');
  const name = data.game_name || localStorage.getItem('spinlab_game_name') || 'No game';
  gameEl.textContent = name;
  if (data.game_name) {
    localStorage.setItem('spinlab_game_name', data.game_name);
    localStorage.setItem('spinlab_game_id', data.game_id);
  }

  // Mode chip
  const chip = document.getElementById('mode-chip');
  const label = document.getElementById('mode-label');
  const stopBtn = document.getElementById('mode-stop');

  chip.className = 'mode-chip';
  stopBtn.style.display = 'none';

  if (!data.tcp_connected) {
    chip.classList.add('disconnected');
    label.textContent = 'Disconnected';
  } else if (data.draft) {
    chip.classList.add('draft');
    label.textContent = 'Draft \u2014 ' + data.draft.segments_captured + ' segments';
  } else if (data.mode === 'reference') {
    chip.classList.add('recording');
    label.textContent = 'Recording \u2014 ' + (data.sections_captured || 0) + ' segments';
    stopBtn.style.display = '';
  } else if (data.mode === 'practice') {
    chip.classList.add('practicing');
    const seg = data.current_segment;
    label.textContent = 'Practicing' + (seg ? ' \u2014 ' + shortSegName(seg) : '');
    stopBtn.style.display = '';
  } else if (data.mode === 'replay') {
    chip.classList.add('replaying');
    label.textContent = 'Replaying\u2026';
    stopBtn.style.display = '';
  } else {
    chip.classList.add('idle');
    label.textContent = 'Idle';
  }
}

function shortSegName(seg) {
  if (seg.description) return seg.description;
  const start = seg.start_type === 'entrance' ? 'ent' : 'cp.' + seg.start_ordinal;
  const end = seg.end_type === 'goal' ? 'goal' : 'cp.' + seg.end_ordinal;
  return 'L' + seg.level_number + ' ' + start + '\u2192' + end;
}

export function initHeader() {
  const selectorBtn = document.getElementById('game-selector');
  const popover = document.getElementById('game-popover');
  const filter = document.getElementById('rom-filter');

  // Restore last game name
  const lastGame = localStorage.getItem('spinlab_game_name');
  if (lastGame) document.getElementById('game-name').textContent = lastGame;

  selectorBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    popoverOpen = !popoverOpen;
    popover.style.display = popoverOpen ? '' : 'none';
    if (popoverOpen) {
      filter.value = '';
      renderRoms('');
      filter.focus();
      if (!allRoms.length) loadRomList().then(() => renderRoms(''));
    }
  });

  filter.addEventListener('input', (e) => renderRoms(e.target.value));

  // Close on click outside or Escape
  document.addEventListener('click', (e) => {
    if (popoverOpen && !popover.contains(e.target) && e.target !== selectorBtn) {
      popoverOpen = false;
      popover.style.display = 'none';
    }
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && popoverOpen) {
      popoverOpen = false;
      popover.style.display = 'none';
    }
  });

  // Stop button
  document.getElementById('mode-stop').addEventListener('click', async () => {
    // Determine what to stop based on current chip class
    const chip = document.getElementById('mode-chip');
    if (chip.classList.contains('recording')) await postJSON('/api/reference/stop');
    else if (chip.classList.contains('practicing')) await postJSON('/api/practice/stop');
    else if (chip.classList.contains('replaying')) await postJSON('/api/replay/stop');
  });
}

function renderRoms(filter) {
  const ul = document.getElementById('rom-list');
  ul.innerHTML = '';
  const lf = filter.toLowerCase();
  const matches = allRoms.filter(r => r.toLowerCase().includes(lf));
  matches.forEach(rom => {
    const li = document.createElement('li');
    li.textContent = rom.replace(/\.(sfc|smc|fig|swc)$/i, '');
    li.addEventListener('click', async () => {
      const res = await postJSON('/api/emulator/launch', { rom });
      if (res?.status === 'error') { alert(res.message); return; }
      launchedRom = rom;
      // Close popover
      popoverOpen = false;
      document.getElementById('game-popover').style.display = 'none';
    });
    ul.appendChild(li);
  });
}
