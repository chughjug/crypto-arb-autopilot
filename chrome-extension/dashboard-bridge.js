/**
 * dashboard-bridge.js — Content script injected into the dashboard pages.
 *
 * Listens for the client-side postMessage event when a user manually triggers
 * a trade execution on the dashboard, and forwards it to background.js.
 */
(function() {
  'use strict';

  window.addEventListener('message', event => {
    // Only accept messages from our own window/app
    if (event.source !== window) return;

    if (event.data && event.data.type === 'EXECUTE_CRYPTOCOM_TRADE') {
      const { opp, quantity } = event.data;
      console.log('[CryptoArb Bridge] Received execution command from dashboard:', opp);
      
      // Send to background service worker to trigger execution flow
      chrome.runtime.sendMessage({
        type: 'TEST_TRADE',
        opp: opp,
        quantity: quantity
      });
    }
  });
})();
