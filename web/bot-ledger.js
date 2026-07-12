/** Shared trade-ledger rendering for paper bots. */
(function (global) {
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const money = v => (v < 0 ? '-$' : '$') + Math.abs(Number(v) || 0).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const pnlFmt = v => (v == null ? '—' : ((v >= 0 ? '+' : '') + money(v)));
  const pnlCls = v => (v == null ? '' : (v > 0 ? 'good' : (v < 0 ? 'bad' : '')));
  const fmtET = iso => {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleString('en-US', {timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}); }
    catch (e) { return iso; }
  };
  const fmtDur = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
    return h ? h + 'h ' + m + 'm' : (m ? m + 'm ' + ss + 's' : ss + 's'); };
  const strk = v => v >= 100 ? '$' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 0})
    : '$' + Number(v).toLocaleString(undefined, {maximumFractionDigits: 4});
  const VLABEL = {cryptocom: 'crypto.com', kalshi: 'Kalshi', polymarket: 'Polymarket'};
  const ccEventUrl = slug => slug ? 'https://web.crypto.com/explore/predict/events/' + encodeURIComponent(slug) : null;
  const venueLink = (v, pv, cls) => {
    const c = cls ? ' ' + cls : '';
    if (!pv) return '<span class="v-cost' + c + ' ' + v + '">' + (VLABEL[v] || v) + '</span>';
    if (v === 'polymarket' && pv.slug) return '<a href="https://polymarket.com/event/' + esc(pv.slug) + '" target="_blank" rel="noopener" class="pv-link v-cost' + c + ' ' + v + '">' + VLABEL[v] + ' ↗</a>';
    if (v === 'kalshi' && pv.ticker) return '<a href="https://kalshi.com/markets/' + esc(pv.ticker.toLowerCase()) + '" target="_blank" rel="noopener" class="pv-link v-cost' + c + ' ' + v + '">' + VLABEL[v] + ' ↗</a>';
    if (v === 'cryptocom' && pv.slug) return '<a href="' + ccEventUrl(pv.slug) + '" target="_blank" rel="noopener" class="pv-link v-cost' + c + ' ' + v + '">' + VLABEL[v] + ' ↗</a>';
    return '<span class="v-cost' + c + ' ' + v + '">' + (VLABEL[v] || v) + '</span>';
  };
  const countdownRing = (secs, total) => {
    if (secs == null) return '';
    total = total || Math.max(secs, 300);
    const pct = Math.max(0, Math.min(1, secs / total));
    const circ = 2 * Math.PI * 9;
    const off = circ * (1 - pct);
    const cls = secs <= 30 ? ' critical' : (secs <= 120 ? ' urgent' : '');
    return '<span class="ca-countdown"><svg class="ca-ring' + cls + '" viewBox="0 0 24 24"><circle class="bg" cx="12" cy="12" r="9"/><circle class="fg" cx="12" cy="12" r="9" stroke-dasharray="' + circ.toFixed(1) + '" stroke-dashoffset="' + off.toFixed(1) + '"/></svg><span>' + fmtDur(secs) + '</span></span>';
  };

  function strategyLabel(id, strategies) {
    return (strategies || {})[id]?.label || id || '';
  }

  function pairedOutcomesHtml(t) {
    const legs = t.legs || [];
    const yes = legs.find(l => l.side === 'yes'), no = legs.find(l => l.side === 'no');
    if (!yes || !no) return '';
    const ifYes = (yes.pnl_if_win || 0) + (no.pnl_if_lose || 0);
    const ifNo = (no.pnl_if_win || 0) + (yes.pnl_if_lose || 0);
    return '<div class="tl-pair-outcomes">' +
      '<div>If <b>YES</b> pays: YES ' + pnlFmt(yes.pnl_if_win) + ' + NO ' + pnlFmt(no.pnl_if_lose) + ' = <span class="locked">' + pnlFmt(ifYes) + '</span></div>' +
      '<div>If <b>NO</b> pays: NO ' + pnlFmt(no.pnl_if_win) + ' + YES ' + pnlFmt(yes.pnl_if_lose) + ' = <span class="locked">' + pnlFmt(ifNo) + '</span></div>' +
      '<div class="locked">Pending payout estimate if venue rules align: ' + pnlFmt(t.locked_pnl) + '</div></div>';
  }

  function renderTradeCard(t, strategies) {
    const open = t.status === 'open';
    const legs = t.legs || [];
    const tid = String(t.id).replace(/[|]/g, '-');
    const total = (t.settles_in_s != null && t.settles_in_s <= 300) ? 300 : 900;
    const headPnl = open
      ? '<span class="tl-pnl lock">' + money(t.locked_pnl) + ' pending estimate</span>'
      : '<span class="tl-pnl ' + pnlCls(t.pnl) + '">' + pnlFmt(t.pnl) + '</span>';
    const meta = open
      ? fmtET(t.expiry) + ' · ' + (t.settlement_status === 'awaiting_price' ? '<b>expired — awaiting price</b>' : countdownRing(t.settles_in_s, total)) + ' · ' + t.contracts + '× · cost ' + money(t.cost_total)
      : fmtET(t.expiry) + ' · ' + t.contracts + '× · cost ' + money(t.cost_total) + (t.life ? ' · L' + t.life : '');
    const legRows = legs.map(leg => {
      let resultHtml, pnlHtml;
      if (open) {
        resultHtml = '<span class="tl-result proj">pending</span>';
        pnlHtml = '<span class="tl-result proj">win ' + pnlFmt(leg.pnl_if_win) + '<br>lose ' + pnlFmt(leg.pnl_if_lose) + '</span>';
      } else {
        const won = !!leg.won;
        resultHtml = '<span class="tl-result ' + (won ? 'won' : 'lost') + '">' + (won ? 'WON' : 'LOST') + '</span>';
        pnlHtml = '<span class="tl-pnl ' + pnlCls(leg.pnl) + '">' + pnlFmt(leg.pnl) + '</span>';
      }
      const strikeSource = leg.venue_detail?.strike_source || 'venue contract';
      const operator = leg.side === 'yes' ? leg.venue_detail?.yes_operator : leg.venue_detail?.no_operator;
      const timing = leg.venue_detail?.strike_delay_ms == null ? '' : ' · T0+' + Number(leg.venue_detail.strike_delay_ms).toLocaleString() + 'ms';
      const strikeMark = leg.venue_detail?.strike_verified && leg.venue_detail?.settlement_rule_verified
        ? '<span class="tl-strike-note ok">✓ ' + esc(strikeSource) + timing + ' · ' + esc(operator) + '</span>'
        : '<span class="tl-strike-note bad">unverified</span>';
      return '<div class="tl-leg-row">' +
        '<div class="tl-leg-cell"><span class="tl-side ' + leg.side + '">' + (leg.side || '').toUpperCase() + '</span></div>' +
        '<div class="tl-leg-cell">' + venueLink(leg.venue, leg.venue_detail || {}, 'tl-venue') + '</div>' +
        '<div class="tl-leg-cell r" title="' + esc(leg.venue_detail?.strike_evidence || '') + '"><span class="tl-strike">' + strk(leg.strike) + '</span>' + strikeMark + '</div>' +
        '<div class="tl-leg-cell r"><span class="v-cost ' + leg.venue + '">' + money(leg.cost_total) + '</span><span class="tl-cost-sub">' + leg.contracts + '× @ $' + Number(leg.cost_per).toFixed(2) + '</span></div>' +
        '<div class="tl-leg-cell">' + resultHtml + '</div>' +
        '<div class="tl-leg-cell r">' + pnlHtml + '</div></div>';
    }).join('');
    const legsBlock = legs.length
      ? '<div class="tl-legs-grid"><div class="tl-leg-row head"><div>Leg</div><div>Venue</div><div class="r">Strike</div><div class="r">Cost</div><div>Result</div><div class="r">P&amp;L</div></div>' + legRows + '</div>'
      : '<div class="tl-legs-empty">Leg details unavailable for this trade.</div>';
    let foot = '';
    if (open) {
      const yesLeg = legs.find(l => l.side === 'yes'), noLeg = legs.find(l => l.side === 'no');
      const yesOp = yesLeg?.venue_detail?.yes_operator, noOp = noLeg?.venue_detail?.no_operator;
      const exact = yesLeg?.venue_detail?.strike_verified && noLeg?.venue_detail?.strike_verified &&
        yesLeg?.venue_detail?.settlement_rule_verified && noLeg?.venue_detail?.settlement_rule_verified &&
        yesLeg.strike === noLeg.strike && !(yesOp === '>' && noOp === '<');
      foot = '<div class="tl-foot"><b>' + (exact ? '✓ Exact strikes and payout coverage verified' : '⚠ Contract verification failed') + '</b> · No success or P&amp;L is recorded before expiry.</div>' + pairedOutcomesHtml(t);
    }
    else {
      const spot = t.spot_price != null ? '$' + Number(t.spot_price).toLocaleString(undefined, {minimumFractionDigits: 2}) : '—';
      const yesPnl = legs.find(l => l.side === 'yes')?.pnl, noPnl = legs.find(l => l.side === 'no')?.pnl;
      foot = '<div class="tl-foot"><b>Pair P&amp;L ' + pnlFmt(t.pnl) + '</b> · YES ' + pnlFmt(yesPnl) + ' + NO ' + pnlFmt(noPnl) +
        ' · Spot <b>' + spot + '</b> · <b>' + esc(t.winning_leg || '—') + '</b> pays</div>';
    }
    const stratBadge = t.strategy ? '<span class="tl-strategy">' + esc(strategyLabel(t.strategy, strategies)) + '</span>' : '';
    const spreadBadge = t.spread_cents != null
      ? '<span class="tl-spread">' + Number(t.spread_cents).toFixed(2) + '¢ spread</span>'
      : '';
    return '<article class="tl-card ' + t.status + '" id="trade-' + tid + '">' +
      '<div class="tl-card-h"><span class="tl-badge ' + t.status + '">' + t.status + '</span>' +
      '<span class="tl-coin">' + esc(t.coin) + ' &gt; ' + strk(t.strike) + '</span>' + stratBadge + spreadBadge +
      '<span class="tl-meta">' + meta + '</span>' + headPnl + '</div>' +
      legsBlock + foot + '</article>';
  }

  function renderEventCard(e) {
    const t = new Date(e.ts * 1000).toLocaleString();
    if (e.kind === 'bust') {
      return '<article class="tl-card event"><div class="tl-card-h"><span class="tl-badge" style="background:#dc26261a;color:#dc2626">BUST</span>' +
        '<span class="tl-coin">Life #' + e.life + ' ended</span><span class="tl-meta">' + t + '</span>' +
        '<span class="tl-pnl bad">' + money(e.final_cash) + ' left</span></div>' +
        '<div class="tl-foot">Realized <b>' + money(e.realized) + '</b> this life · ' + e.settled + ' trades settled (' + e.wins + ' wins). Bankroll reloaded with $50.</div></article>';
    }
    return '<article class="tl-card event"><div class="tl-card-h"><span class="tl-badge" style="background:#f59e0b1a;color:#f59e0b">RELOAD</span>' +
      '<span class="tl-coin">Life #' + e.life + ' started</span><span class="tl-meta">' + t + '</span>' +
      '<span class="tl-pnl">+$' + Number(e.amount || 50).toFixed(0) + '</span></div>' +
      '<div class="tl-foot">Total injected: <b>' + money(e.total_injected) + '</b></div></article>';
  }

  function sortTrades(trades, sort, filter) {
    let arr = trades.filter(t => filter === 'all' || t.status === filter);
    if (sort === 'locked') return arr.sort((a, b) => (b.locked_pnl || 0) - (a.locked_pnl || 0));
    if (sort === 'realized') return arr.sort((a, b) => (b.pnl || 0) - (a.pnl || 0));
    return arr.sort((a, b) => {
      const ao = a.status === 'open' ? (a.settles_in_s ?? 1e9) : 1e9;
      const bo = b.status === 'open' ? (b.settles_in_s ?? 1e9) : 1e9;
      if (ao !== bo) return ao - bo;
      return (b.settled_at || 0) - (a.settled_at || 0);
    });
  }

  function renderTradeLedger(root, trades, meta, state) {
    state = state || {filter: 'all', sort: 'expiry'};
    const filtered = sortTrades(trades || [], state.sort, state.filter);
    const events = (meta.log || []).filter(e => e.kind === 'bust' || e.kind === 'reload');
    const cards = filtered.map(t => renderTradeCard(t, meta.strategies));
    if (state.filter === 'all') events.forEach(e => cards.push(renderEventCard(e)));
    const listEl = root.querySelector('.tl-list');
    const sumEl = root.querySelector('.tl-sum');
    if (listEl) {
      listEl.innerHTML = cards.length ? cards.join('')
        : '<div class="tl-empty">No ' + (state.filter === 'all' ? '' : state.filter + ' ') + 'trades yet.</div>';
    }
    const open = (trades || []).filter(t => t.status === 'open');
    const settled = (trades || []).filter(t => t.status === 'settled');
    const settledPnl = settled.reduce((s, t) => s + (t.pnl || 0), 0);
    const openLocked = open.reduce((s, t) => s + (t.locked_pnl || 0), 0);
    if (sumEl) {
      sumEl.innerHTML = open.length + ' open · ' + settled.length + ' settled · locked <b class="good">' + money(openLocked) + '</b> · realized <b class="' + pnlCls(settledPnl) + '">' + pnlFmt(settledPnl) + '</b>';
    }
    root.querySelectorAll('.tl-tab').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.filter === state.filter);
    });
    if (root.querySelector('.tl-sort')) root.querySelector('.tl-sort').value = state.sort;
  }

  function drawSparkline(canvas, curve) {
    if (!canvas || !curve || curve.length < 2) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    const vals = curve.map(p => p.equity);
    const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.1 || 1;
    const min = lo - pad, max = hi + pad;
    ctx.strokeStyle = '#16a34a'; ctx.lineWidth = 1.5; ctx.beginPath();
    curve.forEach((p, i) => {
      const x = (i / (curve.length - 1)) * w;
      const y = h - ((p.equity - min) / (max - min)) * h;
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
  }

  global.BotLedger = {
    esc, money, pnlFmt, pnlCls, fmtET, fmtDur, strk, strategyLabel,
    renderTradeCard, renderTradeLedger, drawSparkline, sortTrades,
  };
})(typeof window !== 'undefined' ? window : globalThis);
