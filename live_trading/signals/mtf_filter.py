"""
Multi-Timeframe (HTF) Filter — GoldScalperPro v4

Computes a Higher TimeFrame directional bias by reusing the existing
Trend, SMC, Wyckoff, and Regime engines on HTF candles (default H1).
The live loop uses this bias as an additional trade gate:

  • Only BUY entries allowed when HTF bias is BUY.
  • Only SELL entries allowed when HTF bias is SELL.
  • NEUTRAL bias → no filter applied (both directions pass through).

Design principles:
  • Zero risk: any fetch/analysis error → NEUTRAL → trade is NOT blocked.
    MTF failure can never stop a valid M5 trade; it only ever blocks bad ones.
  • Additive: zero changes to any existing signal engine or decision logic.
    This module is a pure add-on — it imports from the existing engines but
    never modifies them.
  • Fail-safe precedence: Trend must be non-NEUTRAL for a bias to be issued.
    If Trend is neutral but SMC has a strong signal, we stay NEUTRAL rather
    than issuing a bias from SMC alone (SMC is noisier on higher timeframes
    without the EMA anchor).
  • Conflict suppression: if Trend and SMC actively disagree, bias = NEUTRAL.
    A conflicted HTF means the market is transitioning — we do not filter.
  • Configurable: MTF_ENABLED / MTF_TIMEFRAME / MTF_CANDLE_WINDOW env vars.
  • Transparent: all reasoning is recorded in MtfBias.reasoning for logging
    and panel display.

Typical usage (inside live_loop._on_new_bar):

    htf_candles = await fetch_candles(SYMBOL, MTF_TIMEFRAME, MTF_CANDLE_WINDOW)
    htf_bias    = compute_mtf_bias(htf_candles)          # never raises
    allowed, reason = mtf_allows_trade(htf_bias, decision.direction)
    if not allowed:
        log.info(f"MTF BLOCK: {reason}")
        return
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.trend_engine import analyze_trend, TrendResult
from live_trading.signals.smc_engine import analyze_smc_structure, SmcResult
from live_trading.signals.wyckoff_engine import analyze_wyckoff, WyckoffResult
from live_trading.signals.market_regime import detect_market_regime, RegimeResult


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class MtfBias:
    """
    HTF directional bias produced by compute_mtf_bias().

    direction : BUY | SELL | NEUTRAL
        The bias to apply.  NEUTRAL means "no filter" — both M5 directions
        are allowed.  Never treat NEUTRAL as bearish.

    trend     : HTF EMA trend string (BULLISH / BEARISH / NEUTRAL).
    smc_signal: HTF SMC signal       (BUY / SELL / NEUTRAL).
    regime    : HTF regime label     (e.g. STRONG_TREND_BULL, RANGE, …).
    strength  : STRONG | MODERATE | WEAK — derived from trend engine.
    reasoning : Ordered list of human-readable explanation strings.  Each
                element explains one decision step; the last element is
                always the summary ("Bias CONFIRMED: …" or "HTF CONFLICT: …").
    """
    direction:  Literal["BUY", "SELL", "NEUTRAL"]
    trend:      str
    smc_signal: str
    regime:     str
    strength:   Literal["STRONG", "MODERATE", "WEAK"]
    reasoning:  List[str] = field(default_factory=list)


def _neutral(reason: str) -> MtfBias:
    """Fast-path helper: return a NEUTRAL bias with a single-element reasoning."""
    return MtfBias(
        direction="NEUTRAL", trend="NEUTRAL",
        smc_signal="NEUTRAL", regime="RANGE",
        strength="WEAK", reasoning=[reason],
    )


# ── Core computation ──────────────────────────────────────────────────────────

def compute_mtf_bias(htf_candles: List[OHLCV]) -> MtfBias:
    """
    Derive the HTF directional bias from Trend + SMC + Regime analysis.

    NEVER raises — any exception produces a NEUTRAL bias (fail-safe).
    Returns NEUTRAL when data is insufficient or engines conflict.

    Parameters
    ----------
    htf_candles : list of OHLCV
        Closed candles on the HTF (e.g. H1).  Callers should pass at least
        200 bars so the trend engine can compute EMA-200 reliably; 300 is
        the recommended default (MTF_CANDLE_WINDOW env var).

    Returns
    -------
    MtfBias
        Always non-None.  Check .direction for BUY / SELL / NEUTRAL.
    """
    if len(htf_candles) < 50:
        return _neutral(f"Insufficient HTF candles ({len(htf_candles)} < 50)")

    try:
        trend   : TrendResult  = analyze_trend(htf_candles)
        smc     : SmcResult    = analyze_smc_structure(htf_candles)
        wyckoff : WyckoffResult = analyze_wyckoff(htf_candles)
        regime  : RegimeResult  = detect_market_regime(
            htf_candles, trend, wyckoff, use_atr_high_vol=False
        )
    except Exception as exc:   # noqa: BLE001
        return _neutral(f"HTF analysis error (fail-safe): {exc}")

    reasoning: List[str] = []

    # ── 1. Trend vote (primary anchor) ───────────────────────────────────────
    # EMA alignment is the most reliable HTF signal for XAUUSD intraday.
    if trend.trend == "BULLISH":
        trend_vote = "BUY"
        reasoning.append(
            f"HTF EMA aligned BULLISH "
            f"(EMA50={trend.ema50:.2f} > EMA100={trend.ema100:.2f}, "
            f"strength={trend.strength})"
        )
    elif trend.trend == "BEARISH":
        trend_vote = "SELL"
        reasoning.append(
            f"HTF EMA aligned BEARISH "
            f"(EMA50={trend.ema50:.2f} < EMA100={trend.ema100:.2f}, "
            f"strength={trend.strength})"
        )
    else:
        trend_vote = "NEUTRAL"
        reasoning.append(
            f"HTF EMA trend NEUTRAL "
            f"(EMA50={trend.ema50:.2f}, EMA100={trend.ema100:.2f}, "
            f"EMA200={trend.ema200:.2f}) — no alignment"
        )

    # ── 2. SMC vote (structural confirmation) ────────────────────────────────
    # HTF SMC gives structural confirmation (BOS, CHoCH, OB), but is
    # NOT allowed to override a neutral trend — it can only reinforce.
    smc_vote = smc.smc_signal   # BUY | SELL | NEUTRAL
    if smc_vote != "NEUTRAL":
        reasoning.append(
            f"HTF SMC signal: {smc_vote} "
            f"(BOS={len(smc.bos_signals)}, "
            f"CHoCH={len(smc.choch_signals)}, "
            f"OBs={len(smc.order_blocks)}, "
            f"FVGs={len(smc.fair_value_gaps)})"
        )
    else:
        reasoning.append("HTF SMC: no structural direction signal")

    # ── 3. Regime context (informational) ────────────────────────────────────
    reasoning.append(
        f"HTF regime: {regime.regime} "
        f"(ADX={regime.adx:.1f}, ATR_ratio={regime.atr_ratio:.2f})"
    )

    # ── 4. Combine → final bias ───────────────────────────────────────────────
    # Rule matrix:
    #   Trend NEUTRAL            → NEUTRAL (no filter regardless of SMC)
    #   Trend directional + SMC agrees or NEUTRAL → bias = trend direction
    #   Trend directional + SMC actively opposes  → NEUTRAL (conflicted HTF)
    if trend_vote == "NEUTRAL":
        direction : Literal["BUY", "SELL", "NEUTRAL"] = "NEUTRAL"
        strength  : Literal["STRONG", "MODERATE", "WEAK"] = "WEAK"
        reasoning.append(
            "HTF bias: NEUTRAL (trend not aligned — no M5 filter applied)"
        )
    elif smc_vote == "NEUTRAL" or smc_vote == trend_vote:
        direction = trend_vote  # type: ignore[assignment]
        strength  = trend.strength
        smc_note  = "SMC confirms" if smc_vote == trend_vote else "SMC neutral"
        reasoning.append(
            f"Bias CONFIRMED: {direction} ({smc_note}, strength={strength})"
        )
    else:
        # Trend and SMC conflict — HTF is transitioning; pass-through
        direction = "NEUTRAL"
        strength  = "WEAK"
        reasoning.append(
            f"HTF CONFLICT: Trend={trend_vote} vs SMC={smc_vote} "
            f"— bias NEUTRAL, no M5 filter applied"
        )

    return MtfBias(
        direction=direction,
        trend=trend.trend,
        smc_signal=smc_vote,
        regime=regime.regime,
        strength=strength,
        reasoning=reasoning,
    )


# ── Gate helper ───────────────────────────────────────────────────────────────

def mtf_allows_trade(
    bias: Optional[MtfBias],
    m5_direction: str,
) -> tuple[bool, str]:
    """
    Check whether a proposed M5 trade is aligned with the HTF bias.

    Parameters
    ----------
    bias          : MtfBias | None
        The result of compute_mtf_bias().  None is treated as NEUTRAL.
    m5_direction  : str
        The M5 decision engine's proposed direction: "BUY", "SELL",
        or "NEUTRAL".

    Returns
    -------
    (allowed, reason)
        allowed=True  → trade may proceed (HTF aligned or NEUTRAL).
        allowed=False → caller should block the trade; reason explains why.
        When allowed=True, reason is an empty string.

    This function NEVER raises.
    """
    # Fail-safe: no bias or NEUTRAL bias → always pass through
    if bias is None or bias.direction == "NEUTRAL":
        return True, ""

    # M5 direction NEUTRAL → no trade anyway; don't block explicitly
    if m5_direction == "NEUTRAL":
        return True, ""

    if m5_direction == bias.direction:
        return True, ""

    reason = (
        f"MTF BLOCK: M5 wants {m5_direction} but HTF bias is {bias.direction} "
        f"[HTF trend={bias.trend}, SMC={bias.smc_signal}, "
        f"regime={bias.regime}, strength={bias.strength}]"
    )
    return False, reason
