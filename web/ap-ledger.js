/** Shared autopilot trade-ledger rendering (autopilot + bankroll pages).
 *  Works with rows from /api/autopilot/trades and the bankroll payload's
 *  executions list; injects its own stylesheet on first use. */
(function (global) {
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = v => (v == null || Number.isNaN(Number(v)) ? '—'
    : (Number(v) < 0 ? '-$' : '$') + Math.abs(Number(v)).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}));
  const pnlFmt = v => (v == null ? '—' : ((Number(v) >= 0 ? '+' : '') + money(v)));
  const pnlCls = v => (v == null ? '' : (Number(v) > 0 ? 'good' : (Number(v) < 0 ? 'bad' : '')));
  const VLABEL = { cryptocom: 'crypto.com', kalshi: 'Kalshi', polymarket: 'Polymarket' };

  const fmtET = iso => {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString('en-US', {timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
    } catch (e) { return String(iso); }
  };
  const fmtTs = ts => ts ? new Date(ts * 1000).toLocaleString(undefined, {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : '—';

  const CSS = `
.apl-list { display: flex; flex-direction: column; gap: 9px; max-height: 560px; overflow-y: auto; padding: 2px; }
.apl-card { border: 1px solid var(--border); border-radius: 12px; background: var(--surface); overflow: hidden; }
.apl-card.settled { background: var(--surface-2); }
.apl-card.failed { border-color: rgba(220,38,38,.28); }
.apl-h { display: flex; align-items: baseline; gap: 9px; padding: 9px 12px; flex-wrap: wrap; }
.apl-badge { font-size: 9px; font-weight: 800; text-transform: uppercase; letter-spacing: .4px; padding: 2.5px 8px; border-radius: 999px; }
.apl-badge.open { background: #dbeafe; color: #2563eb; }
.apl-badge.settled { background: var(--surface-2); color: var(--muted); border: 1px solid var(--border); }
.apl-badge.failed { background: #fee2e2; color: #dc2626; }
.apl-coin { font-size: 13px; font-weight: 900; letter-spacing: -.2px; }
.apl-meta { flex: 1; min-width: 140px; font-size: 11px; color: var(--muted-2); font-variant-numeric: tabular-nums; }
.apl-mode { font-size: 9px; font-weight: 800; text-transform: uppercase; padding: 2.5px 8px; border-radius: 999px; }
.apl-mode.live { background: #fffbeb; color: #b45309; border: 1px solid #fde68a; }
.apl-mode.paper { background: var(--surface-2); color: var(--muted); border: 1px solid var(--border); }
.apl-spread { font-size: 9px; font-weight: 800; padding: 2.5px 8px; border-radius: 999px; background: #d1fae5; color: #047857; }
.apl-pnl { margin-left: auto; font-size: 13px; font-weight: 900; font-variant-numeric: tabular-nums; }
.apl-pnl.good { color: #059669; } .apl-pnl.bad { color: #dc2626; }
.apl-legs { display: flex; flex-wrap: wrap; gap: 6px; padding: 0 12px 9px; }
.apl-leg { font-size: 10.5px; font-weight: 700; padding: 3px 9px; border-radius: 999px; background: var(--surface-2); border: 1px solid var(--border); color: var(--ink-2); }
.apl-leg.kalshi { color: #047857; } .apl-leg.polymarket { color: #6d28d9; } .apl-leg.cryptocom { color: #1199fa; }
.apl-leg.err { background: #fef2f2; border-color: rgba(220,38,38,.3); color: #dc2626; }
.apl-errs { padding: 7px 12px; font-size: 11px; color: #b91c1c; background: #fef2f2; border-top: 1px dashed rgba(220,38,38,.3); }
.apl-foot { padding: 7px 12px; font-size: 11px; color: var(--muted-2); border-top: 1px dashed var(--border); font-variant-numeric: tabular-nums; }
.apl-empty { padding: 24px; text-align: center; color: var(--muted-2); font-size: 12.5px; font-style: italic; }
.apl-tabs { display: flex; gap: 5px; flex-wrap: wrap; }
.apl-tab { font: inherit; font-size: 11px; font-weight: 700; padding: 4px 11px; border-radius: 999px; border: 1px solid var(--border-strong); background: var(--surface); color: var(--muted); cursor: pointer; }
.apl-tab.active { background: var(--dark); border-color: var(--dark); color: #fff; }
.apl-sum { font-size: 11.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
.apl-sum b { color: var(--ink); } .apl-sum .good { color: #059669; } .apl-sum .bad { color: #dc2626; }`;

  let cssDone = false;
  function ensureCss() {
    if (cssDone) return;
    const el = document.createElement('style');
    el.textContent = CSS;
    document.head.appendChild(el);
    cssDone = true;
  }

  /* Accept both /api/autopilot/trades rows and bankroll `executions` rows. */
  function norm(t) {
    const ok = t.ok !== false;
    let status = t.trade_status || t.status;
    if (!ok) status = 'failed';
    else if (status !== 'settled') status = 'open';
    return {
      id: t.arb_id || t.id,
      ts: t.entry_ts ?? t.ts,
      coin: t.coin,
      expiry: t.expiry,
      contracts: t.contracts,
      spread_cents: t.spread_cents ?? t.edge_cents,
      locked_pnl: t.locked_pnl,
      cost_total: t.cost_total,
      live_mode: !!t.live_mode,
      ok, status,
      pnl: t.pnl,
      errors: Array.isArray(t.errors) ? t.errors : [],
      legs: normLegs(t.legs),
    };
  }

  function normLegs(legs) {
    if (!legs) return [];
    const rows = [];
    const entries = Array.isArray(legs) ? legs.map(l => [l.key || l.venue || '', l]) : Object.entries(legs);
    for (const [key, leg] of entries) {
      if (!leg || typeof leg !== 'object') continue;
      const k = String(leg.venue || key);
      const venue = k.includes('kalshi') ? 'kalshi' : (k.includes('poly') ? 'polymarket' : 'cryptocom');
      const side = /yes/i.test(key) || leg.side === 'yes' ? 'YES' : (/no/i.test(key) || leg.side === 'no' ? 'NO' : '');
      rows.push({ venue, side, paper: !!leg.paper, error: leg.error });
    }
    return rows;
  }

  function card(t) {
    const metaBits = [fmtTs(t.ts)];
    if (t.expiry) metaBits.push('exp ' + fmtET(t.expiry));
    if (t.contracts != null) metaBits.push(t.contracts + '×');
    if (t.cost_total != null) metaBits.push('cost ' + money(t.cost_total));
    const legs = t.legs.map(l =>
      `<span class="apl-leg ${l.venue}${l.error ? ' err' : ''}" title="${esc(l.error || '')}">` +
      `${VLABEL[l.venue] || l.venue}${l.side ? ' · ' + l.side : ''}${l.error ? ' ⚠' : (l.paper ? ' · paper' : '')}</span>`
    ).join('');
    let pnlHtml;
    if (t.status === 'failed') pnlHtml = '<span class="apl-pnl bad">failed</span>';
    else if (t.status === 'settled') pnlHtml = `<span class="apl-pnl ${pnlCls(t.pnl)}">${pnlFmt(t.pnl)}</span>`;
    else pnlHtml = `<span class="apl-pnl good">${pnlFmt(t.locked_pnl)} pending</span>`;
    const foot = t.status === 'open' && t.locked_pnl != null
      ? `<div class="apl-foot">Both legs held to expiry — spread locked at entry pays <b>${pnlFmt(t.locked_pnl)}</b> if venue rules align.</div>`
      : '';
    return `<article class="apl-card ${t.status}">
      <div class="apl-h">
        <span class="apl-badge ${t.status}">${t.status}</span>
        <b class="apl-coin">${esc(t.coin || '—')}</b>
        ${t.spread_cents != null ? `<span class="apl-spread">${Number(t.spread_cents).toFixed(2)}¢ edge</span>` : ''}
        <span class="apl-mode ${t.live_mode ? 'live' : 'paper'}">${t.live_mode ? 'live' : 'paper'}</span>
        <span class="apl-meta">${metaBits.join(' · ')}</span>
        ${pnlHtml}
      </div>
      ${legs ? `<div class="apl-legs">${legs}</div>` : ''}
      ${t.errors.length ? `<div class="apl-errs">${esc(t.errors.join(' · ')).slice(0, 400)}</div>` : ''}
      ${foot}
    </article>`;
  }

  /* listEl: container; trades: raw rows; filter: all|open|settled|failed */
  function renderList(listEl, trades, filter) {
    ensureCss();
    const rows = (trades || []).map(norm)
      .filter(t => filter === 'all' || !filter || t.status === filter)
      .sort((a, b) => (b.ts || 0) - (a.ts || 0));
    listEl.innerHTML = rows.length ? rows.map(card).join('')
      : `<div class="apl-empty">No ${filter && filter !== 'all' ? filter + ' ' : ''}trades yet — fills appear here as the runner executes.</div>`;
    return rows.length;
  }

  function summaryHtml(trades, stats) {
    const rows = (trades || []).map(norm);
    const open = rows.filter(t => t.status === 'open');
    const settled = rows.filter(t => t.status === 'settled');
    const failed = rows.filter(t => t.status === 'failed');
    const locked = stats?.pending_locked_usd ?? open.reduce((s, t) => s + (Number(t.locked_pnl) || 0), 0);
    const realized = stats?.realized_pnl_usd ?? settled.reduce((s, t) => s + (Number(t.pnl) || 0), 0);
    return `<b>${open.length}</b> open · <b>${settled.length}</b> settled · <b>${failed.length}</b> failed` +
      ` · pending <b class="good">${pnlFmt(locked)}</b> · realized <b class="${pnlCls(realized)}">${pnlFmt(realized)}</b>`;
  }

  function tabsHtml(filter) {
    return ['all', 'open', 'settled', 'failed'].map(f =>
      `<button type="button" class="apl-tab${f === (filter || 'all') ? ' active' : ''}" data-filter="${f}">${f[0].toUpperCase() + f.slice(1)}</button>`
    ).join('');
  }

  global.ApLedger = { renderList, summaryHtml, tabsHtml, money, pnlFmt, pnlCls, fmtET, esc };
})(typeof window !== 'undefined' ? window : globalThis);
