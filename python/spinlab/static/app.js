import { connectSSE, fetchJSON, postJSON } from './api.js';
import { renderDisconnected, renderIdle, renderReference, renderPractice } from './live.js';
import { fetchModel, initModelTab } from './model.js';
import { fetchManage, initManageTab } from './manage.js';

function updateLive(data) {
  if (!data.tcp_connected) {
    renderDisconnected();
    if (launchedRom) renderLaunched();
    else if (!allRoms.length) loadRomList();
    return;
  }
  launchedRom = null;
  switch (data.mode) {
    case 'reference': renderReference(data); break;
    case 'practice': renderPractice(data); break;
    default: renderIdle(data); break;
  }
  // Refresh active secondary tab
  const activeTab = document.querySelector('.tab.active');
  if (activeTab?.dataset.tab === 'model') fetchModel();
  if (activeTab?.dataset.tab === 'manage') fetchManage();
}

// Tab switching
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

// ROM picker
let allRoms = [];
let launchedRom = null;
async function loadRomList() {
  const data = await fetchJSON('/api/roms');
  if (data?.roms) {
    allRoms = data.roms;
    if (!launchedRom) renderRoms('');
  }
}
function renderLaunched() {
  const container = document.getElementById('mode-disconnected');
  if (!container) return;
  const name = launchedRom.replace(/\.(sfc|smc|fig|swc)$/i, '');
  container.querySelector('#rom-filter').style.display = 'none';
  container.querySelector('#rom-list').style.display = 'none';
  container.querySelector('p').textContent = 'Launched ' + name + ' — waiting for Lua connection...';
  let relaunch = container.querySelector('#btn-relaunch');
  if (!relaunch) {
    relaunch = document.createElement('button');
    relaunch.id = 'btn-relaunch';
    relaunch.className = 'btn-sm';
    relaunch.textContent = 'Pick different ROM';
    relaunch.style.marginTop = '8px';
    relaunch.addEventListener('click', () => {
      launchedRom = null;
      container.querySelector('#rom-filter').style.display = '';
      container.querySelector('#rom-list').style.display = '';
      container.querySelector('#rom-filter').value = '';
      container.querySelector('p').textContent = 'Waiting for emulator...';
      relaunch.remove();
      renderRoms('');
    });
    container.appendChild(relaunch);
  }
}
function renderRoms(filter) {
  const ul = document.getElementById('rom-list');
  if (!ul) return;
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
      renderLaunched();
    });
    ul.appendChild(li);
  });
}
document.getElementById('rom-filter')?.addEventListener('input', (e) => {
  renderRoms(e.target.value);
});

document.getElementById('btn-ref-start')?.addEventListener('click', () =>
  postJSON('/api/reference/start'));

document.getElementById('btn-ref-stop')?.addEventListener('click', () =>
  postJSON('/api/reference/stop'));

document.getElementById('btn-practice-start')?.addEventListener('click', () =>
  postJSON('/api/practice/start'));

document.getElementById('btn-practice-stop')?.addEventListener('click', () =>
  postJSON('/api/practice/stop'));

// Init tabs
initModelTab();
initManageTab();

// Connect SSE (primary) with initial poll for first paint
connectSSE(updateLive);
fetchJSON('/api/state').then(data => { if (data) updateLive(data); });
