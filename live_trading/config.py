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
_VALID_TIMEFRAMES = {
    "1m", "5m", "10m", "15m", "20m", "30m", "1h", "4h", "1d",
    "M1", "M5", "M10", "M15", "M20", "M30", "H1", "H4", "D1",
}

# Minute-equivalent of every supported timeframe — used to sort TRADE_TIMEFRAMES
# highest-first so that longer TF signals always get evaluated before shorter ones.
_TF_MINUTES: dict[str, int] = {
    "1m": 1,   "M1":  1,
    "5m": 5,   "M5":  5,
    "10m": 10, "M10": 10,
    "15m": 15, "M15": 15,
    "20m": 20, "M20": 20,
    "30m": 30, "M30": 30,
    "1h":  60, "H1":  60,
    "4h":  240,"H4":  240,
    "1d":  1440,"D1": 1440,
}


def _trade_timeframes(name: str, default: str) -> list[str]:
    """Parse a comma-separated list of timeframe labels, validate each entry
    against _VALID_TIMEFRAMES, and return them sorted highest-first.

    Example:  TRADE_TIMEFRAMES=M20,M15,M10,5m  →  ["M20","M15","M10","5m"]
    """
    raw = os.getenv(name, default)
    tfs = [tf.strip() for tf in raw.split(",") if tf.strip()]
    if not tfs:
        print(
            f"ERROR: {name} is empty. Provide a comma-separated list "
            f"of timeframes, e.g. M20,M15,M10,5m",
            file=sys.stderr,
        )
        sys.exit(1)
    for tf in tfs:
        if tf not in _VALID_TIMEFRAMES:
            print(
                f"ERROR: {name} contains invalid timeframe {tf!r}. "
                f"Valid values: {', '.join(sorted(_VALID_TIMEFRAMES))}. "
                f"Fix it in the Render dashboard and redeploy.",
                file=sys.stderr,
            )
            sys.exit(1)
    # Sort highest-first so the loop processes longer-TF signals first.
    return sorted(tfs, key=lambda t: _TF_MINUTES.get(t, 0), reverse=True)


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
SYMBOL        = os.getenv("SYMBOL", "XAUUSD")
# M1 is the default execution timeframe. M5/M15 can still be enabled through
# TRADE_TIMEFRAMES for context and confirmation.
TIMEFRAME     = _timeframe("TIMEFRAME", "1m")
# 500 bars gives M1 indicator/structure engines enough lookback without making
# every candle request unnecessarily heavy for the mt5rest bridge.
CANDLE_WINDOW = _int("CANDLE_WINDOW", 500, lo=50, hi=5000)

