"""
GoldScalperPro v4 – Configuration

All settings are read from environment variables so they can be changed
on Render without touching code.

Required:
    MTAPI_URL     – URL of the mt5rest Docker service on Render
    MT5_HOST      – broker server name (e.g. AMarkets-Demo)
    MT5_USER      – MT5 account login number
    MT5_PASSWORD  – MT5 account password
"""
import os

# ── MT5 bridge URL ────────────────────────────────────────────────────────────
# The mt5rest Docker service on Render.  Required.
MTAPI_URL     = os.getenv("MTAPI_URL",     "")

# ── MT5 Broker Credentials ───────────────────────────────────────────────────
MT5_HOST      = os.getenv("MT5_HOST",     "AMarkets-Demo")
MT5_PORT      = int(os.getenv("MT5_PORT", "443"))
MT5_USER      = os.getenv("MT5_USER",     "")
MT5_PASSWORD  = os.getenv("MT5_PASSWORD", "")

# ── Symbol & Timeframe ───────────────────────────────────────────────────────
SYMBOL        = os.getenv("SYMBOL",    "XAUUSDb")
TIMEFRAME     = "5m"          # mt5rest period value
CANDLE_WINDOW = 300           # bars sent to signal engine

# ── Risk & Trade Rules ───────────────────────────────────────────────────────
RISK_PERCENT            = float(os.getenv("RISK_PERCENT",     "1.0"))
MIN_CONFIRMATIONS       = int(os.getenv("MIN_CONFIRMATIONS",  "3"))
MAX_OPEN_TRADES         = 1

# FIX: USE_ATR_HIGH_VOL_FILTER was hardcoded to False and could not be
# enabled without modifying source code.  It is now env-configurable via
# USE_ATR_HIGH_VOL_FILTER=true in the Render service environment.
# Default is "false" (preserves existing behaviour — no behaviour change
# unless the operator explicitly sets USE_ATR_HIGH_VOL_FILTER=true).
USE_ATR_HIGH_VOL_FILTER = os.getenv("USE_ATR_HIGH_VOL_FILTER", "false").lower() == "true"

# ── Order Settings ───────────────────────────────────────────────────────────
COMMENT = "GSPv4"

# ── Loop Timing ──────────────────────────────────────────────────────────────
BAR_CHECK_INTERVAL = 15       # seconds between candle-close checks
RECONNECT_DELAY    = 30       # seconds before reconnect attempt
SYNC_TIMEOUT       = 120      # seconds to wait for initial connect

# ── File Paths (for Telegram panel) ─────────────────────────────────────────
STATE_FILE          = os.getenv("STATE_FILE",           "robot_state.json")
MT5_SNAPSHOT        = os.getenv("MT5_SNAPSHOT",         "robot_mt5_snapshot.json")
COMMANDS_FILE       = os.getenv("COMMANDS_FILE",        "robot_commands.json")
GUARDIAN_STATE_FILE = os.getenv("GUARDIAN_STATE_FILE",  "guardian_state.json")
LOG_FILE            = os.getenv("LOG_FILE",             "live_trading/robot.log")

# ── Risk Guardian – Circuit Breakers ────────────────────────────────────────
DAILY_LOSS_LIMIT_PCT = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "3.0"))
MAX_DRAWDOWN_PCT     = float(os.getenv("MAX_DRAWDOWN_PCT",      "8.0"))
SLIPPAGE_POINTS      = int(os.getenv("SLIPPAGE_POINTS",         "30"))

# ── Wyckoff Calibration ──────────────────────────────────────────────────────
WYCKOFF_MAX_RANGE_PCT = 0.01163
WYCKOFF_SPRING_MARGIN = 2.06

# ── Redis IPC ────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")

# ── Backward-compat stubs (no longer used) ───────────────────────────────────
METAAPI_TOKEN      = ""
METAAPI_ACCOUNT_ID = ""
