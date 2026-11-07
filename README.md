# Renko ATR Pro Cloud Cockpit (XAUUSD / Capital.com)

A 24/7 cloud-ready web dashboard, backtester, 2,500-run Monte Carlo simulator, and semi-automatic / automatic execution bot for **Capital.com** (`GOLD` / `XAUUSD` and FX CFDs).

Exact Python port of the **Pine Script v6 "Renko ATR Strategy (Long + Short)"** (`ATR(5)` on 23-hour timeframe `/ 5`, Supertrend-on-bricks `len=1, factor=2.1`), upgraded with **7 Gold-specific institutional protections**.

---

## Key Features

1. **Exact Pine v6 Engine + Pro Gold Audit Protections (`renko.py`, `risk.py`)**
   - Non-repainting previous-day Wilder `ATR(5) / 5` brick size rounded to `mintick`.
   - 1-brick continuation, 2-brick reversal (`use_hl=True`), Supertrend calculated directly on bricks.
   - **Pro Filters (toggleable ON/OFF vs. Raw Pine v6 in 1 click)**:
     - **Lock 1 Trade Per Brick Run**: prevents the `"While condition holds"` whipsaw bug from re-entering the same brick run after a safety stop.
     - **Multi-Brick News Spike & Overshoot Guard**: skips macro spike candles (`> 3` bricks in one bar or `> 1.5` bricks overshoot past trigger brick).
     - **Grid-Anchored Brick Origin**: deterministic brick levels across server restarts.
     - **Gold Daily Rollover Blackout (`20:45–22:15 UTC`)**: avoids 5x rollover spread spikes.
     - **Session Filter & Breakdown**: separate Asian, London, and New York session performance tables & toggles.
     - **Broker-Side 3-Brick Trailing Safety Stop + Breakeven Lock**.

2. **Cloud-Hardened Web Cockpit (`server.py`, `static/index.html`)**
   - **Password Gate (`DASHBOARD_PASSWORD`)**: HMAC-signed HttpOnly session cookies + Bearer token auth so your cloud URL is locked against unauthorized access.
   - **Built-in 24/7 Background Watcher Thread**: Start, pause, or step the live Capital.com bar watcher directly from the top bar of the web UI.
   - **Semi-Auto Order Confirmation Banner**: Approve or reject pending trade tickets from your phone or desktop browser before the TTL timer expires, or receive instant Telegram alerts (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
   - **Emergency Kill Switch (`FLATTEN / KILL SWITCH`)**: Closes open Capital.com positions and halts the watcher immediately.

3. **Zero External Dependencies**
   - Written 100% in Python 3.12 standard library (`http.server`, `urllib`, `hmac`, `unittest`). Fast cold starts, zero dependency vulnerabilities.

---

## 1-Click Cloud Hosting (Access From Anywhere)

Push this repository to GitHub and connect it to any of the following platforms:

### Option A: Railway (Recommended for 24/7 Bot)
1. Go to [Railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**.
2. Select this repository (`railway.json` and `Dockerfile` are auto-detected).
3. Open the **Variables** tab and set:
   - `DASHBOARD_PASSWORD` = *(choose a strong password to lock your web UI)*
   - `CAPITAL_ENV` = `demo` *(or `live` when ready)*
   - `CAPITAL_API_KEY` = *(from Capital.com Settings → API integrations)*
   - `CAPITAL_IDENTIFIER` = *(your Capital.com login email)*
   - `CAPITAL_PASSWORD` = *(your API key custom password)*
   - `AUTO_START_WATCHER` = `true` *(optional: auto-starts the 24/7 watcher on boot)*
4. Generate a public domain under **Settings → Networking → Generate Domain**.

### Option B: Render.com
1. Go to [Render.com](https://render.com) → **New +** → **Blueprint** (or **Web Service**) and connect this repo (`render.yaml` is included).
2. Enter your environment variables (`DASHBOARD_PASSWORD`, `CAPITAL_API_KEY`, `CAPITAL_IDENTIFIER`, `CAPITAL_PASSWORD`) when prompted.

### Option C: Fly.io / Any Docker VPS
```bash
fly launch --copy-config
fly secrets set DASHBOARD_PASSWORD="your-password" CAPITAL_API_KEY="..." CAPITAL_IDENTIFIER="..." CAPITAL_PASSWORD="..."
fly deploy
```
Or with plain Docker on any Linux VPS:
```bash
docker build -t renko-atr-cockpit .
docker run -d --restart unless-stopped -p 8765:8765 --env-file .env renko-atr-cockpit
```

---

## Running Tests Locally

```bash
python3 -m unittest discover -s tests -v
```
