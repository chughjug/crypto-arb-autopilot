/**
 * popup.js — Popup UI logic
 */

const $ = id => document.getElementById(id);

// ── Load saved settings ────────────────────────────────────────────────────

async function loadSettings() {
  const data = await chrome.storage.local.get([
    'appUrl', 'sessionToken', 'cryptoPin',
    'defaultQty', 'minEdgeCents', 'autoExecute',
    'executionLog', 'lastPollAt',
  ]);

  if (data.appUrl) $('appUrl').value = data.appUrl;
  if (data.sessionToken) $('sessionToken').value = data.sessionToken;
  if (data.cryptoPin) $('cryptoPin').value = data.cryptoPin;
  if (data.defaultQty != null) $('defaultQty').value = data.defaultQty;
  if (data.minEdgeCents != null) $('minEdgeCents').value = data.minEdgeCents;
  $('autoExecuteToggle').checked = !!data.autoExecute;

  updatePinDots(data.cryptoPin || '');
  updateStatus(data.autoExecute);
  updateLastPoll(data.lastPollAt);
  renderLog(data.executionLog || []);
}

// ── Status pill ────────────────────────────────────────────────────────────

function updateStatus(armed) {
  const pill = $('statusPill');
  const txt = $('statusText');
  pill.className = 'status-pill ' + (armed ? 'armed' : 'disarmed');
  txt.textContent = armed ? 'Armed — watching' : 'Disarmed';
}

function updateLastPoll(ts) {
  const el = $('lastPollText');
  if (!ts) { el.textContent = ''; return; }
  const diff = Math.round((Date.now() - ts) / 1000);
  el.textContent = diff < 60 ? `${diff}s ago` : `${Math.floor(diff/60)}m ago`;
}

// ── PIN dots ───────────────────────────────────────────────────────────────

function updatePinDots(pin) {
  for (let i = 0; i < 6; i++) {
    const dot = $(`pd${i}`);
    if (!dot) continue;
    dot.classList.toggle('filled', i < pin.length);
  }
}

$('cryptoPin').addEventListener('input', e => {
  updatePinDots(e.target.value);
});

// ── Save ───────────────────────────────────────────────────────────────────

$('saveBtn').addEventListener('click', async () => {
  const settings = {
    appUrl: $('appUrl').value.trim().replace(/\/$/, ''),
    sessionToken: $('sessionToken').value.trim(),
    cryptoPin: $('cryptoPin').value.trim(),
    defaultQty: parseInt($('defaultQty').value) || 10,
    minEdgeCents: parseFloat($('minEdgeCents').value) || 2,
    autoExecute: $('autoExecuteToggle').checked,
  };

  // Validate
  if (!settings.appUrl) { showToast('⚠ App URL is required', 'err'); return; }
  if (!settings.cryptoPin || settings.cryptoPin.length !== 6 || !/^\d{6}$/.test(settings.cryptoPin)) {
    showToast('⚠ PIN must be exactly 6 digits', 'err'); return;
  }

  await chrome.storage.local.set(settings);
  updateStatus(settings.autoExecute);
  showToast('✓ Settings saved!', 'ok');
});

// ── Auto-execute toggle ────────────────────────────────────────────────────

$('autoExecuteToggle').addEventListener('change', async e => {
  await chrome.storage.local.set({ autoExecute: e.target.checked });
  updateStatus(e.target.checked);
});

// ── Poll now ───────────────────────────────────────────────────────────────

$('testPollBtn').addEventListener('click', async () => {
  const btn = $('testPollBtn');
  btn.disabled = true;
  btn.textContent = 'Polling…';
  await chrome.runtime.sendMessage({ type: 'MANUAL_TRIGGER' });
  await chrome.storage.local.set({ lastPollAt: Date.now() });
  setTimeout(() => {
    btn.disabled = false;
    btn.textContent = '🔍 Poll now';
    loadLog();
  }, 2000);
});

// ── Clear log ─────────────────────────────────────────────────────────────

$('clearLogBtn').addEventListener('click', async () => {
  await chrome.storage.local.set({ executionLog: [] });
  renderLog([]);
});

// ── Log rendering ─────────────────────────────────────────────────────────

async function loadLog() {
  const { executionLog = [] } = await chrome.storage.local.get('executionLog');
  renderLog(executionLog);
}

function renderLog(entries) {
  const list = $('logList');
  if (!entries.length) {
    list.innerHTML = '<div class="log-empty">No activity yet</div>';
    return;
  }
  list.innerHTML = entries.slice(0, 20).map(e => {
    const ts = new Date(e.ts).toLocaleTimeString();
    const cls = e.msg.includes('✅') ? 'ok' : e.msg.includes('❌') || e.msg.includes('ERROR') ? 'err' : 'info';
    return `<div class="log-entry ${cls}">
      <span class="log-ts">${ts}</span>
      <span class="log-msg">${e.msg}</span>
    </div>`;
  }).join('');
}

// ── Toast ─────────────────────────────────────────────────────────────────

function showToast(msg, type = 'ok') {
  const existing = document.querySelector('.toast');
  if (existing) existing.remove();
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2500);
}

// ── Live log refresh (while popup is open) ────────────────────────────────

setInterval(loadLog, 3000);
setInterval(async () => {
  const { lastPollAt } = await chrome.storage.local.get('lastPollAt');
  updateLastPoll(lastPollAt);
}, 5000);

// ── Init ──────────────────────────────────────────────────────────────────

loadSettings();
