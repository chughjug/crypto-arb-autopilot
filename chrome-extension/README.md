# CryptoArb — Crypto.com Auto-Trader Extension

A Chrome extension that automatically executes crypto.com Predict arb trades when your autopilot dashboard detects an opportunity.

## How it works

```
Autopilot API (/api/autopilot/bankroll)
      ↓ (polls every 15s)
Background service worker
      ↓ detects new crypto.com opportunity
      ↓ opens chrome.tabs.create(event URL)
Content script (injected into web.crypto.com)
      ↓ finds the correct strike row
      ↓ clicks YES or NO button
      ↓ enters quantity
      ↓ clicks Place Order → Confirm → PIN
      ↓ reports success/failure
Background closes the tab
```

## Installation

1. Open Chrome → `chrome://extensions/`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select the `chrome-extension/` folder from this project
5. The ⚡ icon will appear in your toolbar

## Setup

Click the extension icon and fill in:

| Field | Where to get it |
|---|---|
| **App URL** | Your Heroku URL e.g. `https://your-app.herokuapp.com` or `http://localhost:8123` |
| **Session token** | Open your dashboard → DevTools → Application → Cookies → copy the `session` cookie value |
| **Default qty** | Number of contracts to buy per leg (e.g. `10`) |
| **Min edge (¢)** | Only trade if edge is at least this many cents (e.g. `2`) |
| **Crypto.com PIN** | 6-digit transaction PIN from crypto.com → Account → Security → Transaction PIN |

Click **Save**, then toggle **Auto-execute** on.

## Trade execution flow

When a new qualifying arb is detected, the extension:

1. Opens `https://web.crypto.com/explore/predict/events/{slug}` in a new tab
2. Waits for the table to render
3. Finds the row matching the target strike price (±$1 tolerance)
4. Clicks the **YES** or **NO** button (whichever leg crypto.com should fill)
5. Sets the quantity in the order form
6. Clicks **Place Order**
7. Clicks **Confirm** in the confirmation modal
8. Enters your 6-digit PIN in the PIN dialog
9. Waits for success
10. Closes the tab automatically

## Security notes

- The session token and PIN are stored in Chrome's `chrome.storage.local` (encrypted at rest by Chrome's profile encryption)
- **Never** share or export your Chrome profile if you have this extension installed with real credentials
- The extension only makes requests to `web.crypto.com` and your own app URL — no external servers
- PIN is entered character-by-character into the native DOM inputs (not transmitted anywhere)

## Files

```
chrome-extension/
├── manifest.json      # Extension config, permissions
├── background.js      # Service worker: polls API, manages tab lifecycle
├── content.js         # DOM automation on crypto.com event pages
├── popup.html         # Extension popup UI
├── popup.js           # Popup logic: settings, log rendering
└── icons/
    ├── icon32.png
    └── icon48.png
```

## Troubleshooting

**"No slug found" error**: The opportunity data from the scanner doesn't have a crypto.com event slug. Make sure the catalog has fully warmed up.

**Wrong row found**: Strike matching uses ±$1 tolerance. If crypto.com's strike labels differ significantly in format, update `parseStrike()` in `content.js`.

**PIN not accepted**: The PIN inputs use Mantine PinInput which responds to native `input` events. If future crypto.com UI updates break this, check the input's `data-*` attributes for changes.

**Tab closes too fast**: Increase `sleep(1500)` in `background.js` `TRADE_RESULT` handler.
