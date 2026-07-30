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
import sys

# ── Bounded env-var helpers ───────────────────────────────────────────────────

def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    raw = os.getenv(name, str(default))
    try:
        val = int(raw)
    except ValueError:
        print(
            f"ERROR: {name}={raw!r} is not a valid integer. "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if lo is not None and val < lo:
        print(
            f"ERROR: {name}={val} is below the minimum allowed value ({lo}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if hi is not None and val > hi:
        print(
            f"ERROR: {name}={val} is above the maximum allowed value ({hi}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


def _float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    raw = os.getenv(name, str(default))
    try:
        val = float(raw)
    except ValueError:
        print(
            f"ERROR: {name}={raw!r} is not a valid number. "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if lo is not None and val < lo:
        print(
            f"ERROR: {name}={val} is below the minimum allowed value ({lo}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    if hi is not None and val > hi:
        print(
            f"ERROR: {name}={val} is above the maximum allowed value ({hi}). "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


# ── Valid timeframe labels ────────────────────────────────────────────────────
_VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d",
                     "M1", "M5", "M15", "M30", "H1", "H4", "D1"}


def _timeframe(name: str, default: str) -> str:
    val = os.getenv(name, default)
    if val not in _VALID_TIMEFRAMES:
        print(
            f"ERROR: {name}={val!r} is not a recognised timeframe. "
            f"Valid values: {', '.join(sorted(_VALID_TIMEFRAMES))}. "
            f"Fix it in the Render dashboard and redeploy.",
            file=sys.stderr,
        )
        sys.exit(1)
    return val


# ── MT5 bridge URL ────────────────────────────────────────────────────────────
MTAPI_URL     = os.getenv("MTAPI_URL",     "")

# ── MT5 Broker Credentials ───────────────────────────────────────────────────
MT5_HOST      = os.getenv("MT5_HOST",     "AMarkets-Demo")
MT5_PORT      = _int("MT5_PORT", 443, lo=1, hi=65535)
MT5_USER      = os.getenv("MT5_USER",     "")
MT5_PASSWORD  = os.getenv("MT5_PASSWORD", "")

# ── Symbol & Timeframe ───────────────────────────────────────────────────────
SYMBOL        = os.getenv("SYMBOL", "XAUUSDb")
TIMEFRAME     = _timeframe("TIMEFRAME", "5m")
CANDLE_WINDOW = _int("CANDLE_WINDOW", 300, lo=50, hi=5000)

# ── Risk & Trade Rules ───────────────────────────────────────────────────────
RISK_PERCENT      = _float("RISK_PERCENT",      1.0, lo=0.01, hi=10.0)
MIN_CONFIRMATIONS = _int("MIN_CONFIRMATIONS",   2,   lo=1,    hi=10)
CONF_HARD_MIN     = _float("CONF_HARD_MIN",      45.0, lo=0.0, hi=100.0)
MAX_OPEN_TRADES   = 1

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

# ── Risk Guardian – Circuit Breakers ─────────────────────────────────────────
# lo=0.1 prevents accidentally disabling protection with 0 or negative values.
DAILY_LOSS_LIMIT_PCT = _float("DAILY_LOSS_LIMIT_PCT", 3.0,  lo=0.1, hi=50.0)
MAX_DRAWDOWN_PCT     = _float("MAX_DRAWDOWN_PCT",      8.0,  lo=0.1, hi=50.0)
SLIPPAGE_POINTS      = _int("SLIPPAGE_POINTS",         30,   lo=1,   hi=500)

# ── Wyckoff Calibration ──────────────────────────────────────────────────────
WYCKOFF_MAX_RANGE_PCT = 0.01163
WYCKOFF_SPRING_MARGIN = 2.06

# ── Redis IPC ────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")

# ── Backward-compat stubs (no longer used) ───────────────────────────────────
METAAPI_TOKEN      = ""
METAAPI_ACCOUNT_ID = ""
