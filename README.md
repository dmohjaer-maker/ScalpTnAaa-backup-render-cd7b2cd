# GoldScalperPro v4 Stable

**Production-grade gold scalping system for MetaTrader 5 via MetaAPI cloud bridge.**

> **Frozen Strategy.** All trading logic, signal engines, and risk thresholds are frozen at v4 Stable. This repository contains only engineering fixes — no strategy modifications.

---

## System Architecture

```
┌──────────────────────────────┐    JSON files    ┌───────────────────────────┐
│  Live Trading Engine          │◄────────────────►│  Telegram Control Panel   │
│  live_trading/                │  robot_state.json│  telegram_panel/          │
│  Python / asyncio / MetaAPI   │  robot_cmds.json │  Python / python-tg-bot   │
│                               │  mt5_snapshot    │  SQLite / aiosqlite       │
└──────────────┬────────────────┘                  └───────────────────────────┘
               │ MetaAPI WebSocket
               ▼
┌──────────────────────────────┐
│  MetaTrader 4/5 Broker        │
│  (via MetaAPI cloud proxy)    │
└──────────────────────────────┘

┌──────────────────────────────┐
│  TypeScript Backtest Engine   │  (offline / development tool only)
│  robot/                       │  Uses backtestEngineV2 with real CSV data
└──────────────────────────────┘
```

---

## Quick Start

### Prerequisites

- Python 3.11 or 3.12 (3.13 not yet tested)
- MetaAPI account: [app.metaapi.cloud](https://app.metaapi.cloud)
- MT5 broker account (demo recommended for first run)

### Live Trading Engine

```bash
# 1. Install dependencies (exact versions — pinned for reproducibility)
pip install -r live_trading/requirements.txt

# 2. Configure environment
cp live_trading/.env.example .env
# Edit .env with your MetaAPI credentials and risk settings

# 3. Run
python -m live_trading.main
```

### Telegram Control Panel

```bash
# 1. Install dependencies
pip install -r telegram_panel/requirements.txt

# 2. Generate an encryption key (MANDATORY for production)
python -m telegram_panel.main --generate-key
# Copy the key to PANEL_ENCRYPTION_KEY env var

# 3. Configure environment
cp telegram_panel/.env.example .env
# Edit .env with your Telegram bot token, owner ID, and encryption key

# 4. Run (separate terminal from the robot)
python -m telegram_panel.main
```

---

## Required Environment Variables

### Live Trading Engine

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `METAAPI_TOKEN` | **YES** | — | MetaAPI API token |
| `METAAPI_ACCOUNT_ID` | **YES** | — | MetaAPI account ID |
| `SYMBOL` | No | `XAUUSDb` | Trading instrument (AMarkets-Demo account symbol) |
| `RISK_PERCENT` | No | `1.0` | Risk per trade (% of balance) |
| `MIN_CONFIRMATIONS` | No | `3` | Min signal confirmations (3 or 4) |
| `DAILY_LOSS_LIMIT_PCT` | No | `3.0` | Guardian: daily loss halt threshold |
| `MAX_DRAWDOWN_PCT` | No | `8.0` | Guardian: drawdown halt threshold |
| `SLIPPAGE_POINTS` | No | `30` | Max fill slippage in broker points |

### Telegram Panel

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | **YES** | — | Bot token from @BotFather |
| `TELEGRAM_OWNER_ID` | **YES** | — | Your Telegram numeric user ID |
| `PANEL_ENCRYPTION_KEY` | **YES** | — | Fernet key for credential encryption |
| `TELEGRAM_ADMIN_IDS` | No | — | Comma-separated admin Telegram IDs |

---

## Deployment

See [DEPLOYMENT_GUIDE.md](audit_reports/DEPLOYMENT_GUIDE.md) for full deployment instructions including:
- Render.com two-service deployment
- Persistent storage configuration
- systemd service setup
- Docker Compose setup

---

## Operations

See [OPERATIONS_GUIDE.md](audit_reports/OPERATIONS_GUIDE.md) for:
- Telegram panel commands reference
- Guardian circuit breaker management
- Backup and recovery procedures
- Log analysis guide
- Upgrade and key rotation procedures

---

## Risk Warning

This software trades real or simulated money. **Always test on a demo account first.**

- The backtest engine V1 (`backtestEngine.ts`) uses **synthetic price data** — its results are NOT validated against real market conditions and must not be used to justify live deployment.
- The backtest engine V2 (`backtestEngineV2.ts`) requires a real XAUUSD historical CSV file. No CSV is included in this repository.
- Past performance (even on real historical data) does not guarantee future results.

---

## Audit Status

| Item | Status |
|------|--------|
| Trading behaviour frozen | ✅ Certified unchanged |
| 9 Phase-1 engineering fixes | ✅ Regression-verified |
| 11 Phase-2 engineering fixes | ✅ Applied (this release) |
| Dependency versions pinned | ✅ |
| Guardian env vars in deployment config | ✅ |
| Log rotation | ✅ |
| Candle deduplication | ✅ |
| Encryption key enforcement | ✅ |
| Engineering test suite | ✅ |
| Double-entry risk on disconnect | ⚠️ Requires live paper test |
| Backtest on real historical data | ⚠️ Requires real XAUUSD CSV |
| Persistent storage (Render) | ⚠️ Requires persistent disk mount |

Full audit reports: [audit_reports/](audit_reports/)

---

## Project Structure

```
GoldScalperPro/
├── live_trading/              ← Live trading engine (Python)
│   ├── main.py
│   ├── config.py
│   ├── requirements.txt       ← Pinned exact versions
│   ├── .env.example           ← Environment variable template
│   ├── signals/               ← 7 signal engines (FROZEN)
│   ├── risk/                  ← Guardian + capital manager (FROZEN)
│   ├── mt5/                   ← MetaAPI connector + executor
│   ├── trading/               ← Async M5 trading loop
│   └── utils/                 ← State file writer
├── telegram_panel/            ← Telegram control panel (Python)
│   ├── main.py
│   ├── requirements.txt       ← Pinned exact versions
│   ├── .env.example
│   └── ...
├── robot/                     ← TypeScript backtest engine (dev tool only)
│   └── src/
├── tests/                     ← Engineering test suite
├── audit_reports/             ← Independent audit documentation
├── render.yaml                ← Two-service Render deployment
├── README.md                  ← This file
├── CHANGELOG.md               ← Version history
└── LICENSE                    ← MIT License
```

---

*GoldScalperPro v4 Stable — Audited Release 2026-07-19*
