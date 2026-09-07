"""
Market Regime Detector — 11 regimes with adaptive entry rules.
Ported from marketRegimeDetector.ts
"""
from dataclasses import dataclass
from typing import Literal
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.trend_engine import TrendResult
from live_trading.signals.wyckoff_engine import WyckoffResult
from typing import List

MarketRegime = Literal[
    "STRONG_TREND_BULL", "STRONG_TREND_BEAR",
    "WEAK_TREND_BULL",   "WEAK_TREND_BEAR",
    "PULLBACK_BULL",     "PULLBACK_BEAR",
    "RANGE", "ACCUMULATION", "DISTRIBUTION",
    "HIGH_VOLATILITY",   "LOW_VOLATILITY",
]


@dataclass
class RegimeEntryRules:
    min_confidence: float
    min_rr: float
    allow_long: bool
    allow_short: bool
    sl_atr_mult_adjust: float
    label: str


@dataclass
class RegimeResult:
    regime: str
    rules: RegimeEntryRules
    atr: float
    atr_mean: float
    atr_ratio: float
    adx: float
    description: str


# Calibrated regime rules — balanced for 5m XAUUSD scalping.
# STRONG/WEAK trend: lower confidence bar (EMA alignment already strong evidence).
# RANGE / HIGH_VOLATILITY: tighter gates — dangerous for scalping.
# Marginal setups (conf ≥ CONF_HARD_MIN but < min_conf) can still trade
# via the CONF_MARGINAL_RR path in decision_engine.
REGIME_RULES = {
    #                                           min_conf  min_rr  long   short  sl_mult  label
    "STRONG_TREND_BULL": RegimeEntryRules(40,  1.2,  True,  False, 1.0,  "Strong Bull Trend"),
    "STRONG_TREND_BEAR": RegimeEntryRules(40,  1.2,  False, True,  1.0,  "Strong Bear Trend"),
    "WEAK_TREND_BULL":   RegimeEntryRules(45,  1.2,  True,  False, 0.9,  "Weak Bull Trend"),
    "WEAK_TREND_BEAR":   RegimeEntryRules(45,  1.2,  False, True,  0.9,  "Weak Bear Trend"),
    "PULLBACK_BULL":     RegimeEntryRules(50,  1.2,  True,  False, 0.95, "Bull Pullback"),
    "PULLBACK_BEAR":     RegimeEntryRules(50,  1.2,  False, True,  0.95, "Bear Pullback"),
    "RANGE":             RegimeEntryRules(60,  1.2,  True,  True,  0.8,  "Range / Choppy"),
    "ACCUMULATION":      RegimeEntryRules(50,  1.2,  True,  False, 1.0,  "Wyckoff Accumulation"),
    "DISTRIBUTION":      RegimeEntryRules(50,  1.2,  False, True,  1.0,  "Wyckoff Distribution"),
    "HIGH_VOLATILITY":   RegimeEntryRules(65,  1.2,  True,  True,  1.3,  "High Volatility"),
    "LOW_VOLATILITY":    RegimeEntryRules(40,  1.2,  True,  True,  0.7,  "Low Volatility / Squeeze"),
}


def _calc_atr_values(candles: List[OHLCV], period: int = 20):
    trs = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
    if not trs:
        return 0.0, 0.0, 1.0
    # Use a 5-bar average for the "current" ATR instead of the single last TR.
    # A single-bar TR spike (e.g. from a news wick) would otherwise instantly
    # flip the regime to HIGH_VOLATILITY and block valid trade entries for the
    # entire bar.  A 5-bar average still reacts quickly to real volatility
    # expansions while filtering one-candle outliers.
    short_window = min(5, len(trs))
    atr      = sum(trs[-short_window:]) / short_window
    atr_mean = sum(trs[-period:]) / min(period, len(trs))
    atr_ratio = round(atr / atr_mean, 3) if atr_mean > 0 else 1.0
    return atr, atr_mean, atr_ratio


def calc_adx(candles: List[OHLCV], period: int = 14) -> float:
    if len(candles) < period * 2:
        return 20.0  # conservative non-trending default when data is insufficient
    n = len(candles)
    trs, dm_p, dm_m = [], [], []
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)))
        up = c.high - p.high
        dn = p.low  - c.low
        dm_p.append(up if up > dn and up > 0 else 0.0)
        dm_m.append(dn if dn > up and dn > 0 else 0.0)
    s_tr = sum(trs[:period])
    s_dp = sum(dm_p[:period])
    s_dm = sum(dm_m[:period])
    dx_arr = []
    for i in range(period, len(trs)):
        s_tr = s_tr - s_tr / period + trs[i]
        s_dp = s_dp - s_dp / period + dm_p[i]
        s_dm = s_dm - s_dm / period + dm_m[i]
        di_p = 100 * s_dp / s_tr if s_tr > 0 else 0
        di_m = 100 * s_dm / s_tr if s_tr > 0 else 0
        total = di_p + di_m
        dx_arr.append(100 * abs(di_p - di_m) / total if total > 0 else 0)
    if len(dx_arr) < period:
        return 20.0  # conservative non-trending default
    return round(sum(dx_arr[-period:]) / period, 2)


