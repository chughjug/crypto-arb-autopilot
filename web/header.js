/** Crypto Arb Autopilot — focused nav */
(function (global) {
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const N = {
    markets:   { href: "/crypto", label: "Markets", match: p => p === "/crypto" || p.startsWith("/crypto-market") },
    arb:       { href: "/cryptoarbitrage", label: "Arb scanner", match: p => p.startsWith("/cryptoarbitrage") || p === "/cryptoarb" },
    bots:      { href: "/bots", label: "Bots", match: p => p === "/bots" },
    autopilot: { href: "/autopilot", label: "Autopilot", match: p => p === "/autopilot" },
    account:   { href: "/account", label: "Account", match: p => p === "/account" },
  };

  const PRIMARY = [N.markets, N.arb, N.bots, N.autopilot, N.account];

  function mount(sel, opts = {}) {
    const el = document.querySelector(sel);
    if (!el) return;
    const path = location.pathname;
    const links = PRIMARY.map(n => {
      const on = n.match(path) || (opts.activeView === n.href);
      return `<a href="${n.href}" class="nav-link${on ? ' on' : ''}">${esc(n.label)}</a>`;
    }).join("");
    el.innerHTML = `<header class="app-header"><div class="app-header-inner">
      <a href="/crypto" class="brand">Crypto Arb</a>
      <nav class="nav-primary">${links}</nav>
    </div></header>`;
  }

  global.AppHeader = { mount, PRIMARY, N };
})(window);