# ── Risk & Trade Rules ───────────────────────────────────────────────────────
# Production defaults — override via Render env vars if needed.
# MIN_CONFIRMATIONS: minimum engines that must agree (out of 4: SMC, Trend, PA, Wyckoff).
# CONF_HARD_MIN: trades below this confidence % are always rejected.
RISK_PERCENT      = _float("RISK_PERCENT",      1.0,  lo=0.01, hi=10.0)
# MIN_CONFIRMATIONS=2: SMC (always) + any 1 of (Trend / PA / Wyckoff).
# Wyckoff fires rarely on 5m; PA patterns don't appear every candle.
# Requiring 3 caused multi-day silences. 2 keeps quality while allowing flow.
MIN_CONFIRMATIONS = _int("MIN_CONFIRMATIONS",   2,    lo=1,    hi=10)
# When enabled, every new trade must also have a same-direction Price Action signal.
# Default false preserves existing behavior until explicitly enabled on Render.
REQUIRE_PRICE_ACTION = os.getenv("REQUIRE_PRICE_ACTION", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
# Aggressive M1 range profile: RANGE entries may use the configured minimum
# (normally SMC + one independent confirmation) instead of the stricter
# three-vote range gate. Confidence, quality, R:R, MTF, retest, and Guardian
# gates remain active.
RANGE_MIN_CONFIRMATIONS = _int("RANGE_MIN_CONFIRMATIONS", 2, lo=2, hi=4)
RANGE_REQUIRE_PRICE_ACTION = os.getenv(
    "RANGE_REQUIRE_PRICE_ACTION", "false"
).strip().lower() in {"1", "true", "yes", "on"}
# Exact option 1 gate: SMC, Price Action, and Wyckoff must all agree with the
# candidate direction. EMA remains informational/confirmatory and is not
# required for entry.
REQUIRE_SMC_PRICE_ACTION_WYCKOFF = os.getenv(
    "REQUIRE_SMC_PRICE_ACTION_WYCKOFF", "false"
).strip().lower() in {"1", "true", "yes", "on"}
# CONF_HARD_MIN=40%: balanced absolute floor for live entry quality.
# Regime-specific thresholds remain higher where the market is less forgiving.
CONF_HARD_MIN     = _float("CONF_HARD_MIN",      40.0, lo=0.0, hi=100.0)
# QUALITY_ADX_MIN: minimum ADX value required to confirm trend momentum.
# Below this threshold the quality filter rejects the signal as "low momentum".
# 15 is the recommended floor for M5 gold — catches genuine micro-trends
# without blocking valid moves that ADX=20 would silently filter out.
QUALITY_ADX_MIN   = _float("QUALITY_ADX_MIN",    15.0, lo=5.0,  hi=40.0)
# Maximum age of the structure event that can authorize a new entry.
# The M1 profile uses 60 closed bars (about one hour) so a valid structure
# does not expire after only a few minutes while the retest gate remains active.
STRUCTURE_MAX_AGE_BARS = _int("STRUCTURE_MAX_AGE_BARS", 60, lo=3, hi=100)
MAX_OPEN_TRADES   = 1

USE_ATR_HIGH_VOL_FILTER = os.getenv("USE_ATR_HIGH_VOL_FILTER", "false").lower() == "true"
# ── Multi-Timeframe (HTF) Filter ─────────────────────────────────────────────
# MTF_ENABLED       : enable/disable the HTF alignment gate (default on).
#                     Set to "false" to revert to M5-only behaviour instantly.
# MTF_TIMEFRAME     : the Higher TimeFrame to use for bias detection.
#                     "H1" is the recommended default for M5 scalping of gold.
#                     Supported: M1 M5 M15 M30 H1 H4 D1 (same set as TIMEFRAME).
# MTF_CANDLE_WINDOW : number of HTF bars to fetch (needs ≥ 210 for EMA-200).
#                     300 gives a comfortable margin without excessive latency.
MTF_ENABLED       = os.getenv("MTF_ENABLED",   "true").lower() == "true"
MTF_TIMEFRAME     = _timeframe("MTF_TIMEFRAME",  "H1")
MTF_CANDLE_WINDOW = _int("MTF_CANDLE_WINDOW",    300, lo=50, hi=1000)

# ── Break-and-retest entry protection ─────────────────────────────────────────
# A directional BOS/CHoCH is not enough by itself: the default gate waits for a
# closed-candle retest and rejection of the broken level before placing an order.
REQUIRE_BREAK_RETEST = os.getenv(
    "REQUIRE_BREAK_RETEST", "true"
).strip().lower() in {"1", "true", "yes", "on"}
RETEST_MAX_BARS = _int("RETEST_MAX_BARS", 3, lo=1, hi=10)
RETEST_ZONE_ATR_MULT = _float("RETEST_ZONE_ATR_MULT", 0.20, lo=0.0, hi=2.0)
RETEST_CLOSE_BUFFER_ATR = _float(
    "RETEST_CLOSE_BUFFER_ATR", 0.05, lo=0.0, hi=1.0
)
RETEST_MIN_BODY_ATR = _float("RETEST_MIN_BODY_ATR", 0.15, lo=0.0, hi=1.0)

# ── Trade Timeframes (Multi-Timeframe entry) ─────────────────────────────────
# Comma-separated list of timeframes the robot will watch for new bars and
# generate independent trade signals on.  Sorted automatically highest-first
# so M20 and M15 signals take priority over M10 and M5 when bars close
# simultaneously (e.g. at minute :20 all four TFs close at once).
#
# The H1 HTF bias filter (MTF_TIMEFRAME above) is separate — it is always
# computed on H1 regardless of which trade TFs are active, because H1
# represents the directional context for the whole session.
#
# M1 execution profile: "M5,1m" (confirmation first, M1 entry last).
# H1 supplies the broader directional context; keeping M5 and M1 avoids
# duplicate signals from too many overlapping entry timeframes.
TRADE_TIMEFRAMES  = _trade_timeframes("TRADE_TIMEFRAMES", "M5,1m")
# A dashboard-level TRADE_TIMEFRAMES override previously put the live robot
# onto an older M20/M15/M10/M5 profile even while TIMEFRAME was still 1m.
# Keep M1 present whenever the execution profile says M1 is required.
M1_EXECUTION_REQUIRED = os.getenv(
    "M1_EXECUTION_REQUIRED", "true"
).strip().lower() in {"1", "true", "yes", "on"}
if M1_EXECUTION_REQUIRED and TIMEFRAME in {"1m", "M1"} and not any(
    _TF_MINUTES.get(tf) == 1 for tf in TRADE_TIMEFRAMES
):
    TRADE_TIMEFRAMES = sorted(
        {*TRADE_TIMEFRAMES, "1m"},
        key=lambda tf: _TF_MINUTES.get(tf, 0),
        reverse=True,
    )



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

# ── Staircase Trailing Stop ───────────────────────────────────────────────────
# The stop loss placed at trade entry never moved automatically before — a
# trade could run deep into profit and still be stopped out at its original,
# now far-too-generous level if price reversed. The staircase trailing engine
# (live_trading/risk/trailing_stop.py) now ratchets the stop forward in
# discrete steps, measured in multiples of the trade's own original risk (R),
# as the trade advances — never backwards. All of it is env-configurable so
# it can be tuned on Render without a code change.
TRAIL_ENABLED        = os.getenv("TRAIL_ENABLED", "true").lower() == "true"
# R-multiple of profit required before the stop first moves off its entry level.
# Keep this at 1R by default: normal noise before the trade has earned its
# original risk must never cause an SL modification.
TRAIL_ACTIVATION_R   = _float("TRAIL_ACTIVATION_R",   1.0,  lo=0.1, hi=10.0)
# Size, in R-multiples, of each staircase step beyond activation.
TRAIL_STEP_R         = _float("TRAIL_STEP_R",         0.5,  lo=0.05, hi=5.0)
# Extra R-multiple locked in at every step so the stop locks real profit
# (covers spread/slippage) instead of landing on exact break-even.
TRAIL_LOCK_BUFFER_R  = _float("TRAIL_LOCK_BUFFER_R",  0.08, lo=0.0, hi=2.0)
# Safety floor: the stop is never placed closer to the live price than this
# multiple of the current ATR, so a fast move can't ratchet the stop into
# the middle of normal M5 noise.
TRAIL_ATR_GAP_MULT   = _float("TRAIL_ATR_GAP_MULT",   0.8,  lo=0.0, hi=5.0)
# Minimum price-unit improvement required before sending a modify request —
# avoids spamming OrderModifySafe with no-op / sub-cent adjustments.
TRAIL_MIN_STEP_PRICE = _float("TRAIL_MIN_STEP_PRICE", 0.05, lo=0.0, hi=100.0)
# Smart candle-aware trailing.  The stop follows the best favorable quote,
# then tightens around confirmed swing structure and consecutive opposing
# closed candles.  These are deliberately separate knobs so the operator can
# tune responsiveness without changing the entry strategy.
TRAIL_PEAK_ATR_GAP_MULT = _float("TRAIL_PEAK_ATR_GAP_MULT", 1.10, lo=0.1, hi=5.0)
TRAIL_PEAK_R_GAP_MULT   = _float("TRAIL_PEAK_R_GAP_MULT",   0.60, lo=0.1, hi=5.0)
TRAIL_STRUCTURE_BUFFER_ATR = _float(
    "TRAIL_STRUCTURE_BUFFER_ATR", 0.18, lo=0.0, hi=2.0
)
TRAIL_REVERSAL_CONFIRMATION_BARS = _int(
    "TRAIL_REVERSAL_CONFIRMATION_BARS", 2, lo=1, hi=5
)
TRAIL_REVERSAL_TIGHTEN_ATR_MULT = _float(
    "TRAIL_REVERSAL_TIGHTEN_ATR_MULT", 0.75, lo=0.1, hi=3.0
)
TRAIL_SWING_LOOKBACK = _int("TRAIL_SWING_LOOKBACK", 2, lo=1, hi=5)

# TP structure clearance.  A structure-aware target is placed just before a
# significant resistance/support level.  If no usable level exists, the
# capital manager falls back to its 2R target.
TP_STRUCTURE_BUFFER_ATR = _float(
    "TP_STRUCTURE_BUFFER_ATR", 0.15, lo=0.0, hi=1.0
)

# ── Wyckoff Calibration ──────────────────────────────────────────────────────
WYCKOFF_MAX_RANGE_PCT = 0.01163
WYCKOFF_SPRING_MARGIN = 2.06

# ── Redis IPC ────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "")

# ── Backward-compat stubs (no longer used) ───────────────────────────────────
METAAPI_TOKEN      = ""
METAAPI_ACCOUNT_ID = ""
