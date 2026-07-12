/**
 * redirect.js — Content script running at document_start
 *
 * Intercepts /cryptocom/[id] URLs loaded in the browser and redirects
 * them immediately to the real crypto.com prediction details page.
 */
(function() {
  'use strict';
  const match = window.location.pathname.match(/\/cryptocom\/([a-zA-Z0-9\-]+)/);
  if (match) {
    const id = match[1];
    const realUrl = `https://web.crypto.com/hub/predict/events/details/${id}${window.location.search}`;
    console.log(`[CryptoArb Redirect] Intercepting ${window.location.pathname} -> redirecting to ${realUrl}`);
    window.location.replace(realUrl);
  }
})();
