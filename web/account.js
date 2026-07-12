/** Account page — register, login, mandatory 2FA */
(function () {
  const $ = s => document.querySelector(s);
  let mode = "login";
  let pending = null;

  function setMode(m) {
    mode = m;
    pending = null;
    $("tabLogin").classList.toggle("on", m === "login");
    $("tabRegister").classList.toggle("on", m === "register");
    $("authTitle").textContent = m === "login" ? "Sign in" : "Create account";
    $("authSubmit").textContent = m === "login" ? "Sign In" : "Create account";
    $("password").required = true;
    $("password").autocomplete = m === "login" ? "current-password" : "new-password";
    $("twofaBox").classList.remove("show");
    $("setupInfo").style.display = "none";
    $("authTabs").style.display = "flex";
    $("userField").style.display = "block";
    $("pwField").style.display = "block";
    $("authMsg").textContent = "";
  }

  function show2fa(kind, data) {
    pending = { kind, token: data.challenge_token || data.setup_token };
    $("twofaBox").classList.add("show");
    $("totpCode").value = "";
    $("totpCode").focus();
    if (kind === "setup") {
      $("authTabs").style.display = "none";
      $("userField").style.display = "none";
      $("pwField").style.display = "none";
      $("authTitle").textContent = "Set up 2FA";
      $("authSubmit").textContent = "Confirm & sign in";
      $("twofaTitle").textContent = "Scan authenticator app";
      $("setupInfo").style.display = "block";
      $("otpLink").href = data.otpauth_uri || "#";
      $("otpSecret").textContent = data.totp_secret || "";
    } else {
      $("setupInfo").style.display = "none";
      $("authTitle").textContent = "Two-factor authentication";
      $("authSubmit").textContent = "Verify & sign in";
      $("twofaHint").textContent = "Enter the 6-digit code from your authenticator app.";
    }
  }

  async function parseJson(res) {
    const text = await res.text();
    try {
      return text ? JSON.parse(text) : {};
    } catch {
      throw new Error(text || res.statusText || "Request failed");
    }
  }

  async function finish2fa(code) {
    const path = pending.kind === "setup" ? "/api/auth/2fa/confirm" : "/api/auth/2fa/verify";
    const body = pending.kind === "setup"
      ? { setup_token: pending.token, code }
      : { challenge_token: pending.token, code };
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(body),
    });
    const d = await parseJson(res);
    if (!res.ok) throw new Error(d.error || res.statusText);
    $("authMsg").className = "auth-msg ok";
    $("authMsg").textContent = "Success — redirecting…";
    if (window.AppHeader) AppHeader.refreshAuth().then(() => AppHeader.mount("#appHeader"));
    location.href = "/autopilot";
  }

  function init() {
    const tabLogin = $("tabLogin");
    const tabRegister = $("tabRegister");
    const authForm = $("authForm");
    if (!tabLogin || !tabRegister || !authForm) return;

    tabLogin.addEventListener("click", () => setMode("login"));
    tabRegister.addEventListener("click", () => setMode("register"));

    $("authForm").addEventListener("submit", async e => {
      e.preventDefault();
      const msg = $("authMsg");
      msg.className = "auth-msg";
      try {
        if (pending) {
          const code = $("totpCode").value.trim();
          if (!/^\d{6}$/.test(code)) throw new Error("Enter a 6-digit authenticator code");
          msg.textContent = "Verifying…";
          await finish2fa(code);
          return;
        }

        const username = $("username").value.trim();
        const password = $("password").value;
        if (!username) throw new Error("Username required");
        if (password.length < 10) throw new Error("Password must be at least 10 characters");

        msg.textContent = "Working…";
        if (mode === "register") {
          const res = await fetch("/api/auth/register", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            credentials: "same-origin",
            body: JSON.stringify({ username, password }),
          });
          const d = await parseJson(res);
          if (!res.ok) throw new Error(d.error || res.statusText);
          if (d.requires_2fa_setup) {
            show2fa("setup", d);
            msg.className = "auth-msg ok";
            msg.textContent = "Account created — set up your authenticator app below.";
            return;
          }
          throw new Error("Unexpected response — try again");
        }

        const res = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "same-origin",
          body: JSON.stringify({ username, password }),
        });
        const d = await parseJson(res);
        if (!res.ok) throw new Error(d.error || res.statusText);
        if (d.requires_2fa) {
          show2fa("login", d);
          msg.textContent = "";
          return;
        }
        if (d.requires_2fa_setup) {
          show2fa("setup", d);
          msg.className = "auth-msg ok";
          msg.textContent = "Finish setting up 2FA to continue.";
          return;
        }
      } catch (err) {
        msg.textContent = err.message || "Something went wrong";
      }
    });

    fetch("/api/auth/me", { credentials: "same-origin" })
      .then(r => r.json())
      .then(d => {
        if (d.user && !d.user.is_guest) location.href = "/autopilot";
      })
      .catch(() => {});

    setMode("login");
  }

  function boot() {
    if ($("authForm")) init();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
