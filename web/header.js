/** Crypto Arb Autopilot — app header (Radian-style, matches theme.css) */
(function (global) {
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const N = {
    markets:   { href: "/crypto", label: "Markets", match: p => p === "/crypto" || p.startsWith("/crypto-market") },
    arb:       { href: "/cryptoarbitrage", label: "Arb scanner", match: p => p.startsWith("/cryptoarbitrage") || p === "/cryptoarb" },
    bots:      { href: "/bots", label: "Bots", match: p => p === "/bots" },
    autopilot: { href: "/autopilot", label: "Autopilot", match: p => p === "/autopilot" || p === "/bankroll" },
    account:   { href: "/account", label: "Account", match: p => p === "/account" },
  };

  const PRIMARY = [N.markets, N.arb, N.bots, N.autopilot];

  const SECTIONS = [
    {
      match: p => p === "/crypto" || p.startsWith("/crypto-market"),
      eyebrow: "Crypto",
      items: [
        { href: "/crypto", label: "Markets" },
        { href: "/cryptoarbitrage", label: "Arb scanner" },
      ],
    },
  ];

  let _auth = { user: null };

  function navItem(n, path) {
    const active = n.match && n.match(path) ? " active" : "";
    return `<a href="${esc(n.href)}" class="nav-link${active}">${esc(n.label)}</a>`;
  }

  function accountHtml(user) {
    if (!user || user.is_guest) {
      return `<a href="/account" class="btn-signin">Sign in</a>`;
    }
    const initial = (user.display_name || user.username || "?")[0].toUpperCase();
    const name = (user.display_name || user.username || "Account").split(" ")[0];
    const lock = user.totp_enabled
      ? `<span class="header-2fa" title="2FA enabled">🔒</span>`
      : `<span class="header-2fa warn" title="2FA not enabled">!</span>`;
    return `<a href="/account" class="account-chip" title="${esc(user.display_name || user.username)}">
      <span class="avatar">${esc(initial)}</span>
      <span class="name">${esc(name)}</span>${lock}
    </a>`;
  }

  function renderSubnav(host, path) {
    const existing = document.getElementById("appSubnav");
    const section = SECTIONS.find(s => s.match(path));
    if (!section) {
      if (existing) existing.remove();
      return;
    }
    const links = section.items.map(it => {
      const active = it.href === path || (it.href === "/cryptoarbitrage" && path.startsWith("/cryptoarbitrage"))
        ? " active" : "";
      return `<a href="${esc(it.href)}" class="subnav-link${active}">${esc(it.label)}</a>`;
    }).join("");
    const html = `<div class="subnav-inner">
      <span class="nav-eyebrow">${esc(section.eyebrow)}</span>
      <nav class="subnav-links">${links}</nav>
    </div>`;
    if (existing) {
      existing.innerHTML = html;
      return;
    }
    const bar = document.createElement("div");
    bar.id = "appSubnav";
    bar.className = "app-subnav";
    bar.innerHTML = html;
    host.insertAdjacentElement("afterend", bar);
  }

  function primaryNav(path, user) {
    const items = user ? PRIMARY : PRIMARY.filter(n => n.href !== "/autopilot");
    return items.map(n => navItem(n, path)).join("");
  }

  function shellHtml(path, user) {
    const primary = primaryNav(path, user);
    const drawerItems = user ? PRIMARY : PRIMARY.filter(n => n.href !== "/autopilot");
    const drawer = drawerItems.map(n => navItem(n, path)).join("")
      + navItem(N.account, path);
    return `<div class="app-nav-inner">
        <a href="/crypto" class="nav-logo" aria-label="Crypto Arb home">
          <img src="/logo.png" alt="" class="nav-logo-img" width="36" height="36" />
        </a>
        <div class="nav-explore">
          <nav class="nav-links nav-desktop" aria-label="Primary">${primary}</nav>
        </div>
        <div class="nav-right nav-desktop">${accountHtml(user)}</div>
        <button type="button" class="menu-btn nav-mobile" aria-expanded="false" aria-controls="navDrawer">Menu</button>
      </div>
      <div class="nav-drawer nav-mobile" id="navDrawer" hidden>
        <div class="nav-drawer-section">
          <span class="nav-eyebrow">Navigate</span>
          <nav class="nav-links nav-links-stack">${drawer}</nav>
        </div>
        <div class="nav-drawer-account">${accountHtml(user)}</div>
      </div>`;
  }

  function injectHeaderCss() {
    if (document.getElementById("caa-header-css")) return;
    const s = document.createElement("style");
    s.id = "caa-header-css";
    s.textContent = `
      .nav-desktop { display: flex; }
      .nav-mobile { display: none !important; }
      @media (max-width: 768px) {
        .nav-desktop { display: none !important; }
        .nav-mobile.menu-btn { display: inline-flex !important; }
        .nav-mobile.nav-drawer:not([hidden]) { display: block !important; }
      }
      .header-2fa { font-size: 11px; margin-left: 2px; opacity: .85; }
      .header-2fa.warn { color: #d97706; font-weight: 800; }
    `;
    document.head.appendChild(s);
  }

  function wireMenu(root) {
    const btn = root.querySelector(".menu-btn");
    const drawer = root.querySelector("#navDrawer");
    if (!btn || !drawer) return;
    btn.addEventListener("click", () => {
      const open = drawer.hasAttribute("hidden");
      if (open) drawer.removeAttribute("hidden");
      else drawer.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      root.classList.toggle("menu-open", open);
    });
    document.addEventListener("click", e => {
      if (!root.contains(e.target)) {
        drawer.setAttribute("hidden", "");
        btn.setAttribute("aria-expanded", "false");
        root.classList.remove("menu-open");
      }
    });
  }

  function isMounted(host) {
    return host && host.classList.contains("app-nav") && host.querySelector(".app-nav-inner");
  }

  async function refreshAuth() {
    try {
      const r = await fetch("/api/auth/me", { credentials: "same-origin" });
      const d = await r.json();
      _auth.user = d.user || null;
    } catch {
      _auth.user = null;
    }
    return _auth;
  }

  function mount(sel, opts = {}) {
    const host = typeof sel === "string" ? document.querySelector(sel) : sel;
    if (!host) return;
    injectHeaderCss();
    const path = location.pathname;
    const user = opts.user ?? _auth.user;

    host.classList.add("app-nav");
    host.innerHTML = shellHtml(path, user);
    renderSubnav(host, path);
    wireMenu(host);

    refreshAuth().then(auth => {
      if (!auth.user && !user) return;
      if (JSON.stringify(auth.user) !== JSON.stringify(user)) {
        mount(host, { ...opts, user: auth.user });
      }
    });
  }

  function autoMount() {
    const host = document.getElementById("appHeader");
    if (host && !isMounted(host)) mount(host);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoMount);
  } else {
    autoMount();
  }

  global.AppHeader = { mount, refreshAuth, autoMount, PRIMARY, N, SECTIONS };
})(window);
