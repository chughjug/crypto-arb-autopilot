/**
 * Site-wide configurable view mode: auto / mobile / desktop.
 *
 * Instead of hardcoded @media queries, page CSS keys off classes this script
 * sets on <html> (synchronously, before first paint):
 *   .vm-mobile   phone layout    — forced by "mobile", or auto at <= 768px
 *   .vm-compact  narrow layout   — forced by "mobile", or auto at <= 900px
 *   .vm-wide     wide extras     — width >= 1220px, suppressed by "mobile"
 *   .vm-frame    centered phone frame when mobile is forced on a wide screen
 * "desktop" also widens the viewport meta so phones render the desktop layout
 * (browser "request desktop site" behavior). The choice persists in
 * localStorage and is shared by every page.
 */
(function (global) {
  const KEY = "kpa:viewmode";
  const MOBILE_BP = 768, COMPACT_BP = 900, WIDE_BP = 1220;
  const MODES = ["auto", "mobile", "desktop"];
  const LABELS = { auto: "Auto", mobile: "Mobile", desktop: "Desktop" };

  function get() {
    try {
      const v = localStorage.getItem(KEY);
      return MODES.includes(v) ? v : "auto";
    } catch { return "auto"; }
  }

  function viewportMeta() {
    let m = document.querySelector('meta[name="viewport"]');
    if (!m) {
      m = document.createElement("meta");
      m.name = "viewport";
      document.head.appendChild(m);
    }
    return m;
  }

  function apply() {
    const mode = get();
    const w = global.innerWidth || document.documentElement.clientWidth || 1280;
    const cls = document.documentElement.classList;
    const mobile = mode === "mobile" || (mode === "auto" && w <= MOBILE_BP);
    cls.toggle("vm-mobile", mobile);
    cls.toggle("vm-compact", mode === "mobile" || (mode === "auto" && w <= COMPACT_BP));
    cls.toggle("vm-wide", mode !== "mobile" && w >= WIDE_BP);
    cls.toggle("vm-frame", mode === "mobile" && w > 560);
    viewportMeta().setAttribute("content",
      mode === "desktop" ? "width=1100" : "width=device-width, initial-scale=1");
    document.dispatchEvent(new CustomEvent("viewmodechange", { detail: { mode, mobile } }));
  }

  function set(mode) {
    if (!MODES.includes(mode)) mode = "auto";
    try { localStorage.setItem(KEY, mode); } catch { /* private mode etc. */ }
    apply();
    document.querySelectorAll(".vm-seg button").forEach(b =>
      b.classList.toggle("on", b.dataset.vmMode === mode));
  }

  /** Segmented Auto|Mobile|Desktop control — used by the header and fallback. */
  function controlHtml() {
    const cur = get();
    return `<span class="vm-seg" role="group" aria-label="View mode">` +
      MODES.map(m =>
        `<button type="button" data-vm-mode="${m}" class="${m === cur ? "on" : ""}">${LABELS[m]}</button>`
      ).join("") + `</span>`;
  }

  document.addEventListener("click", e => {
    const b = e.target.closest(".vm-seg button");
    if (b) set(b.dataset.vmMode);
  });
  global.addEventListener("resize", apply);

  // Self-contained styles (with token fallbacks) so the control and phone
  // frame also work on standalone pages that don't load theme.css.
  const css = document.createElement("style");
  css.id = "vm-css";
  css.textContent = `
    .vm-seg { display: inline-flex; background: var(--surface-2, #f1f5f9); border: 1px solid var(--border, #e2e8f0);
      border-radius: 999px; padding: 2px; gap: 2px; }
    .vm-seg button { border: 0; background: transparent; padding: 5px 12px; border-radius: 999px;
      font-size: 12px; font-weight: 600; line-height: 1.2; font-family: inherit;
      color: var(--muted, #64748b); cursor: pointer; }
    .vm-seg button.on { background: var(--surface, #fff); color: var(--ink, #0f172a);
      box-shadow: 0 1px 2px rgba(15,23,42,.12); }
    .vm-float { position: fixed; right: 14px; bottom: 14px; z-index: 90;
      background: var(--surface, #fff); border-radius: 999px; box-shadow: 0 6px 20px rgba(0,0,0,.16); }
    html.vm-frame body { max-width: 460px; margin: 0 auto; min-height: 100vh;
      border-left: 1px solid var(--border-strong, #cbd5e1); border-right: 1px solid var(--border-strong, #cbd5e1);
      box-shadow: 0 0 40px rgba(15,23,42,.08); }
    html.vm-mobile body { overflow-x: hidden; }
  `;
  document.head.appendChild(css);

  // Floating control for pages without the shared header (never inside embeds).
  function mountFallback() {
    if (global.top !== global.self) return;
    if (document.getElementById("appHeader") || document.querySelector(".app-nav")) return;
    if (document.querySelector(".vm-float")) return;
    const d = document.createElement("div");
    d.className = "vm-float";
    d.innerHTML = controlHtml();
    document.body.appendChild(d);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountFallback);
  } else {
    mountFallback();
  }

  apply();
  global.ViewMode = { get, set, apply, controlHtml };
})(window);