def _detect_pullback(candles: List[OHLCV], trend: TrendResult):
    # A one-candle move against the EMA trend is not enough to relabel the
    # market as a pullback.  The old detector compared only candle -1 with
    # candle -6, so a single wick or news candle could flip the regime.
    if len(candles) < 12:
        return None
    window = candles[-6:]
    changes = [
        window[i].close - window[i - 1].close
        for i in range(1, len(window))
    ]
    net_move = window[-1].close - window[0].close
    reference = max(abs(window[0].close), 1e-9)
    net_pct = abs(net_move) / reference

    # Require a majority of counter-trend closes and a meaningful net move.
    # This is intentionally below the full trend threshold: it detects a
    # retracement, not a confirmed reversal.
    if trend.trend == "BULLISH":
        counter_moves = sum(change < 0 for change in changes)
        if counter_moves >= 3 and net_move < 0 and net_pct >= 0.0005:
            return "BULL"
    if trend.trend == "BEARISH":
        counter_moves = sum(change > 0 for change in changes)
        if counter_moves >= 3 and net_move > 0 and net_pct >= 0.0005:
            return "BEAR"
    return None


def detect_market_regime(
    candles: List[OHLCV],
    trend: TrendResult,
    wyckoff: WyckoffResult,
    use_atr_high_vol: bool = False,
) -> RegimeResult:
    atr, atr_mean, atr_ratio = _calc_atr_values(candles, 20)
    adx = calc_adx(candles, 14)

    def make(regime: str, desc: str) -> RegimeResult:
        return RegimeResult(
            regime=regime, rules=REGIME_RULES[regime],
            atr=atr, atr_mean=atr_mean, atr_ratio=atr_ratio,
            adx=adx, description=desc,
        )

    if use_atr_high_vol and atr_ratio > 1.8:
        return make("HIGH_VOLATILITY", f"ATR {atr_ratio:.2f}× above mean")

    if atr_ratio < 0.60 and adx < 20:
        return make("LOW_VOLATILITY", f"ATR at {atr_ratio*100:.0f}% of mean + ADX {adx}")

    if adx >= 30 and trend.strength == "STRONG":
        if trend.trend == "BULLISH":
            return make("STRONG_TREND_BULL", f"ADX {adx} — all EMAs aligned bull")
        if trend.trend == "BEARISH":
            return make("STRONG_TREND_BEAR", f"ADX {adx} — all EMAs aligned bear")

    pull = _detect_pullback(candles, trend)
    if pull == "BULL" and adx >= 20:
        return make("PULLBACK_BULL", "Bear retracement within bull trend")
    if pull == "BEAR" and adx >= 20:
        return make("PULLBACK_BEAR", "Bull retracement within bear trend")

    if adx >= 20 and trend.trend != "NEUTRAL":
        if trend.trend == "BULLISH":
            return make("WEAK_TREND_BULL", f"ADX {adx} — developing bull trend")
        return make("WEAK_TREND_BEAR", f"ADX {adx} — developing bear trend")

    # A Wyckoff phase is context, not a directional veto by itself.  The
    # Wyckoff engine deliberately keeps ``wyckoff_signal`` neutral until both
    # the directional event (Spring/Upthrust) and directional volume are
    # confirmed.  Using ``phase`` alone here was making an unresolved range
    # block a valid trend setup (for example: "Distribution does not allow
    # LONG") even though Wyckoff had not produced a SELL vote.
    if (
        wyckoff.phase == "ACCUMULATION"
        and wyckoff.wyckoff_signal == "BUY"
    ):
        return make("ACCUMULATION", "Wyckoff Accumulation" +
                    (" + Spring" if wyckoff.spring else ""))
    if (
        wyckoff.phase == "DISTRIBUTION"
        and wyckoff.wyckoff_signal == "SELL"
    ):
        return make("DISTRIBUTION", "Wyckoff Distribution" +
                    (" + Upthrust" if wyckoff.upthrust else ""))

    return make(
        "RANGE",
        f"ADX {adx} < 20 — ranging / choppy "
        "(Wyckoff phase unconfirmed)",
    )
