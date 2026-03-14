import { connectSSE, fetchJSON, postJSON } from './api.js';
import { renderDisconnected, renderIdle, renderReference, renderPractice } from './live.js';
import { fetchModel, initModelTab } from './model.js';
import { fetchManage, initManageTab } from './manage.js';

function updateLive(data) {
  if (!data.tcp_connected) return renderDisconnected();
  switch (data.mode) {
    case 'reference': return renderReference(data);
    case 'practice': return renderPractice(data);
    default: return renderIdle(data);
  }
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

// Mode control buttons
document.getElementById('btn-launch-emu')?.addEventListener('click', async () => {
  const data = await postJSON('/api/emulator/launch');
  if (data?.status === 'error') alert(data.message);
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
