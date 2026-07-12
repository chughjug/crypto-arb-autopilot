/** Tiny live-data client: WebSocket with auto-reconnect + REST polling fallback. */
(function (global) {
  function wsUrl(path) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}`;
  }

  /**
   * LiveChannel(opts)
   *   opts.ws        WebSocket path (e.g. "/ws/game?league=nba&event=1")
   *   opts.poll      REST URL for fallback (e.g. "/api/game?league=nba&event=1")
   *   opts.interval  fallback poll interval ms (default 10000)
   *   opts.onData(msg)   called with parsed payload on every update
   *   opts.onStatus(s)   called with "live" | "polling" | "closed"
   */
  function LiveChannel(opts) {
    let socket = null, pollTimer = null, retry = 0, stopped = false, mode = "";

    function setStatus(s) { if (s !== mode) { mode = s; opts.onStatus && opts.onStatus(s); } }

    function startPolling() {
      setStatus("polling");
      stopPolling();
      const hit = () => fetch(opts.poll).then(r => r.json()).then(d => opts.onData && opts.onData(d)).catch(() => {});
      hit();
      pollTimer = setInterval(hit, opts.interval || 10000);
    }
    function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

    function connect() {
      if (stopped) return;
      let ws;
      try { ws = new WebSocket(wsUrl(opts.ws)); }
      catch { startPolling(); return; }
      socket = ws;
      ws.onopen = () => { retry = 0; stopPolling(); setStatus("live"); };
      ws.onmessage = e => {
        try { opts.onData && opts.onData(JSON.parse(e.data)); } catch {}
      };
      ws.onclose = () => {
        if (stopped) { setStatus("closed"); return; }
        // fall back to polling immediately, then try to re-establish the socket
        if (mode !== "polling") startPolling();
        retry = Math.min(retry + 1, 6);
        setTimeout(connect, 1000 * retry);
      };
      ws.onerror = () => { try { ws.close(); } catch {} };
    }

    connect();

    return {
      close() {
        stopped = true;
        stopPolling();
        if (socket) { try { socket.close(); } catch {} }
        setStatus("closed");
      },
    };
  }

  global.Live = { LiveChannel, wsUrl };
})(window);
