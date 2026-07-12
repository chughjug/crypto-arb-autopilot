/**
 * background.js — Service Worker
 *
 * Polls your autopilot dashboard for new crypto.com arb opportunities,
 * opens the right event page, and sends trade commands to the content script.
 */

const POLL_ALARM = 'arbPoll';
const POLL_INTERVAL_MINUTES = 0.25; // every 15 seconds

// ── State (in-memory; persists across alarm wakes via chrome.storage) ──────
let seenKeys = new Set();
let activeTabId = null;
let pendingTrade = null;
let isExecuting = false;

// ── Helpers ────────────────────────────────────────────────────────────────

async function cfg() {
  return chrome.storage.local.get([
    'appUrl', 'sessionToken', 'cryptoPin', 'defaultQty',
    'minEdgeCents', 'autoExecute', 'seenKeys',
  ]);
}

function oppKey(opp) {
  const yv = opp.per_venue?.cryptocom || {};
  return `${opp.coin}|${opp.expiry}|${opp.yes_strike ?? opp.strike}|${opp.yes_venue}>${opp.no_venue}`;
}

function log(msg, data = null) {
  const entry = { ts: Date.now(), msg, data };
  console.log('[CryptoArb]', msg, data || '');
  chrome.storage.local.get(['executionLog'], ({ executionLog = [] }) => {
    executionLog.unshift(entry);
    if (executionLog.length > 100) executionLog.length = 100;
    chrome.storage.local.set({ executionLog });
  });
}

function badge(text, color = '#059669') {
  chrome.action.setBadgeText({ text: String(text) });
  chrome.action.setBadgeBackgroundColor({ color });
}

// ── Polling ────────────────────────────────────────────────────────────────

async function poll() {
  const conf = await cfg();
  if (!conf.autoExecute || !conf.appUrl || !conf.sessionToken) return;

  let data;
  try {
    const url = conf.appUrl.replace(/\/$/, '') + '/api/autopilot/bankroll';
    const res = await fetch(url, {
      headers: { 'Cookie': `session=${conf.sessionToken}` },
      credentials: 'include',
    });
    if (!res.ok) { log('Poll failed: HTTP ' + res.status); return; }
    data = await res.json();
  } catch (e) {
    log('Poll error: ' + e.message);
    return;
  }

  // Restore seen keys from storage on first run
  if (seenKeys.size === 0 && conf.seenKeys) {
    seenKeys = new Set(conf.seenKeys);
  }

  const opportunities = data?.scanner?.opportunities || [];

  // Filter: crypto.com must be one of the venues, edge above threshold
  const minEdge = (Number(conf.minEdgeCents) || 2) / 100;
  const cryptoOpps = opportunities.filter(opp => {
    const hasCrypto = opp.yes_venue === 'cryptocom' || opp.no_venue === 'cryptocom';
    const edge = Number(opp.max_arb ?? 0);
    const key = oppKey(opp);
    return hasCrypto && edge >= minEdge && !seenKeys.has(key);
  });

  if (!cryptoOpps.length) return;

  // Sort by best edge first
  cryptoOpps.sort((a, b) => Number(b.max_arb) - Number(a.max_arb));
  const best = cryptoOpps[0];
  const key = oppKey(best);
  seenKeys.add(key);

  // Persist seen keys
  const keysArr = [...seenKeys].slice(-500);
  chrome.storage.local.set({ seenKeys: keysArr });

  log(`New arb detected: ${best.coin} edge=${(Number(best.max_arb)*100).toFixed(2)}¢`, best);
  badge('NEW', '#059669');

  // Fire desktop notification
  chrome.notifications.create(key, {
    type: 'basic',
    iconUrl: 'icons/icon48.png',
    title: `📈 Arb: ${best.coin} +${(Number(best.max_arb)*100).toFixed(2)}¢`,
    message: `crypto.com ${best.yes_venue === 'cryptocom' ? 'YES' : 'NO'} · expires ${fmtTtl(best.expires_in_s)}`,
    priority: 2,
    requireInteraction: false,
  });

  if (!isExecuting) {
    executeArb(best, conf, data?.scanner?.cc_pin);
  } else {
    log('Skipping — already executing another trade');
  }
}

function fmtTtl(s) {
  if (s == null) return '?';
  if (s > 3600) return `${Math.floor(s/3600)}h`;
  if (s > 60) return `${Math.floor(s/60)}m`;
  return `${s}s`;
}

// ── Execution orchestration ────────────────────────────────────────────────

