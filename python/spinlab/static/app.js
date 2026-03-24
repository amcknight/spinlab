import { connectSSE, fetchJSON, postJSON } from './api.js';
import { initHeader, updateHeader, loadRomList } from './header.js';
import { updatePracticeCard, fetchModel, initModelTab } from './model.js';
import { fetchManage, initManageTab, updateManageState } from './manage.js';

function updateFromState(data) {
  updateHeader(data);
  updatePracticeCard(data);
  updateManageState(data);  // <-- add this line

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

// Init
initHeader();
initModelTab();
initManageTab();

// Connect SSE with initial poll
connectSSE(updateFromState);
fetchJSON('/api/state').then(data => { if (data) updateFromState(data); });
