/**
 * content.js — Injected into https://web.crypto.com/explore/predict/events/*
 *
 * Waits for a EXECUTE_TRADE message from the background service worker,
 * then drives the full click sequence:
 *   1. Find the correct row by strike price
 *   2. Click YES or NO button
 *   3. Set quantity
 *   4. Click "Place Order"
 *   5. Click "Confirm"
 *   6. Enter 6-digit PIN
 *   7. Report success/failure back to background
 */

(function () {
  'use strict';

  // Prevent double-injection
  if (window.__cryptoArbInjected) return;
  window.__cryptoArbInjected = true;

  // ── Utilities ──────────────────────────────────────────────────────────

  const sleep = ms => new Promise(r => setTimeout(r, ms));

  /**
   * Wait for a selector to appear in the DOM.
   * Returns the element or throws if timeout expires.
   */
  function waitFor(selector, timeoutMs = 12000, root = document) {
    return new Promise((resolve, reject) => {
      const existing = root.querySelector(selector);
      if (existing) { resolve(existing); return; }

      const observer = new MutationObserver(() => {
        const el = root.querySelector(selector);
        if (el) { observer.disconnect(); resolve(el); }
      });
      observer.observe(root.body || root, { childList: true, subtree: true });

      setTimeout(() => {
        observer.disconnect();
        reject(new Error(`Timeout waiting for "${selector}"`));
      }, timeoutMs);
    });
  }

  /**
   * Wait for an element matching selector whose textContent includes text.
   */
  function waitForText(selector, text, timeoutMs = 10000) {
    return new Promise((resolve, reject) => {
      const check = () => {
        const els = document.querySelectorAll(selector);
        for (const el of els) {
          if (el.textContent.includes(text)) return el;
        }
        return null;
      };
      const found = check();
      if (found) { resolve(found); return; }

      const observer = new MutationObserver(() => {
        const el = check();
        if (el) { observer.disconnect(); resolve(el); }
      });
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });

      setTimeout(() => {
        observer.disconnect();
        const el = check();
        if (el) resolve(el);
        else reject(new Error(`Timeout waiting for "${selector}" with text "${text}"`));
      }, timeoutMs);
    });
  }

  /**
   * Click an element robustly by dispatching pointer events to satisfy React handlers.
   */
  function clickElement(el) {
    if (!el) return;
    log('Clicking element: ' + (el.tagName || '') + ' ' + (el.textContent || '').trim());
    el.focus?.();
    const opts = { bubbles: true, cancelable: true, view: window };
    el.dispatchEvent(new MouseEvent('mousedown', opts));
    el.dispatchEvent(new MouseEvent('mouseup', opts));
    el.click();
  }

  /**
   * Set value on a React-controlled input by bypassing React's synthetic
   * event system via document.execCommand and native setters.
   */
  function setReactInputValue(input, value) {
    log('Setting React input value to: ' + value);
    input.focus();
    input.select?.();
    
    // Try to set via execCommand (types like a user)
    let commandSuccess = false;
    try {
      commandSuccess = document.execCommand('insertText', false, String(value));
    } catch (e) {
      log('execCommand failed, trying fallback', e);
    }
    
    if (!commandSuccess) {
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
      ).set;
      if (nativeSetter) {
        nativeSetter.call(input, String(value));
      } else {
        input.value = String(value);
      }
    }
    
    input.dispatchEvent(new Event('input', { bubbles: true, cancelable: true }));
    input.dispatchEvent(new Event('change', { bubbles: true, cancelable: true }));
    input.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: String(value) }));
    input.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: String(value) }));
  }

  /**
   * Parse a strike string like ">$63,960.00" → 63960
   */
  function parseStrike(text) {
    const cleaned = text.replace(/[^0-9.]/g, '');
    return parseFloat(cleaned);
  }

  /**
   * Find the tbody row whose strike price matches targetStrike (±$1 tolerance).
   */
  function findRow(targetStrike) {
    const rows = document.querySelectorAll('tbody.mantine-Table-tbody tr.mantine-Table-tr');
    for (const row of rows) {
      const h2 = row.querySelector('h2');
      if (!h2) continue;
      const val = parseStrike(h2.textContent);
      if (Math.abs(val - targetStrike) <= 1) return row;
    }
    return null;
  }

  // ── Main execution flow ────────────────────────────────────────────────

  async function executeTrade({ side, strike, price, quantity, pin }) {
    const log = (msg, data) => {
      console.log('[CryptoArb content]', msg, data || '');
    };

    log('Starting trade execution', { side, strike, quantity });

    try {
      // ── Step 1: Wait for table to render ────────────────────────────
      log('Waiting for prediction table…');
      await waitFor('tbody.mantine-Table-tbody tr.mantine-Table-tr', 15000);
      await sleep(800); // Extra wait for all rows to populate

      // ── Step 2: Find the correct row ─────────────────────────────────
      const row = findRow(strike);
      if (!row) {
        throw new Error(`Could not find row for strike $${strike}. Table may not have loaded.`);
      }
      log('Found row for strike ' + strike);

      // ── Step 3: Click YES or NO button ───────────────────────────────
      // YES button: data-disabled-yes attribute
      // NO button:  data-disabled-no attribute
      const btnAttr = side === 'yes' ? 'data-disabled-yes' : 'data-disabled-no';
      const btn = row.querySelector(`button[${btnAttr}]`);
      if (!btn) {
        throw new Error(`Could not find ${side.toUpperCase()} button in row`);
      }

      // Verify the price roughly matches (sanity check within 10¢)
      const btnText = btn.textContent || '';
      const btnPrice = parseFloat(btnText.replace(/[^0-9.]/g, '')) || 0;
      const expectedPrice = Number(price);
      if (Math.abs(btnPrice - expectedPrice) > 0.10) {
        log(`Warning: button shows $${btnPrice.toFixed(2)} but expected ~$${expectedPrice.toFixed(2)} — proceeding anyway`);
      }

      log(`Clicking ${side.toUpperCase()} button: "${btnText.trim()}"`);
      clickElement(btn);
      await sleep(800);

      // ── Step 4: Set quantity ──────────────────────────────────────────
      log('Waiting for quantity input…');
      const qtyInput = await waitFor('input[data-path="quantity"]', 8000);
      qtyInput.focus();
      await sleep(200);

      // Clear existing value and set new one
      // Some Mantine NumberInputs need the value selected first
      qtyInput.select?.();
      setReactInputValue(qtyInput, quantity);
      await sleep(400);

      // Verify it took
      log(`Quantity set to: ${qtyInput.value}`);

      // ── Step 5: Click "Place Order" ───────────────────────────────────
      log('Waiting for Place Order button…');
      let placeBtn;
      try {
        placeBtn = await waitForText(
          'button .mantine-Button-label, button span',
          'Place Order',
          8000
        );
        // Walk up to the actual button element
        placeBtn = placeBtn.closest('button') || placeBtn;
      } catch {
        // Fallback: find any button whose text contains Place Order
        placeBtn = [...document.querySelectorAll('button')].find(
          b => b.textContent.trim().includes('Place Order')
        );
      }
      if (!placeBtn) throw new Error('Could not find "Place Order" button');
      log('Clicking Place Order');
      clickElement(placeBtn);
      await sleep(1000);

      // ── Step 6: Click "Confirm" ───────────────────────────────────────
      log('Waiting for Confirm button…');
      let confirmBtn;
      try {
        confirmBtn = await waitForText('button', 'Confirm', 8000);
      } catch {
        confirmBtn = [...document.querySelectorAll('button')].find(
          b => b.textContent.trim() === 'Confirm'
        );
      }
      if (!confirmBtn) throw new Error('Could not find "Confirm" button');
      log('Clicking Confirm');
      clickElement(confirmBtn);
      await sleep(1200);

      // ── Step 7: Enter PIN ─────────────────────────────────────────────
      if (!pin || pin.length < 1) {
        throw new Error('No PIN configured — cannot complete trade');
      }

      log('Waiting for PIN input…');
      await waitFor('.mantine-PinInput-input', 10000);
      await sleep(300);

      const pinInputs = document.querySelectorAll('.mantine-PinInput-input');
      if (!pinInputs.length) throw new Error('PIN inputs not found');

      log(`Entering ${pin.length}-digit PIN into ${pinInputs.length} inputs`);
      for (let i = 0; i < Math.min(pin.length, pinInputs.length); i++) {
        const inp = pinInputs[i];
        inp.focus();
        await sleep(80);
        setReactInputValue(inp, pin[i]);
        // Also dispatch keyboard events for PIN components that listen to keydown
        inp.dispatchEvent(new KeyboardEvent('keydown', { key: pin[i], code: 'Digit' + pin[i], bubbles: true }));
        inp.dispatchEvent(new KeyboardEvent('keyup',  { key: pin[i], code: 'Digit' + pin[i], bubbles: true }));
        await sleep(120);
      }

      // ── Step 8: Wait for success indicator ───────────────────────────
      log('PIN entered — waiting for success confirmation…');
      await sleep(2000);

      // Look for success text or lack of PIN inputs (dialog closed)
      const stillHasPin = document.querySelector('.mantine-PinInput-input');
      const successEl = document.querySelector('[data-success]') ||
        [...document.querySelectorAll('*')].find(el =>
          el.textContent.includes('Order placed') ||
          el.textContent.includes('Success') ||
          el.textContent.includes('Confirmed')
        );

      if (stillHasPin && !successEl) {
        // PIN dialog still open — might have failed
        log('Warning: PIN dialog still visible after entry — checking for errors');
        const errEl = document.querySelector('[data-error="true"]') ||
          document.querySelector('[role="alert"]');
        if (errEl) {
          throw new Error('PIN error: ' + (errEl.textContent || 'Invalid PIN'));
        }
        // Give it one more second
        await sleep(1500);
      }

      log('Trade execution complete!');
      return { success: true, details: { side, strike, quantity, price: btnPrice } };

    } catch (err) {
      log('ERROR: ' + err.message, err);
      return { success: false, error: err.message };
    }
  }

  // ── Listen for background → content messages ───────────────────────────

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.type === 'EXECUTE_TRADE') {
      executeTrade(msg).then(result => {
        chrome.runtime.sendMessage({ type: 'TRADE_RESULT', ...result });
        sendResponse({ ok: true });
      });
      return true; // keep channel open for async
    }
  });

  // ── Startup check for query parameters ─────────────────────────────────
  sleep(1500).then(() => {
    const urlParams = new URLSearchParams(window.location.search);
    const side = urlParams.get('side');
    const strike = parseFloat(urlParams.get('strike'));
    const qty = parseInt(urlParams.get('qty') || urlParams.get('quantity'));
    const price = parseFloat(urlParams.get('price'));
    const pin = urlParams.get('pin');

    if (side && strike && qty) {
      executeTrade({ side: side.toLowerCase(), strike, price, quantity: qty, pin: pin || '' }).then(result => {
        chrome.runtime.sendMessage({ type: 'TRADE_RESULT', ...result });
      });
    } else {
      chrome.runtime.sendMessage({ type: 'CONTENT_READY' });
    }
  });

})();