async function executeArb(opp, conf, ccPin) {
  isExecuting = true;
  badge('…', '#6366f1');

  try {
    // Determine which side crypto.com is on
    const cryptoSide = opp.yes_venue === 'cryptocom' ? 'yes' : 'no';
    const cryptoPv = opp.per_venue?.cryptocom || {};
    const slug = cryptoPv.slug || cryptoPv.ticker;

    if (!slug) {
      log('No slug found for crypto.com leg — cannot navigate', opp);
      isExecuting = false;
      badge('ERR', '#ef4444');
      return;
    }

    const pin = String(ccPin || conf.cryptoPin || '');
    const eventUrl = `${conf.appUrl.replace(/\/$/, '')}/cryptocom/${slug}`;
    const targetStrike = opp[cryptoSide === 'yes' ? 'yes_strike' : 'no_strike'] ?? opp.strike;
    const targetPrice = cryptoSide === 'yes' ? opp.yes_cost : opp.no_cost;
    const quantity = Number(conf.defaultQty) || 10;

    pendingTrade = {
      side: cryptoSide,
      strike: targetStrike,
      price: targetPrice,
      quantity,
      pin,
      opp,
    };

    log(`Opening tab → ${eventUrl}`, { side: cryptoSide, strike: targetStrike, quantity });

    // Open tab
    const tab = await chrome.tabs.create({ url: eventUrl, active: true });
    activeTabId = tab.id;

    // Wait for content script to signal ready, then send trade
    // (handled via chrome.runtime.onMessage)

  } catch (e) {
    log('executeArb error: ' + e.message);
    isExecuting = false;
    badge('ERR', '#ef4444');
  }
}

// ── Message handler (from content script) ─────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'CONTENT_READY') {
    if (sender.tab?.id === activeTabId && pendingTrade) {
      log('Content script ready — sending trade command', pendingTrade);
      chrome.tabs.sendMessage(activeTabId, {
        type: 'EXECUTE_TRADE',
        ...pendingTrade,
      });
    }
    sendResponse({ ok: true });
    return true;
  }

  if (msg.type === 'TRADE_RESULT') {
    const { success, error, details } = msg;
    if (success) {
      log('✅ Trade executed successfully', details);
      badge('✓', '#059669');
      chrome.notifications.create('result_' + Date.now(), {
        type: 'basic',
        iconUrl: 'icons/icon48.png',
        title: '✅ Trade executed!',
        message: `${pendingTrade?.opp?.coin} ${pendingTrade?.side?.toUpperCase()} · qty ${pendingTrade?.quantity}`,
        priority: 1,
      });
    } else {
      log('❌ Trade failed: ' + error, details);
      badge('ERR', '#ef4444');
      chrome.notifications.create('result_err_' + Date.now(), {
        type: 'basic',
        iconUrl: 'icons/icon48.png',
        title: '❌ Trade failed',
        message: String(error || 'Unknown error'),
        priority: 2,
      });
    }

    // Clean up — close tab after short delay
    setTimeout(() => {
      if (activeTabId) {
        chrome.tabs.remove(activeTabId).catch(() => {});
        activeTabId = null;
      }
      pendingTrade = null;
      isExecuting = false;
      setTimeout(() => badge('', ''), 5000);
    }, success ? 1500 : 4000);

    sendResponse({ ok: true });
    return true;
  }

  // Manual trigger from popup
  if (msg.type === 'MANUAL_TRIGGER') {
    poll();
    sendResponse({ ok: true });
    return true;
  }

  // Test trade (from popup "Test" button)
  if (msg.type === 'TEST_TRADE') {
    cfg().then(conf => {
      if (!conf.cryptoPin) {
        chrome.runtime.sendMessage({ type: 'TEST_RESULT', ok: false, error: 'No PIN configured' });
        return;
      }
      log('Test trade triggered manually', msg.opp);
      if (!isExecuting) {
        executeArb(msg.opp, conf);
        sendResponse({ ok: true });
      } else {
        sendResponse({ ok: false, error: 'Already executing' });
      }
    });
    return true;
  }
});

// ── Alarm setup ───────────────────────────────────────────────────────────

chrome.alarms.onAlarm.addListener(alarm => {
  if (alarm.name === POLL_ALARM) poll();
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: POLL_INTERVAL_MINUTES });
  log('Extension installed / updated — poll alarm set');
});

chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create(POLL_ALARM, { periodInMinutes: POLL_INTERVAL_MINUTES });
});

// Intercept tab loads for /cryptocom/<id> and redirect them directly
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url && changeInfo.url.includes('/cryptocom/')) {
    const match = changeInfo.url.match(/\/cryptocom\/([a-zA-Z0-9\-]+)/);
    if (match) {
      const id = match[1];
      const realUrl = `https://web.crypto.com/hub/predict/events/details/${id}`;
      log(`Intercepted /cryptocom/${id} -> redirecting to ${realUrl}`);
      chrome.tabs.update(tabId, { url: realUrl });
    }
  }
});

