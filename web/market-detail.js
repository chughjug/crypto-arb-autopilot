/** Oddpool-style market: event overview → outcome detail. */
(function (global) {
  const $ = s => document.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const TOP_N = 10;
  const CHART_COLORS = ["#2563eb", "#059669", "#ea580c", "#7c3aed", "#db2777", "#0891b2", "#ca8a04", "#4f46e5", "#65a30d", "#9333ea"];
  const HOURS = { "24H": 24, "3D": 72, "7D": 168, "30D": 720 };
  const VENUES = {
    kalshi: { name: "Kalshi", short: "K", logo: "https://upload.wikimedia.org/wikipedia/commons/thumb/e/ee/Kalshi_logo.svg/1280px-Kalshi_logo.svg.png" },
    polymarket: { name: "Polymarket", short: "P", logo: "https://images.cryptorank.io/coins/polymarket1671006384460.png" },
    cryptocom: { name: "Crypto.com", short: "C", logo: "https://s2.coinmarketcap.com/static/img/coins/64x64/3635.png" },
  };
  const vMeta = v => VENUES[v] || VENUES.polymarket;

  let state = {
    venue: "", id: "", outcome: "", hours: 72, data: null,
    chartSlugs: [], chartSeries: [], overviewHours: 72,
  };
  let loadGen = 0;

  function isDetail() { return !!state.outcome; }
  function outcomeFromUrl() { return new URLSearchParams(location.search).get("outcome") || ""; }

  function setOutcome(slug) {
    const p = new URLSearchParams(location.search);
    if (slug) p.set("outcome", slug); else p.delete("outcome");
    history.pushState(null, "", p.toString() ? "?" + p.toString() : location.pathname);
    state.outcome = slug;
  }

  function fmtUsd(n) {
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return "$" + (n / 1e3).toFixed(1) + "K";
    return "$" + (Math.round((n || 0) * 10) / 10).toLocaleString();
  }

  function ago(ts) {
    if (!ts) return "—";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.round(s) + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  function hostFromUrl(url) {
    try {
      return new URL(url).hostname.replace(/^www\./, "");
    } catch (e) {
      return "";
    }
  }

  function sourceInitials(source) {
    const parts = String(source || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2);
    const initials = parts.map(part => part[0]).join("").toUpperCase();
    return initials || "N";
  }

  function sourceLogoUrl(article) {
    const host = hostFromUrl(article.source_url || article.link || "");
    return host ? `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=64` : "";
  }

  async function fetchAndRenderNews(query, elId) {
    try {
      const res = await fetch(`/api/news?q=${encodeURIComponent(query)}&limit=4`);
      const d = await res.json();
      const articles = d.news || [];
      const el = document.getElementById(elId);
      if (!articles.length || !el) return;
      const cards = articles.map(a => `
        <a class="news-card" href="${esc(a.link)}" target="_blank" rel="noopener" style="display:block;background:linear-gradient(180deg, color-mix(in srgb, var(--surface) 94%, white 6%), var(--surface));border:1px solid var(--border);border-radius:var(--radius-md);padding:14px 16px;text-decoration:none;color:inherit;transition:transform 0.1s, box-shadow 0.1s;box-shadow:0 1px 0 rgba(0,0,0,.02);">
          <div style="display:flex;gap:12px;align-items:flex-start;">
            <div class="news-source-mark" style="width:42px;height:42px;border-radius:14px;background:var(--surface-2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;flex-shrink:0;overflow:hidden;position:relative;">
              <img src="${esc(sourceLogoUrl(a))}" alt="" aria-hidden="true" loading="lazy" referrerpolicy="no-referrer" style="width:100%;height:100%;object-fit:cover;display:block;">
              <span style="font-size:12px;font-weight:900;letter-spacing:0.08em;color:var(--muted-2);display:none;align-items:center;justify-content:center;width:100%;height:100%;">${esc(sourceInitials(a.source))}</span>
            </div>
            <div class="nc-body" style="display:flex;flex-direction:column;gap:6px;min-width:0;flex:1;">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;min-width:0;">
                <div style="display:flex;align-items:center;gap:8px;min-width:0;">
                  <div class="nc-league" style="font-size:10px;font-weight:800;text-transform:uppercase;letter-spacing:0.5px;color:var(--muted-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(a.source)}</div>
                </div>
                <div style="font-size:10px;font-weight:700;color:var(--muted-2);white-space:nowrap;">${esc(ago(a.published ? Date.parse(a.published) / 1000 : 0))}</div>
              </div>
              <h4 style="margin:0;font-size:13.5px;font-weight:700;line-height:1.35;color:var(--ink);display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;">${esc(a.headline)}</h4>
            </div>
          </div>
        </a>`).join("");
      el.innerHTML = `
        <h3 style="margin: 24px 0 12px; font-size: 16px; font-weight: 800;">Related News</h3>
        <div class="news-grid" style="display:grid;grid-template-columns:repeat(auto-fill, minmax(280px, 1fr));gap:14px;margin-bottom:24px;">
          ${cards}
        </div>`;
      el.querySelectorAll('.news-source-mark img').forEach(img => {
        const mark = img.parentElement;
        const fallback = mark ? mark.querySelector('span') : null;
        img.addEventListener('error', () => {
          img.style.display = 'none';
          if (fallback) fallback.style.display = 'flex';
        });
        if (!img.getAttribute('src')) {
          img.style.display = 'none';
          if (fallback) fallback.style.display = 'flex';
        }
      });
    } catch (e) {}
  }

  function topMarkets(markets) {
    return [...(markets || [])]
      .sort((a, b) => (b.yes ?? -1) - (a.yes ?? -1))
      .slice(0, TOP_N);
  }

  function pickOutcome(markets) {
    if (!state.outcome) return null;
    const ol = state.outcome.toLowerCase();
    for (const m of markets) {
      if (m.slug === ol || (m.label || "").toLowerCase() === ol) return m;
    }
    return null;
  }

  function slugForLabel(markets, label) {
    const m = markets.find(x => x.label === label);
    return m?.slug || "";
  }

  function colorForSlug(slug, markets) {
    const top = topMarkets(markets);
    const i = top.findIndex(m => m.slug === slug);
    return CHART_COLORS[i >= 0 ? i % CHART_COLORS.length : 0];
  }

  function drawMultiChart(el, series, hours) {
    if (!el) return;
    if (!series?.length) {
      if (el.__chartSig !== "empty") { el.innerHTML = `<div class="chart-empty">No price history available</div>`; el.__chartSig = "empty"; }
      return;
    }
    const cutoff = Date.now() / 1000 - hours * 3600;
    const filtered = series.map(s => ({
      label: s.label,
      slug: s.slug,
      color: s.color,
      pts: (s.points || []).filter(p => p.t >= cutoff),
    })).filter(s => s.pts.length >= 2);
    if (!filtered.length) {
      if (el.__chartSig !== "insufficient") { el.innerHTML = `<div class="chart-empty">Not enough history in this range</div>`; el.__chartSig = "insufficient"; }
      return;
    }
    // Skip redraw when nothing meaningful changed — avoids the chart flickering every poll.
    const sig = hours + "|" + filtered.map(s => {
      const last = s.pts[s.pts.length - 1];
      return `${s.label}:${s.color}:${s.pts.length}:${last.t}:${last.p}`;
    }).join(",");
    if (el.__chartSig === sig) return;
    el.__chartSig = sig;
    const W = 800, H = 240, padL = 36, padR = 12, padT = 12, padB = 24;
    const allT = filtered.flatMap(s => s.pts.map(p => p.t));
    const allP = filtered.flatMap(s => s.pts.map(p => p.p));
    const xMin = Math.min(...allT), xMax = Math.max(...allT);
    const yMax = Math.min(100, Math.max(10, Math.ceil(Math.max(...allP) * 1.15)));
    const X = t => padL + (xMax === xMin ? 0 : (t - xMin) / (xMax - xMin)) * (W - padL - padR);
    const Y = p => padT + (1 - p / yMax) * (H - padT - padB);
    const grid = [0, 25, 50, 75, 100].filter(v => v <= yMax).map(v =>
      `<line class="grid-line" x1="${padL}" y1="${Y(v)}" x2="${W - padR}" y2="${Y(v)}"/>
       <text class="axis" x="${padL - 6}" y="${Y(v) + 3}" text-anchor="end">${v}%</text>`).join("");
    const fmtD = t => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    const xticks = [xMin, xMax].map(t =>
      `<text class="axis" x="${X(t)}" y="${H - 6}" text-anchor="middle">${fmtD(t)}</text>`).join("");
    const lines = filtered.map(s => {
      const path = s.pts.map((p, i) => `${i ? "L" : "M"}${X(p.t).toFixed(1)} ${Y(p.p).toFixed(1)}`).join(" ");
      return `<path d="${path}" fill="none" stroke="${s.color}" stroke-width="2"/>`;
    }).join("");
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${grid}${lines}${xticks}</svg>`;
  }

  function drawSingleChart(el, points, hours) {
    if (!el || !points?.length) {
      if (el) el.innerHTML = `<div class="chart-empty">No price history available</div>`;
      return;
    }
    const cutoff = Date.now() / 1000 - hours * 3600;
    const pts = points.filter(p => p.t >= cutoff);
    if (pts.length < 2) {
      el.innerHTML = `<div class="chart-empty">Not enough history in this range</div>`;
      return;
    }
    drawMultiChart(el, [{ label: "", points: pts, color: "#2563eb" }], hours);
  }

  // Removed chipBar

  function fmtSize(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
    return String(Math.round(n * 10) / 10);
  }

  function orderBookHtml(ob, label, venue) {
    const vLabel = vMeta(venue).name;
    if (ob === null) return `<div class="panel ob-panel"><div class="ob-panel-head"><h3>Order Book</h3></div><div class="ob-body"><p class="ob-meta">Loading…</p></div></div>`;
    const asks = (ob.asks || []).slice().reverse().slice(0, 8);
    const bids = (ob.bids || (!ob.asks ? ob.levels : []) || []).slice(0, 8);
    const maxTotal = Math.max(1, ...asks.map(l => l.total_usd || 0), ...bids.map(l => l.total_usd || 0));
    const askRows = asks.map(l => {
      const w = Math.round(100 * (l.total_usd || 0) / maxTotal);
      return `<div class="ob-row ask"><span class="r">${l.price_cents}¢</span><span class="r">${fmtSize(l.size)}</span>
        <span class="r ob-total"><span class="ob-bar" style="width:${w}%"></span>${fmtUsd(l.total_usd || 0)}</span></div>`;
    }).join("");
    const bidRows = bids.map(l => {
      const w = Math.round(100 * (l.total_usd || 0) / maxTotal);
      return `<div class="ob-row bid"><span class="g">${l.price_cents}¢</span><span class="g">${fmtSize(l.size)}</span>
        <span class="g ob-total"><span class="ob-bar bid-bar" style="width:${w}%"></span>${fmtUsd(l.total_usd || 0)}</span></div>`;
    }).join("");
    const meta = `Last: ${ob.last != null ? ob.last + "¢" : "—"} · Spread: ${ob.spread != null ? ob.spread + "¢" : "—"}`;
    return `<div class="panel ob-panel">
      <div class="ob-panel-head"><h3>Order Book</h3><span class="ob-meta">${esc(label)} · YES · ${vLabel}</span></div>
      <div class="ob-body">
        <div class="ob-ladder">
          <div class="ob-row hdr"><span>Price</span><span>Size</span><span>Total</span></div>
          ${askRows || `<div class="ob-row"><span class="r" style="grid-column:1/-1;text-align:center;color:var(--muted)">No asks</span></div>`}
          <div class="ob-mid">${meta}</div>
          ${bidRows || `<div class="ob-row"><span class="g" style="grid-column:1/-1;text-align:center;color:var(--muted)">No bids</span></div>`}
        </div>
      </div>
    </div>`;
  }

  function outcomeList(markets, selectedSlug) {
    const top = topMarkets(markets);
    const rows = top.map((m, i) => {
      const col = CHART_COLORS[i % CHART_COLORS.length];
      const pct = m.yes != null ? m.yes : 0;
      const active = m.slug === selectedSlug ? " active" : "";
      const img = m.image ? `<img src="${esc(m.image)}" class="out-img" />` : `<div class="out-img"></div>`;
      return `<tr class="out-row${active}" data-slug="${esc(m.slug)}">
        <td><div class="out-name-cell">${img} ${esc(m.label)}</div></td>
        <td><div class="out-bar-wrap"><div class="out-bar" style="width:${pct}%;background:${col}"></div></div></td>
        <td class="out-pct" style="color:${col}">${pct}%</td>
      </tr>`;
    }).join("");
    return `<div class="out-table-wrap">
      <table class="out-table">
        <thead><tr><th>Outcome</th><th>Probability</th><th>Price</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }

  function whaleSection(whales, minUsd) {
    const rows = (whales || []).slice(0, TOP_N);
    const total = rows.reduce((a, x) => a + (x.total_usd || 0), 0);
    const trades = rows.reduce((a, x) => a + (x.trade_count || 0), 0);
    if (whales === null) {
      return `<div class="panel panel-pad whale-panel"><p class="whale-empty">Loading whale activity…</p></div>`;
    }
    if (!total && !trades) {
      return `<div class="panel panel-pad whale-panel">
        <div class="whale-head"><div><h2>Whale Activity by Outcome</h2>
          <div class="meta">YES → / ← NO · last 24h · trades ≥ ${fmtUsd(minUsd)}</div></div>
          <div class="total"><b>$0</b><span>0 whale trades</span></div></div>
        <div class="whale-empty">No whale activity<br><small>Nothing above ${fmtUsd(minUsd)} traded in the last 24h on any leading outcome</small></div>
      </div>`;
    }
    const body = rows.map(o => {
      const yesW = o.yes_usd || 0, noW = o.no_usd || 0, t = yesW + noW || 1;
      const yesPct = Math.round(100 * yesW / t);
      return `<div class="whale-row" data-slug="${esc(o.slug)}">
        <span>${esc(o.label)}</span>
        <div class="whale-bar"><span class="y" style="width:${yesPct}%"></span><span class="n" style="width:${100 - yesPct}%"></span></div>
        <span style="text-align:right;font-weight:700">${fmtUsd(o.total_usd)}</span>
      </div>`;
    }).join("");
    return `<div class="panel panel-pad whale-panel">
      <div class="whale-head"><div><h2>Whale Activity by Outcome</h2>
        <div class="meta">YES → / ← NO · last 24h · trades ≥ ${fmtUsd(minUsd)}</div></div>
        <div class="total"><b>${fmtUsd(total)}</b><span>${trades} whale trades</span></div></div>
      <div class="whale-rows">${body}</div>
    </div>`;
  }

  function whaleTickerHtml(data) {
    const items = [];
    const vLabel = vMeta(data.event?.venue).short;
    const isD = isDetail();

    if (isD && data.whale_trades?.length) {
      const label = data.outcome?.label || "";
      for (const t of data.whale_trades.slice(0, 20)) {
        items.push({
          side: t.side,
          label: label,
          amount: fmtUsd(t.usd),
          venue: vLabel,
          time: ago(t.ts),
        });
      }
    } else if (!isD && data.event_whales?.length) {
      for (const w of data.event_whales) {
        if (!w.total_usd || w.total_usd < 1) continue;
        const dominant = (w.yes_usd || 0) >= (w.no_usd || 0) ? "yes" : "no";
        items.push({
          side: dominant,
          label: w.label || "",
          amount: fmtUsd(w.total_usd),
          venue: vLabel,
          time: `${w.trade_count || 0} trades`,
        });
      }
    }

    if (!items.length) {
      return `<div class="whale-ticker">
        <div class="whale-ticker-label"><span class="whale-icon">⚡</span> Recent activity</div>
        <div class="whale-ticker-empty">No recent activity in the last 24h</div>
      </div>`;
    }

    const renderItem = (it) =>
      `<div class="ticker-item">
        <span class="ticker-dot ${it.side}"></span>
        <span class="ticker-side ${it.side}">${it.side.toUpperCase()}</span>
        <span class="ticker-question">${esc(it.label)}</span>
        <span class="ticker-amount">${it.amount}</span>
        <span class="ticker-venue">${it.venue}</span>
        <span class="ticker-time">${it.time}</span>
      </div>`;

    const strip = items.map(renderItem).join("");
    const dur = Math.max(40, items.length * 10);

    return `<div class="whale-ticker">
      <div class="whale-ticker-label"><span class="whale-icon">⚡</span> Recent activity</div>
      <div class="whale-ticker-track">
        <div class="whale-ticker-inner" style="--ticker-dur:${dur}s">
          ${strip}${strip}
        </div>
      </div>
    </div>`;
  }

  function rangeButtons(activeHours, id) {
    return Object.entries(HOURS).map(([lab, h]) =>
      `<button type="button" data-h="${h}" class="${activeHours === h ? "on" : ""}">${lab}</button>`).join("");
  }

  function tradesHtml(trades, title) {
    if (trades === null) return `<div class="panel panel-pad"><h3>${title}</h3><p class="ob-meta">Loading…</p></div>`;
    if (!trades?.length) return `<div class="panel panel-pad"><h3>${title}</h3><p class="ob-meta" style="padding:12px 0">No recent trades</p></div>`;
    const rows = trades.slice(0, 12).map(t =>
      `<div class="row"><span><b>${fmtUsd(t.usd)}</b> <span class="${t.side}">${t.side.toUpperCase()}</span> @ ${t.price_cents}¢</span><span style="color:var(--muted)">${ago(t.ts)}</span></div>`).join("");
    return `<div class="panel panel-pad"><h3>${title}</h3><div class="trade-mini">${rows}</div></div>`;
  }

  function sentimentHtml(w, oc, venue) {
    const vLabel = vMeta(venue).name;
    if (!w?.trade_count) {
      return `<div class="panel panel-pad sent-panel"><h3>Whale Sentiment <span style="font-weight:600;color:var(--muted)">24H</span></h3>
        <p class="sub">${esc(oc.label)} · ${vLabel} · $500+ trades</p>
        <div class="sent-empty">No whale trades in the last 24 hours</div></div>`;
    }
    const sent = w.sentiment === "bullish" ? "Bullish" : w.sentiment === "bearish" ? "Bearish" : "Mixed";
    return `<div class="panel panel-pad sent-panel"><h3>Whale Sentiment <span style="font-weight:600;color:var(--muted)">24H</span> · ${sent}</h3>
      <p class="sub">${esc(oc.label)} · ${vLabel} · ≥${fmtUsd(500)} · ${w.trade_count} trades · ${fmtUsd(w.total_usd)}</p>
      <div class="whale-bar" style="height:8px;margin-top:8px"><span class="y" style="width:${w.yes_pct}%"></span><span class="n" style="width:${w.no_pct}%"></span></div>
      <p class="sub" style="margin-top:8px">YES ${fmtUsd(w.yes_usd)} · NO ${fmtUsd(w.no_usd)}</p></div>`;
  }

  function formatExpirationDate(dateStr) {
    if (!dateStr) return "";
    try {
      const d = new Date(dateStr);
      if (isNaN(d.getTime())) return "";
      return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
    } catch (e) {
      return "";
    }
  }

  function headerHtml(ev, oc) {
    const vLabel = vMeta(ev.venue).name;
    const top = topMarkets(ev.markets);
    const lead = top[0];
    const leadPct = lead?.yes != null ? lead.yes + "%" : "—";
    const imgHtml = ev.image ? `<img src="${esc(ev.image)}" class="mkt-hdr-img" />` : `<div class="mkt-hdr-img" style="display:flex;align-items:center;justify-content:center;font-size:24px;">📊</div>`;

    const expDate = formatExpirationDate((oc && oc.end_date) || ev.end_date);
    const expHtml = expDate ? `<div class="mkt-stat"><span class="mkt-stat-lbl">Expires</span><span class="mkt-stat-val">${esc(expDate)}</span></div>` : "";

    return `<div class="mkt-hdr">
      <div class="mkt-hdr-identity">
        ${imgHtml}
        <div class="mkt-hdr-text">
          <div class="mkt-hdr-top">
            <div class="venue-logo-wrap"><span class="live-dot ${ev.venue}"></span><img class="venue-logo" src="${vMeta(ev.venue).logo}" alt="${vLabel}" loading="eager"><span>${vLabel}</span></div>
          </div>
          <h1>${esc(ev.title)}</h1>
        </div>
      </div>
      <div class="mkt-hdr-meta">
        ${lead ? `<div class="mkt-stat"><span class="mkt-stat-lbl">Leading (${esc(lead.label)})</span><span class="mkt-stat-val">${leadPct}</span></div>` : ""}
        <div class="mkt-stat"><span class="mkt-stat-lbl">Volume</span><span class="mkt-stat-val">${fmtUsd(ev.volume)}</span></div>
        ${expHtml}
        <div class="mkt-hdr-actions">
          <a class="btn ghost" href="${esc(ev.url)}" target="_blank" rel="noopener">${vLabel} ↗</a>
        </div>
      </div>
    </div>`;
  }

  function bindChips() {
    $$(".chip").forEach(b => b.addEventListener("click", () => {
      const slug = b.dataset.slug || "";
      if (slug === state.outcome && slug) return;
      setOutcome(slug);
      state.data = null;
      load();
    }));
  }

  function bindSlugClicks(sel) {
    $$(sel).forEach(el => el.addEventListener("click", () => {
      const slug = el.dataset.slug;
      if (!slug) return;
      setOutcome(slug);
      state.data = null;
      load();
    }));
  }

  async function fetchHistory(slugs) {
    const p = new URLSearchParams({ venue: state.venue, id: state.id });
    if (slugs?.length) p.set("outcomes", slugs.join(","));
    else p.set("top_n", "3");
    const r = await fetch(`/api/history?${p}`);
    const d = await r.json();
    const markets = state.data?.event?.markets || [];
    return (d.series || []).map(s => ({
      label: s.label,
      slug: slugForLabel(markets, s.label),
      color: colorForSlug(slugForLabel(markets, s.label), markets),
      points: s.points || [],
    }));
  }

  async function refreshOverviewChart() {
    const el = $("#multiChart");
    if (!el) return;
    if (!state.chartSlugs.length) {
      state.chartSlugs = topMarkets(state.data.event.markets).slice(0, 3).map(m => m.slug);
    }
    state.chartSeries = await fetchHistory(state.chartSlugs);
    drawMultiChart(el, state.chartSeries, state.overviewHours);
    const leg = $("#chartLegend");
    if (leg) {
      const top = topMarkets(state.data.event.markets);
      const onChart = new Set(state.chartSlugs);
      const items = state.chartSeries.map(s =>
        `<span class="leg-item"><span class="dot" style="background:${s.color}"></span>${esc(s.label)}</span>`).join("");
      const addable = top.filter(m => !onChart.has(m.slug));
      const addMenu = addable.length ? `<span class="add-menu"><button type="button" class="leg-item leg-add" id="addChartBtn">+ Add</button>
        <div class="add-dropdown" id="addDropdown">${addable.map(m =>
          `<button type="button" data-slug="${esc(m.slug)}">${esc(m.label)}</button>`).join("")}</div></span>` : "";
      leg.innerHTML = items + addMenu;
      $("#addChartBtn")?.addEventListener("click", e => {
        e.stopPropagation();
        $("#addDropdown")?.classList.toggle("open");
      });
      $("#addDropdown")?.addEventListener("click", e => e.stopPropagation());
      $$("#addDropdown button").forEach(b => b.addEventListener("click", async () => {
        if (state.chartSlugs.length >= 6) return;
        state.chartSlugs.push(b.dataset.slug);
        $("#addDropdown")?.classList.remove("open");
        await refreshOverviewChart();
      }));
    }
  }

  function bindOverviewRange() {
    $("#overviewRange")?.addEventListener("click", async e => {
      const b = e.target.closest("button");
      if (!b) return;
      state.overviewHours = +b.dataset.h;
      $$("#overviewRange button").forEach(x => x.classList.toggle("on", x === b));
      drawMultiChart($("#multiChart"), state.chartSeries, state.overviewHours);
    });
  }

  function bindDetailRange() {
    $("#detailRange")?.addEventListener("click", e => {
      const b = e.target.closest("button");
      if (!b) return;
      state.hours = +b.dataset.h;
      $$("#detailRange button").forEach(x => x.classList.toggle("on", x === b));
      drawSingleChart($("#detailChart"), state.data.history || [], state.hours);
    });
  }

  function fmtDollars(n) {
    return "$" + (Math.round(n * 100) / 100).toLocaleString(undefined, {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }

  function paperShares(amountUsd, priceCents) {
    if (!amountUsd || !priceCents || priceCents <= 0) return 0;
    return amountUsd / (priceCents / 100);
  }

  function bindPaperTrade(oc) {
    const yesPx = oc.yes != null ? oc.yes : null;
    const noPx = oc.yes != null ? 100 - oc.yes : null;

    function getSide() {
      const activeTab = document.querySelector(".paper-side-tabs .side-tab.on");
      return activeTab ? activeTab.dataset.side : "yes";
    }

    function currentPrice() {
      const raw = parseFloat($("#paperPrice")?.value);
      if (Number.isFinite(raw) && raw > 0) return raw;
      return getSide() === "yes" ? yesPx : noPx;
    }

    function updateSummary() {
      const sum = $("#paperSummary");
      const btn = $("#paperSubmit");
      if (!sum || !btn) return;
      const amount = parseFloat($("#paperAmount")?.value) || 0;
      const price = currentPrice();
      const shares = paperShares(amount, price);
      const side = getSide();
      const sideLabel = side === "yes" ? "Yes" : "No";
      if (!amount || amount <= 0) {
        sum.textContent = "Enter an amount to trade";
        btn.disabled = true;
        return;
      }
      if (!price || price <= 0) {
        sum.textContent = "Enter a valid limit price";
        btn.disabled = true;
        return;
      }
      sum.textContent = `Buy ${shares.toFixed(1)} shares of ${sideLabel} @ ${price.toFixed(1)}¢ · ${fmtDollars(amount)} total`;
      btn.disabled = side === "no" && !oc.no_token_id;
      btn.textContent = `Buy ${sideLabel}`;
      btn.className = `paper-submit ${side === "yes" ? "buy-yes" : "buy-no"}`;
    }

    // Expose this for the global delegator to trigger price updates
    window._setSideCache = (nextSide) => {
      const px = nextSide === "yes" ? yesPx : noPx;
      const priceEl = $("#paperPrice");
      if (priceEl && px != null) priceEl.value = px;
      updateSummary();
    };

    async function refreshBalance() {
      const bal = $("#paperBal");
      if (!bal) return;
      const auth = global.AppHeader ? await AppHeader.refreshAuth() : null;
      if (!auth?.user) {
        bal.innerHTML = `<a href="/account">Sign in</a>`;
        $("#paperSubmit")?.setAttribute("disabled", "");
        return;
      }
      const cash = auth.portfolio?.cash_cents;
      bal.textContent = cash != null
        ? fmtDollars(cash / 100) + " cash"
        : fmtDollars(10000) + " cash";
      updateSummary();
    }

    if (!window._paperBound) {
      window._paperBound = true;
      $("#paperAmount")?.addEventListener("input", updateSummary);
      $("#paperPrice")?.addEventListener("input", updateSummary);

      $("#paperSubmit")?.addEventListener("click", async () => {
        const msg = $("#paperMsg");
        if (!msg) return;
        const amount = parseFloat($("#paperAmount")?.value) || 0;
        const price = currentPrice();
        const shares = paperShares(amount, price);
        if (!global.__kpaUser) {
          msg.className = "paper-msg err";
          msg.textContent = "Sign in to paper trade";
          return;
        }
        if (amount <= 0 || !price || shares <= 0) {
          msg.className = "paper-msg err";
          msg.textContent = "Enter amount and price";
          return;
        }
        msg.className = "paper-msg";
        msg.textContent = "Placing…";
        $("#paperSubmit").disabled = true;
        try {
          const res = await fetch("/api/portfolio/order", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              venue: "polymarket", market_id: state.data.outcome.market_id,
              token_id: side === "yes" ? state.data.outcome.yes_token_id : state.data.outcome.no_token_id,
              outcome: side === "yes" ? "Yes" : "No",
              shares: Math.round(shares * 1000) / 1000,
              amount_cents: Math.round(amount * 100),
              limit_price: price / 100,
            }),
          });
          const d = await res.json();
          if (!res.ok) throw new Error(d.error || res.statusText);
          const t = d.trade;
          msg.className = "paper-msg ok";
          msg.textContent = `Filled ${(+t.shares).toFixed(1)} shares @ ${(t.avg_price * 100).toFixed(1)}¢ · ${fmtDollars(t.cost_cents / 100)}`;
          if (global.AppHeader) AppHeader.refreshAuth();
          refreshBalance();
        } catch (e) {
          msg.className = "paper-msg err";
          msg.textContent = e.message;
        } finally {
          updateSummary();
        }
      });
    }

    // Call updateSummary once to sync text on render
    updateSummary();
    refreshBalance();
  }

  function morph(oldNode, newHtml) {
    const tpl = document.createElement('template');
    tpl.innerHTML = newHtml;
    const newNode = tpl.content;
    function walk(oldEl, newEl) {
      if (oldEl.nodeType !== newEl.nodeType) {
        oldEl.replaceWith(newEl.cloneNode(true));
        return;
      }
      if (oldEl.nodeType === Node.TEXT_NODE) {
        if (oldEl.textContent !== newEl.textContent) oldEl.textContent = newEl.textContent;
        return;
      }
      if (oldEl.id === 'detailChart' || oldEl.id === 'multiChart' || oldEl.id === 'chartLegend' || oldEl.id === 'marketNews' || oldEl.classList?.contains('whale-ticker-track')) return;
      const oldAttrs = oldEl.attributes;
      const newAttrs = newEl.attributes;
      for (let i = oldAttrs.length - 1; i >= 0; i--) {
        const name = oldAttrs[i].name;
        if (name !== 'value' && name !== 'checked') {
          if (!newEl.hasAttribute(name)) oldEl.removeAttribute(name);
        }
      }
      for (let i = 0; i < newAttrs.length; i++) {
        const name = newAttrs[i].name;
        const val = newAttrs[i].value;
        if (name !== 'value' && name !== 'checked') {
          if (oldEl.getAttribute(name) !== val) oldEl.setAttribute(name, val);
        }
      }
      if (oldEl.tagName === 'INPUT' || oldEl.tagName === 'TEXTAREA' || oldEl.tagName === 'SELECT') {
        if (document.activeElement !== oldEl && oldEl.value !== newEl.value) {
           oldEl.value = newEl.value;
        }
      }
      const oldChildren = Array.from(oldEl.childNodes);
      const newChildren = Array.from(newEl.childNodes);
      const max = Math.max(oldChildren.length, newChildren.length);
      for (let i = 0; i < max; i++) {
        if (!oldChildren[i]) {
          oldEl.appendChild(newChildren[i].cloneNode(true));
        } else if (!newChildren[i]) {
          oldEl.removeChild(oldChildren[i]);
        } else {
          walk(oldChildren[i], newChildren[i]);
        }
      }
    }
    const newRoot = document.createElement('div');
    newRoot.appendChild(newNode);
    const oldChildren = Array.from(oldNode.childNodes);
    const newChildren = Array.from(newRoot.childNodes);
    const max = Math.max(oldChildren.length, newChildren.length);
    for (let i = 0; i < max; i++) {
      if (!oldChildren[i]) oldNode.appendChild(newChildren[i].cloneNode(true));
      else if (!newChildren[i]) oldNode.removeChild(oldChildren[i]);
      else walk(oldChildren[i], newChildren[i]);
    }
  }

  function renderLayout(isDetail) {
    const d = state.data, ev = d.event;
    const oc = isDetail ? d.outcome : null;
    const vLabel = vMeta(ev.venue).name;
    document.title = oc ? `${oc.label} · ${ev.title} · Future360` : `${ev.title} · Future360`;

    const chartHours = isDetail ? state.hours : state.overviewHours;

    const paper = isDetail && ev.venue === "polymarket" && oc.market_id && oc.yes_token_id ? `
      <div class="panel paper-box">
        <div class="paper-head">
          <span class="lbl">Paper Trade</span>
          <span class="bal" id="paperBal">—</span>
        </div>
        <div class="paper-body">
          <div class="paper-side-tabs">
            <button type="button" class="side-tab yes on" data-side="yes">Yes ${oc.yes != null ? oc.yes : "—"}¢</button>
            <button type="button" class="side-tab no" data-side="no" ${oc.no_token_id ? "" : "disabled"}>No ${oc.yes != null ? 100 - oc.yes : "—"}¢</button>
          </div>
          <div class="paper-fields">
            <label class="paper-field">
              <span>Amount</span>
              <div class="paper-input-wrap"><span class="prefix">$</span><input type="number" id="paperAmount" min="0.01" step="0.01" value="10" inputmode="decimal" /></div>
            </label>
            <label class="paper-field">
              <span>Limit price</span>
              <div class="paper-input-wrap"><input type="number" id="paperPrice" min="0.1" max="99.9" step="0.1" value="${oc.yes != null ? oc.yes : ""}" inputmode="decimal" /><span class="suffix">¢</span></div>
            </label>
          </div>
          <p class="paper-summary" id="paperSummary">Enter an amount to trade</p>
          <button type="button" class="paper-submit buy-yes" id="paperSubmit">Buy Yes</button>
          <p class="paper-msg" id="paperMsg"></p>
        </div>
      </div>` : (!isDetail ? `<div class="panel paper-box"><div class="center-msg" style="padding:40px 20px">Select an outcome to trade</div></div>` : "");

    const desc = (oc && oc.description) ? oc.description : ev.description;
    const infoHtml = (desc || ev.rules || ev.resolution_source) ? `<div class="panel panel-pad info-block">
        ${desc ? `<div><h3>Market Context</h3><p>${esc(desc).replace(/\\n/g, '<br>')}</p></div>` : ''}
        ${ev.rules ? `<div><h3>Rules & Settlement</h3><p>${esc(ev.rules).replace(/\\n/g, '<br>')}</p></div>` : ''}
        ${ev.resolution_source ? `<div><h3>Resolution Source</h3><p>${esc(ev.resolution_source).replace(/\\n/g, '<br>')}</p></div>` : ''}
      </div>` : '';

    return `
      ${headerHtml(ev, oc)}
      ${whaleTickerHtml(d)}
      <div class="detail-grid">
        <div class="grid-left">
          <div class="panel panel-pad">
            <div class="panel-hdr">
              <div><h2>${isDetail ? esc(oc.label) : "Leading Outcomes"}</h2><p class="hint">${vLabel}</p></div>
              <div class="range-row" id="${isDetail ? "detailRange" : "overviewRange"}">${rangeButtons(chartHours)}</div>
            </div>
            <div class="chart-box" id="${isDetail ? "detailChart" : "multiChart"}"></div>
            ${!isDetail ? `<div class="chart-legend" id="chartLegend"></div>` : ""}
          </div>
          
          <div class="panel panel-pad">
            <div class="panel-hdr"><div><h2>All Outcomes</h2><p class="hint">${ev.markets.length} total</p></div></div>
            ${outcomeList(ev.markets, state.outcome)}
          </div>
          
          ${infoHtml}
          ${understandHtml()}
          <div id="whaleBlock">${!isDetail ? whaleSection(d.event_whales, d.min_usd || 500) : sentimentHtml(d.whale, oc, ev.venue)}</div>
        </div>
        <div class="grid-right">
          ${paper}
          ${isDetail ? orderBookHtml(d.orderbook, oc.label, ev.venue) : `<div class="panel ob-panel"><div class="ob-panel-head"><h3>Order Book</h3></div><div class="ob-body"><div class="center-msg" style="padding:40px 20px">Select an outcome</div></div></div>`}
          ${isDetail ? tradesHtml(d.recent_trades, "Recent Trades") : `<div class="panel ob-panel"><div class="ob-panel-head"><h3>Recent Trades</h3></div><div class="ob-body"><div class="center-msg" style="padding:40px 20px">Select an outcome</div></div></div>`}
          <div id="marketNews"></div>
        </div>
      </div>
      <div class="foot-links"><a class="btn ghost" href="${isDetail ? esc(location.pathname) : "/explore"}">← ${isDetail ? "All outcomes" : "Markets"}</a></div>`;
  }

  function render() {
    if (!state.data) return;
    const isD = isDetail();
    const html = renderLayout(isD);
    morph($("#root"), html);
    
    if (isD) {
      bindPaperTrade(state.data.outcome);
      drawSingleChart($("#detailChart"), state.data.history || [], state.hours);
    } else {
      refreshOverviewChart();
    }
  }

  function understandHtml() {
    return `<div id="understandBlock"></div>`;
  }

  let _explainKey = "";
  async function fetchUnderstanding() {
    const key = `${state.venue}:${state.id}`;
    if (_explainKey === key) return; // already loaded for this market
    _explainKey = key;
    const el = document.getElementById("understandBlock");
    if (!el) return;
    try {
      const r = await fetch(`/api/market/explain?venue=${encodeURIComponent(state.venue)}&id=${encodeURIComponent(state.id)}`);
      const d = await r.json();
      if (!d.enabled) { el.innerHTML = ""; return; }
      const factors = (d.key_factors || []).map(f => `<li>${esc(f)}</li>`).join("");
      el.innerHTML = `
        <div class="understand-panel">
          <div class="understand-hdr">
            <span class="understand-hdr-icon">🔍</span>
            <h2>Understanding the Data</h2>
            <span class="ai-badge">AI</span>
          </div>
          <div class="understand-body">
            <p class="understand-summary">${esc(d.summary || "")}</p>
            <div class="understand-grid">
              ${factors ? `<div class="understand-card">
                <h4>Key Factors</h4>
                <ul class="understand-factors">${factors}</ul>
              </div>` : ""}
              ${d.how_to_read ? `<div class="understand-card">
                <h4>How to Read the Price</h4>
                <p>${esc(d.how_to_read)}</p>
              </div>` : ""}
              ${d.resolution ? `<div class="understand-card" style="grid-column:1/-1">
                <h4>Resolution</h4>
                <p>${esc(d.resolution)}</p>
              </div>` : ""}
            </div>
          </div>
        </div>`;
    } catch(e) {
      const el2 = document.getElementById("understandBlock");
      if (el2) el2.innerHTML = "";
    }
  }

  async function fetchActivity() {
    const p = new URLSearchParams({ venue: state.venue, id: state.id, hours: 24, min_usd: 500 });
    if (state.outcome) p.set("outcome", state.outcome);
    const r = await fetch(`/api/market/activity?${p}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    return d;
  }

  async function load(isPoll = false) {
    const detail = isDetail();
    if (!state.data && !isPoll) {
      $("#root").innerHTML = `<div class="center-msg">Loading market…</div>`;
    }
    
    try {
      const snap = await (await fetch(`/api/market?venue=${state.venue}&id=${encodeURIComponent(state.id)}`)).json();
      if (snap.error) throw new Error(snap.error);

      if (!state.data) {
        if (detail) {
          const pick = pickOutcome(snap.markets || []);
          if (!pick) throw new Error("Outcome not found");
          state.data = {
            event: { ...snap },
            outcome: pick, min_usd: 500,
            orderbook: null, recent_trades: null, whale: null, history: [],
          };
        } else {
          state.chartSlugs = topMarkets(snap.markets).slice(0, 3).map(m => m.slug);
          state.data = {
            event: { ...snap },
            min_usd: 500, event_whales: null,
          };
        }
        render();
        fetchUnderstanding();

        if (detail) {
          fetchAndRenderNews(`${state.data.outcome.label} ${snap.title}`, "marketNews");
        } else {
          fetchAndRenderNews(snap.title, "marketNews");
        }
      } else {
        state.data.event.markets = snap.markets;
        if (detail && state.data.outcome) {
          const newOc = snap.markets.find(m => m.slug === state.data.outcome.slug);
          if (newOc) state.data.outcome = newOc;
        }
      }

      const act = await fetchActivity();
      if (detail) {
        state.data = { ...state.data, ...act };
      } else {
        const whales = (act.event_whales || []).slice(0, TOP_N);
        state.data = { ...state.data, event_whales: whales, min_usd: act.min_usd };
      }
      render();

    } catch (e) {
      if (!isPoll) {
        $("#root").innerHTML = `<div class="center-msg">Couldn't load market.<br><small>${esc(e.message)}</small>
          <br><br><a class="btn ghost" href="/explore">Back</a></div>`;
      }
    }
  }

  function init(venue, id) {
    state.venue = venue;
    state.id = id;
    state.outcome = outcomeFromUrl();
    state.hours = 72;
    state.overviewHours = 72;
    document.addEventListener("click", () => $("#addDropdown")?.classList.remove("open"));
    
    document.addEventListener("click", e => {
      const row = e.target.closest(".out-row, .whale-row");
      if (row && row.dataset.slug) {
        setOutcome(row.dataset.slug);
        state.data = null;
        load();
        return;
      }
      
      if (e.target.closest("#overviewRange button")) {
        state.overviewHours = +e.target.dataset.h;
        render();
        return;
      }
      if (e.target.closest("#detailRange button")) {
        state.hours = +e.target.dataset.h;
        render();
        return;
      }
      
      if (e.target.closest(".paper-side-tabs .side-tab")) {
        const side = e.target.dataset.side;
        if (side === "no" && !state.data.outcome.no_token_id) return;
        $$(".paper-side-tabs .side-tab").forEach(t => t.classList.toggle("on", t.dataset.side === side));
        if (window._setSideCache) window._setSideCache(side);
        return;
      }
    });

    load();
    window.addEventListener("popstate", () => {
      state.outcome = outcomeFromUrl();
      state.data = null;
      load();
    });
    
    setInterval(() => {
      if (state.data) load(true);
    }, 2500);
  }

  global.MarketDetail = { init };
})(window);
