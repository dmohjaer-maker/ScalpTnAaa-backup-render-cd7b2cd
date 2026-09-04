"""
Decision Engine — Central orchestrator of all 7 signal engines.
Ported from decisionEngine.ts
"""
from dataclasses import dataclass, field
from typing import List, Literal, Optional
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import (
    SmcBos,
    SmcChoch,
    SmcLiquiditySweep,
    SmcResult,
    analyze_smc_structure,
    detect_order_block_fake_breakout,
    get_latest_structure_event,
)
from live_trading.signals.wyckoff_engine import WyckoffResult, analyze_wyckoff
from live_trading.signals.price_action_engine import PriceActionResult, analyze_price_action
from live_trading.signals.trend_engine import TrendResult, analyze_trend
from live_trading.signals.market_regime import RegimeResult, RegimeEntryRules, detect_market_regime
from live_trading.signals.confidence_engine import ConfidenceResult, ConfidenceComponents, calc_confidence
from live_trading.signals.quality_filter import QualityFilterResult, apply_quality_filter, get_session_quality
from live_trading.signals.entry_filter import apply_entry_filter, EntryFilterResult
from live_trading.signals.divergence_engine import analyze_divergence, DivergenceResult
from live_trading.trading.entry_guards import validate_directional_alignment
from live_trading.risk.capital_manager import (
    CapitalInput, CapitalOutput, REQUIRED_ENTRY_RR, calc_trade_parameters,
)
from live_trading.config import (
    CONF_HARD_MIN,
    REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
    ENTRY_TRIGGER_MAX_AGE_BARS,
    STRICT_ENTRY_MODE,
)

# Marginal confidence R:R floor: trades with confidence between CONF_HARD_MIN
# and the regime minimum must still achieve this R:R to be allowed.
# 1.3 = profitable in expectancy even at 45% win rate (1.3 × 0.45 > 0.55).
CONF_MARGINAL_RR = 1.3


def _recent_micro_levels(candles: List[OHLCV], lookback: int = 12) -> tuple[Optional[float], Optional[float]]:
    """Return the latest confirmed short-term swing high/low.

    Two candles on each side confirm a pivot. The fallback uses only the last
    six completed candles, so a stale historical swing cannot widen a scalp.
    """
    if len(candles) < 5:
        return None, None
    last = len(candles) - 1
    start = max(2, last - lookback)
    end = last - 2
    highs: List[float] = []
    lows: List[float] = []
    for i in range(start, end + 1):
        window = candles[i - 2:i + 3]
        if candles[i].high >= max(c.high for c in window):
            highs.append(candles[i].high)
        if candles[i].low <= min(c.low for c in window):
            lows.append(candles[i].low)
    if not highs:
        highs = [c.high for c in candles[max(0, last - 6):last]]
    if not lows:
        lows = [c.low for c in candles[max(0, last - 6):last]]
    return (highs[-1] if highs else None, lows[-1] if lows else None)


def _has_fresh_entry_trigger(
    smc: SmcResult,
    pa_signal: str,
    candidate: str,
    current_bar_index: int,
    max_age_bars: int,
) -> bool:
    """Return True only when the closed-candle setup has a fresh trigger."""
    latest_structure = get_latest_structure_event(smc)
    structure_trigger = (
        latest_structure is not None
        and latest_structure.type == candidate
        and 0 <= current_bar_index - latest_structure.bar_index <= max_age_bars
    )
    sweep_type = "BULLISH" if candidate == "BUY" else "BEARISH"
    sweep_trigger = any(
        sweep.type == sweep_type and sweep.bar_index == current_bar_index
        for sweep in smc.liquidity_sweeps
    )
    return structure_trigger or sweep_trigger or pa_signal == candidate


def _liquidity_sweep_reason(
    candles: List[OHLCV],
    smc: SmcResult,
    candidate: str,
    confirmation_max_age: int = 3,
) -> Optional[str]:
    """Return a block reason when a fresh liquidity sweep lacks confirmation.

    A sweep is a liquidity event, not a complete entry signal.  Require the
    next closed candle to keep its close beyond the swept level with a
    directional body.  A sweep older than a small bounded window is ignored so
    it cannot veto an unrelated later setup.
    """
    if candidate not in {"BUY", "SELL"} or not candles:
        return None

    expected_type = "BULLISH" if candidate == "BUY" else "BEARISH"
    aligned_sweeps = [
        sweep for sweep in smc.liquidity_sweeps
        if sweep.type == expected_type
    ]
    if not aligned_sweeps:
        return None

    latest = max(aligned_sweeps, key=lambda sweep: sweep.bar_index)
    current_index = len(candles) - 1
    bars_after = current_index - latest.bar_index
    if bars_after < 0 or bars_after > confirmation_max_age:
        return None
    if bars_after == 0:
        return (
            f"Liquidity sweep pending confirmation: {candidate} sweep needs "
            "one subsequent closed candle"
        )

    confirmation_index = latest.bar_index + 1
    if confirmation_index >= len(candles):
        return "Liquidity sweep guard: missing confirmation candle"

    confirmation = candles[confirmation_index]
    confirmation_range = confirmation.high - confirmation.low
    body_ratio = (
        abs(confirmation.close - confirmation.open) / confirmation_range
        if confirmation_range > 0 else 0.0
    )
    directional_body = (
        confirmation.close > confirmation.open
        if candidate == "BUY"
        else confirmation.close < confirmation.open
    )
    held_level = (
        confirmation.close > latest.swept_level
        if candidate == "BUY"
        else confirmation.close < latest.swept_level
    )
    if not directional_body or not held_level or body_ratio < 0.30:
        return (
            f"False liquidity sweep detected: {candidate} sweep lacked "
            "directional follow-through"
        )

    crossed_back = any(
        (c.close <= latest.swept_level if candidate == "BUY"
         else c.close >= latest.swept_level)
        for c in candles[confirmation_index:]
    )
    if crossed_back:
        return (
            f"False liquidity sweep detected: price closed back across the "
            f"{candidate} swept level"
        )
    return None


def _bos_follow_through_reason(
    candles: List[OHLCV],
    smc: SmcResult,
    candidate: str,
    confirmation_max_age: int = 3,
) -> Optional[str]:
    """Return a block reason when the latest BOS lacks continuation.

    A BOS is only actionable when the next closed candle accepts the broken
    level.  This prevents a single wide candle or a stop-hunt close from
    authorizing an entry that immediately loses the structure.
    """
    if candidate not in {"BUY", "SELL"} or not candles:
        return None

    latest = get_latest_structure_event(smc)
    if not isinstance(latest, SmcBos) or latest.type != candidate:
        return None

    current_index = len(candles) - 1
    bars_after = current_index - latest.bar_index
    if bars_after < 0 or bars_after > confirmation_max_age:
        return None
    if bars_after == 0:
        return (
            f"BOS pending confirmation: {candidate} breakout needs "
            "one subsequent closed candle"
        )

    confirmation_index = latest.bar_index + 1
    if confirmation_index >= len(candles):
        return "BOS guard: missing confirmation candle"

    confirmation = candles[confirmation_index]
    confirmation_range = confirmation.high - confirmation.low
    body_ratio = (
        abs(confirmation.close - confirmation.open) / confirmation_range
        if confirmation_range > 0 else 0.0
    )
    directional_body = (
        confirmation.close > confirmation.open
        if candidate == "BUY"
        else confirmation.close < confirmation.open
    )
    held_level = (
        confirmation.close > latest.price
        if candidate == "BUY"
        else confirmation.close < latest.price
    )
    if not directional_body or not held_level or body_ratio < 0.35:
        return (
            f"False BOS detected: {candidate} breakout lacked "
            "directional follow-through"
        )

    crossed_back = any(
        (c.close <= latest.price if candidate == "BUY"
         else c.close >= latest.price)
        for c in candles[confirmation_index:]
    )
    if crossed_back:
        return (
            f"False BOS detected: price closed back across the "
            f"{candidate} breakout level"
        )
    return None


def _breakout_follow_through_reason(
    candles: List[OHLCV],
    pa: PriceActionResult,
    candidate: str,
    retest_bars: int = 4,
) -> Optional[str]:
    """Require a retest or two closes for a Price Action breakout.

    ``valid_*_breakout`` is intentionally a signal, not proof that a level
    has been accepted.  A breakout is actionable only after either two
    consecutive closes beyond its level or a later candle that retests the
    level and closes away from it with a directional body.
    """
    if candidate == "BUY":
        is_breakout = pa.valid_bull_breakout
        level = pa.bull_breakout_level
    elif candidate == "SELL":
        is_breakout = pa.valid_bear_breakout
        level = pa.bear_breakout_level
    else:
        return None

    if not is_breakout or level is None or len(candles) < 2:
        return None

    current = candles[-1]
    previous = candles[-2]
    if (
        (candidate == "BUY" and previous.close > level and current.close > level)
        or
        (candidate == "SELL" and previous.close < level and current.close < level)
    ):
        return None

    current_index = len(candles) - 1
    start = max(0, current_index - retest_bars - 1)
    breakout_bar: Optional[int] = None
    for i in range(start, current_index):
        if (
            candidate == "BUY"
            and candles[i].close <= level
            and candles[i + 1].close > level
        ):
            breakout_bar = i + 1
        elif (
            candidate == "SELL"
            and candles[i].close >= level
            and candles[i + 1].close < level
        ):
            breakout_bar = i + 1

    if breakout_bar is not None:
        for candle in candles[breakout_bar + 1:]:
            candle_range = candle.high - candle.low
            body_ratio = (
                abs(candle.close - candle.open) / candle_range
                if candle_range > 0 else 0.0
            )
            directional_body = (
                candle.close > candle.open
                if candidate == "BUY"
                else candle.close < candle.open
            )
            touched_level = (
                candle.low <= level if candidate == "BUY"
                else candle.high >= level
            )
            held_level = (
                candle.close > level if candidate == "BUY"
                else candle.close < level
            )
            if touched_level and held_level and directional_body and body_ratio >= 0.30:
                return None

    return (
        f"Breakout pending confirmation: {candidate} needs a successful "
        "retest or two consecutive closes beyond the level"
    )


def _weak_volume_breakout_reason(
    candles: List[OHLCV],
    smc: SmcResult,
    pa: PriceActionResult,
    candidate: str,
    is_weak_volume: bool,
) -> Optional[str]:
    """Block only breakout setups when current volume is exceptionally weak."""
    if not is_weak_volume or candidate not in {"BUY", "SELL"}:
        return None

    pa_breakout = (
        candidate == "BUY" and pa.valid_bull_breakout
    ) or (
        candidate == "SELL" and pa.valid_bear_breakout
    )

    latest_structure = get_latest_structure_event(smc)
    current_index = len(candles) - 1
    bos_breakout = (
        isinstance(latest_structure, SmcBos)
        and latest_structure.type == candidate
        and 0 <= current_index - latest_structure.bar_index <= 3
    )

    if not pa_breakout and not bos_breakout:
        return None

    return (
        f"Very weak volume on {candidate} breakout — current volume is "
        "below 35% of the 20-bar average; entry blocked"
    )


def _false_reversal_reason(
    candles: List[OHLCV],
    smc: SmcResult,
    candidate: str,
) -> Optional[str]:
    """Return a block reason when a CHoCH fails its first follow-through.

    A CHoCH is a reversal *candidate*, not proof that the new direction will
    hold.  Require one subsequent closed candle to stay beyond the broken
    structure level, with a directional body.  If price closes back across
    that level during the first few bars, classify the move as a false
    reversal and keep it out of the order executor.
    """
    latest = get_latest_structure_event(smc)
    if not isinstance(latest, SmcChoch) or latest.type != candidate:
        return None

    current_index = len(candles) - 1
    bars_after = current_index - latest.bar_index
    if bars_after < 0:
        return "False reversal guard: invalid CHoCH bar index"
    if bars_after == 0:
        return (
            f"Reversal pending confirmation: {candidate} CHoCH needs "
            "one subsequent closed candle"
        )

    confirmation = candles[latest.bar_index + 1] \
        if latest.bar_index + 1 < len(candles) else None
    if confirmation is None:
        return "False reversal guard: missing confirmation candle"

    confirmation_range = confirmation.high - confirmation.low
    confirmation_body_ratio = (
        abs(confirmation.close - confirmation.open) / confirmation_range
        if confirmation_range > 0 else 0.0
    )
    directional_body = (
        confirmation.close > confirmation.open
        if candidate == "BUY"
        else confirmation.close < confirmation.open
    )
    held_level = (
        confirmation.close > latest.price
        if candidate == "BUY"
        else confirmation.close < latest.price
    )
    if not directional_body or not held_level or confirmation_body_ratio < 0.30:
        return (
            f"False reversal detected: {candidate} CHoCH lacked "
            "directional follow-through"
        )

    # A reclaim of the broken level shortly after the CHoCH invalidates the
    # reversal even if the first confirmation candle looked acceptable.
    recent_after = candles[latest.bar_index + 1:]
    crossed_back = any(
        (c.close <= latest.price if candidate == "BUY"
         else c.close >= latest.price)
        for c in recent_after
    )
    if crossed_back:
        return (
            f"False reversal detected: price closed back across the "
            f"{candidate} CHoCH level"
        )
    return None


@dataclass
class DecisionResult:
    allowed:         bool
    direction:       Literal["BUY", "SELL", "NEUTRAL"]
    confidence:      float
    components:      ConfidenceComponents
    grade:           str
    regime:          str
    regime_label:    str
    regime_rules:    RegimeEntryRules
    quality_filter:  QualityFilterResult
    blocked_reasons: List[str]
    reasoning:       List[str]
    trade_params:    Optional[CapitalOutput]
    smc:    SmcResult
    wyckoff: WyckoffResult
    pa:     PriceActionResult
    trend:  TrendResult
    # Additive, optional — which of the 4 independent engines (SMC/Trend/
    # PriceAction/Wyckoff) voted for this trade's direction. None on the
    # early "no SMC signal" path, where the vote was never computed.
    # Existing callers that construct/consume DecisionResult are unaffected
    # since this has a default and nothing reads it unless it asks for it.
    entry_filter:    Optional[EntryFilterResult] = None
    divergence:      Optional[DivergenceResult]  = None
    dxy_signal:      str                         = "NEUTRAL"


def _candidate_direction(
    smc: SmcResult,
    pa: Optional[PriceActionResult] = None,
    trend: Optional[TrendResult] = None,
    wyckoff: Optional[WyckoffResult] = None,
) -> str:
    """Choose direction from non-SMC context first, with SMC as a fallback.

    SMC is used as a fallback for candidate selection. Neutral SMC does not
    suppress a setup supported by the local EMA trend and other confirmations,
    while the directional alignment guard below vetoes conflicting SMC context.
    """
    if trend is not None:
        if trend.trend == "BULLISH":
            return "BUY"
        if trend.trend == "BEARISH":
            return "SELL"

    non_smc_votes = [
        signal for signal in (
            pa.pa_signal if pa is not None else "NEUTRAL",
            wyckoff.wyckoff_signal if wyckoff is not None else "NEUTRAL",
        )
        if signal in {"BUY", "SELL"}
    ]
    if non_smc_votes:
        if non_smc_votes.count("BUY") > non_smc_votes.count("SELL"):
            return "BUY"
        if non_smc_votes.count("SELL") > non_smc_votes.count("BUY"):
            return "SELL"

    # Use the newest event across both lists only as a last-resort fallback.
    # Prioritising the last CHoCH unconditionally can resurrect an old
    # reversal against a newer BOS.
    latest_structure = get_latest_structure_event(smc)
    if latest_structure is not None:
        return latest_structure.type
    if smc.trend == "BULLISH": return "BUY"
    if smc.trend == "BEARISH": return "SELL"
    return "NEUTRAL"


def _smc_direction_conflict_reason(smc: SmcResult, candidate: str) -> Optional[str]:
    """Return a fail-closed reason when the candidate is not SMC-confirmed.

    The candidate direction is used for freshness/event handling, while
    ``smc.smc_signal`` is the composite SMC verdict. They must agree before
    the entry filter can count SMC as a vote; otherwise a stale or weak BOS/
    CHoCH could be promoted into a trade even when the composite is NEUTRAL.
    """
    if candidate not in {"BUY", "SELL"}:
        return None
    if smc.smc_signal == candidate:
        return None
    if smc.smc_signal == "NEUTRAL":
        return (
            f"SMC composite is NEUTRAL while structure candidate is {candidate} "
            "— entry blocked"
        )
    return (
        f"SMC direction conflict: structure candidate {candidate} vs "
        f"composite {smc.smc_signal} — entry blocked"
    )


def _make_neutral(
    smc, wyckoff, pa, trend, blocked_reasons, reasoning=None,
    dxy_signal: str = "NEUTRAL",
) -> DecisionResult:
    from live_trading.signals.market_regime import REGIME_RULES
    rules = REGIME_RULES["RANGE"]
    return DecisionResult(
        allowed=False, direction="NEUTRAL", confidence=0.0,
        components=ConfidenceComponents(0,0,0,0,0,0,0),
        grade="REJECTED", regime="RANGE", regime_label="No Signal",
        regime_rules=rules,
        quality_filter=QualityFilterResult(
            allowed=False, blocked_reasons=blocked_reasons,
            session_quality="BLOCKED", adx=0.0,
            is_severe_range=False, is_late_entry=False,
            is_low_probability=False, is_fake_breakout=False,
            is_weak_volume=False, is_low_momentum=False,
        ),
        blocked_reasons=blocked_reasons,
        reasoning=reasoning or [],
        trade_params=None,
        smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
        dxy_signal=dxy_signal,
    )


def run_decision_engine(
    candles:           List[OHLCV],
    account_balance:   float,
    risk_percent:      float = 1.0,
    min_confirmations: int   = 1,
    use_atr_high_vol:  bool  = False,
    dxy_signal:        str   = "NEUTRAL",
    require_price_action: bool = False,
    require_smc_price_action_wyckoff: bool = REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
    entry_price_override: Optional[float] = None,
    spread: float = 0.0,
    symbol: str = "XAUUSD",
) -> DecisionResult:

    smc     = analyze_smc_structure(candles)
    wyckoff = analyze_wyckoff(candles)
    pa      = analyze_price_action(candles)
    trend   = analyze_trend(candles)

    candidate = _candidate_direction(smc, pa, trend, wyckoff)
    if candidate == "NEUTRAL":
        return _make_neutral(
            smc, wyckoff, pa, trend,
            ["No directional Trend, Price Action, or Wyckoff signal"],
        )

    # Hard trend gate: a trade must follow the local EMA trend.
    # Counter-trend and unresolved (NEUTRAL) setups are rejected before
    # confidence, regime, or confirmation votes are evaluated.
    trend_dir = ("BUY" if trend.trend == "BULLISH" else
                 "SELL" if trend.trend == "BEARISH" else "NEUTRAL")
    if trend_dir == "NEUTRAL":
        trend_reason = "Trend filter: EMA trend is NEUTRAL — entry blocked"
        return _make_neutral(
            smc, wyckoff, pa, trend, [trend_reason], [trend_reason]
        )
    if trend_dir != candidate:
        trend_reason = (
            f"Trend filter: {candidate} conflicts with EMA trend "
            f"{trend.trend} — counter-trend entry blocked"
        )
        return _make_neutral(
            smc, wyckoff, pa, trend, [trend_reason], [trend_reason]
        )

    # SMC is a hard directional veto. It remains optional when neutral, but a
    # confirmed opposing structure or composite signal is never allowed to
    # authorize a counter-trend entry.
    _alignment_ok, _alignment_reason = validate_directional_alignment(
        candidate,
        local_trend=trend.trend,
        smc_trend=smc.trend,
        smc_signal=smc.smc_signal,
    )
    if not _alignment_ok:
        return _make_neutral(
            smc, wyckoff, pa, trend,
            [_alignment_reason], [_alignment_reason],
        )

    # Detect regime early — needed to set the adaptive confirmation threshold.
    # RANGE / ACCUMULATION / DISTRIBUTION / HIGH_VOLATILITY markets suppress
    # PA and Wyckoff signals by design, so we lower the bar to 2 in those
    # regimes. Trending regimes keep the stricter operator-configured value.
    regime = detect_market_regime(candles, trend, wyckoff, use_atr_high_vol)

    # DXY is an active hard directional veto for gold. The feed fails open to
    # NEUTRAL, so an outage never blocks trading; only a confirmed opposing
    # dollar trend blocks the corresponding gold direction.
    if (
        (candidate == "BUY" and dxy_signal == "BULLISH_DXY")
        or (candidate == "SELL" and dxy_signal == "BEARISH_DXY")
    ):
        dxy_reason = (
            f"DXY filter: {dxy_signal} opposes gold {candidate} — entry blocked"
        )
        return _make_neutral(
            smc, wyckoff, pa, trend, [dxy_reason], [dxy_reason],
            dxy_signal=dxy_signal,
        )

    _RANGE_REGIMES = {"RANGE", "ACCUMULATION", "DISTRIBUTION", "HIGH_VOLATILITY"}
    if regime.regime in _RANGE_REGIMES:
        # Range/volatile regimes: require one extra confirmation over the base
        # minimum.  Structural signals alone (e.g. SMC + Wyckoff without EMA
        # trend or PA) are insufficient in choppy/ranging markets — at least
        # one momentum engine must also agree to avoid repeated SL hits.
        effective_min_confirmations = min(min_confirmations + 1, 4)
    else:
        effective_min_confirmations = min_confirmations

    # Entry filter — minimum confirmation gate. SMC contributes only when it
    # agrees; it is not required and cannot veto the non-SMC setup.
    ef = apply_entry_filter(
        smc_signal      = smc.smc_signal,
        ema_trend       = trend.trend,
        pa_signal       = pa.pa_signal,
        wyckoff_signal  = wyckoff.wyckoff_signal,
        min_confirmations = effective_min_confirmations,
        require_price_action = require_price_action,
        require_smc_price_action_wyckoff = require_smc_price_action_wyckoff,
        require_trend_alignment = True,
        candidate_direction = candidate,
    )
    if not ef.allowed:
        votes = (f"SMC={'✓' if ef.smc else '✗'}  "
                 f"Trend={'✓' if ef.trend else '✗'}  "
                 f"PA={'✓' if ef.price_action else '✗'}  "
                 f"Wyckoff={'✓' if ef.wyckoff else '✗'}")
        if (
            require_smc_price_action_wyckoff
            and not (ef.price_action and ef.wyckoff)
        ):
            reason = (
                "Entry filter: strict mode requires Price Action + Wyckoff "
                "(SMC is optional when neutral) — "
                f"{votes}  [regime={regime.regime}]"
            )
        elif require_price_action and not ef.price_action:
            reason = (f"Entry filter: Price Action confirmation required — "
                      f"{votes}  [regime={regime.regime}]")
        else:
            reason = (f"Entry filter: only {ef.confirmation_count}/{effective_min_confirmations} "
                      f"confirmations — {votes}  [regime={regime.regime}]")
        return _make_neutral(smc, wyckoff, pa, trend, [reason], [reason])

    # Exact entry trigger: a fresh setup must be confirmed by the latest
    # closed candle. This prevents a stale BOS plus static confirmations from
    # opening a market order many bars after the actual opportunity passed.
    if STRICT_ENTRY_MODE and not _has_fresh_entry_trigger(
        smc, pa.pa_signal, candidate, len(candles) - 1, ENTRY_TRIGGER_MAX_AGE_BARS
    ):
        reason = (
            f"Entry trigger missing: no fresh {candidate} structure/sweep/price-action "
            f"trigger within {ENTRY_TRIGGER_MAX_AGE_BARS} bars"
        )
        return _make_neutral(smc, wyckoff, pa, trend, [reason], [reason])

    false_reversal_reason = _false_reversal_reason(candles, smc, candidate)
    if false_reversal_reason:
        return _make_neutral(
            smc, wyckoff, pa, trend,
            [false_reversal_reason], [false_reversal_reason],
        )

    liquidity_sweep_reason = _liquidity_sweep_reason(candles, smc, candidate)
    if liquidity_sweep_reason:
        return _make_neutral(
            smc, wyckoff, pa, trend,
            [liquidity_sweep_reason], [liquidity_sweep_reason],
        )

    bos_follow_through_reason = _bos_follow_through_reason(
        candles, smc, candidate
    )
    if bos_follow_through_reason:
        return _make_neutral(
            smc, wyckoff, pa, trend,
            [bos_follow_through_reason], [bos_follow_through_reason],
        )

    breakout_follow_through_reason = _breakout_follow_through_reason(
        candles, pa, candidate
    )
    if breakout_follow_through_reason:
        return _make_neutral(
            smc, wyckoff, pa, trend,
            [breakout_follow_through_reason],
            [breakout_follow_through_reason],
        )

    if candidate == "BUY"  and not regime.rules.allow_long:
        return _make_neutral(smc, wyckoff, pa, trend,
                             [f'Regime "{regime.rules.label}" does not allow LONG'])
    if candidate == "SELL" and not regime.rules.allow_short:
        return _make_neutral(smc, wyckoff, pa, trend,
                             [f'Regime "{regime.rules.label}" does not allow SHORT'])

    last_candle  = candles[-1]
    session      = get_session_quality(last_candle.time)
    divergence   = analyze_divergence(candles)
    conf_result  = calc_confidence(
        smc, wyckoff, pa, trend, regime, session, candidate,
        divergence_signal=divergence.signal,
        dxy_signal=dxy_signal,
    )

    if conf_result.confidence < CONF_HARD_MIN:
        n = DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade="REJECTED", regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=regime.rules,
            quality_filter=QualityFilterResult(
                False, [f"Confidence {conf_result.confidence:.1f}% < {CONF_HARD_MIN}% minimum"],
                session, regime.adx, False, False, True, False, False, False),
            blocked_reasons=[f"Confidence {conf_result.confidence:.1f}% < {CONF_HARD_MIN}%"],
            reasoning=conf_result.reasoning, trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
            dxy_signal=dxy_signal,
        )
        return n

    latest_structure = get_latest_structure_event(smc)
    last_structure_bar = (latest_structure.bar_index
                          if latest_structure is not None else None)
    # Feed the newest BOS/CHoCH bar through the existing quality-filter slot
    # so both event types share the same freshness gate.
    quality  = apply_quality_filter(candles, candidate, conf_result.confidence,
                                    last_structure_bar, regime.adx, regime.atr_ratio)
    if not quality.allowed:
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=regime.rules, quality_filter=quality,
            blocked_reasons=quality.blocked_reasons, reasoning=conf_result.reasoning,
            trade_params=None, smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
        )

    weak_volume_reason = _weak_volume_breakout_reason(
        candles, smc, pa, candidate, quality.is_weak_volume
    )
    if weak_volume_reason:
        quality.allowed = False
        quality.blocked_reasons.append(weak_volume_reason)
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime,
            regime_label=regime.rules.label, regime_rules=regime.rules,
            quality_filter=quality,
            blocked_reasons=[weak_volume_reason],
            reasoning=conf_result.reasoning + [weak_volume_reason],
            trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
            divergence=divergence,
            dxy_signal=dxy_signal,
        )

    # Option 2 hard gate: a directional breakout that closes back inside its
    # aligned Order Block is treated as a fake breakout.  Do not let this
    # setup reach capital sizing or the order executor.  The existing quality
    # result is reused so the panel receives the normal filter telemetry plus
    # the explicit rejection flag.
    fake_ob = detect_order_block_fake_breakout(candles, smc, candidate)
    if fake_ob is not None:
        fake_reason = (
            f"Fake Breakout in {fake_ob.type.title()} Order Block "
            f"[{fake_ob.low:.2f}, {fake_ob.high:.2f}] — entry blocked"
        )
        quality.allowed = False
        quality.is_fake_breakout = True
        quality.blocked_reasons.append(fake_reason)
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime,
            regime_label=regime.rules.label, regime_rules=regime.rules,
            quality_filter=quality,
            blocked_reasons=[fake_reason],
            reasoning=conf_result.reasoning + [fake_reason],
            trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
            divergence=divergence,
            dxy_signal=dxy_signal,
        )

    # Capital manager inputs
    aligned_obs = [ob for ob in smc.order_blocks
                   if ob.type == ("BULLISH" if candidate == "BUY" else "BEARISH")]
    latest_ob = aligned_obs[-1] if aligned_obs else None

    entry = (float(entry_price_override)
             if entry_price_override is not None else last_candle.close)
    micro_high, micro_low = _recent_micro_levels(candles)

    # H-1 FIX: use most-recent directionally-valid BOS price as the SL anchor,
    # not the global max/min across all time.
    # BUY SL anchor: most recent SELL-BOS price below entry (= broken swing low)
    # SELL SL anchor: most recent BUY-BOS price above entry (= broken swing high)
    sell_bos_below = [b.price for b in smc.bos_signals if b.type == "SELL" and b.price < entry]
    buy_bos_above  = [b.price for b in smc.bos_signals if b.type == "BUY"  and b.price > entry]

    # H-2 FIX: populate support/resistance from SMC equal levels (previously always None).
    # Equal lows = institutional demand / support; equal highs = supply / resistance.
    eq_support    = (smc.equal_lows[-1].price
                     if smc.equal_lows  and smc.equal_lows[-1].price  < entry else None)
    eq_resistance = (smc.equal_highs[-1].price
                     if smc.equal_highs and smc.equal_highs[-1].price > entry else None)

    cap_input = CapitalInput(
        direction=candidate,
        entry_price=entry,
        atr=regime.atr,
        account_balance=account_balance,
        risk_percent=risk_percent,
        order_block_top=latest_ob.high if latest_ob else None,
        order_block_bottom=latest_ob.low if latest_ob else None,
        swing_high=buy_bos_above[-1]  if buy_bos_above  else None,
        swing_low=sell_bos_below[-1]  if sell_bos_below else None,
        support_level=eq_support,
        resistance_level=eq_resistance,
        atr_mean=regime.atr_mean,
        spread=spread,
        micro_swing_high=micro_high,
        micro_swing_low=micro_low,
        symbol=symbol,
    )
    trade_params = calc_trade_parameters(cap_input)

    # Non-negotiable operator rule: never send a trade below 1:2 R:R, even if
    # a stale Render env or a future structural-target change allows a lower
    # ratio inside the capital manager.
    if trade_params.risk_reward_ratio < REQUIRED_ENTRY_RR:
        rr_reason = (
            f"R:R {trade_params.risk_reward_ratio:.2f} < required "
            f"{REQUIRED_ENTRY_RR:.2f} — entry blocked"
        )
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade="REJECTED", regime=regime.regime,
            regime_label=regime.rules.label, regime_rules=regime.rules,
            quality_filter=quality, blocked_reasons=[rr_reason],
            reasoning=conf_result.reasoning + [rr_reason],
            trade_params=None, smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef, divergence=divergence, dxy_signal=dxy_signal,
        )

    # Marginal confidence check
    min_conf = regime.rules.min_confidence
    if conf_result.confidence < min_conf:
        if trade_params.risk_reward_ratio < CONF_MARGINAL_RR:
            return DecisionResult(
                allowed=False, direction=candidate,  # type: ignore
                confidence=conf_result.confidence, components=conf_result.components,
                grade="MARGINAL", regime=regime.regime, regime_label=regime.rules.label,
                regime_rules=regime.rules, quality_filter=quality,
                blocked_reasons=[
                    f"Marginal conf {conf_result.confidence:.1f}% requires R:R ≥ {CONF_MARGINAL_RR} "
                    f"(got {trade_params.risk_reward_ratio:.2f})"
                ],
                reasoning=conf_result.reasoning, trade_params=None,
                smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
                entry_filter=ef,
            )

    # R:R gate
    if trade_params.risk_reward_ratio < regime.rules.min_rr:
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=regime.rules, quality_filter=quality,
            blocked_reasons=[
                f"R:R {trade_params.risk_reward_ratio:.2f} < {regime.rules.min_rr} "
                f"minimum for {regime.rules.label}"
            ],
            reasoning=conf_result.reasoning, trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
        )

    # ── TRADE ALLOWED ─────────────────────────────────────────────────────────
    return DecisionResult(
        allowed=True, direction=candidate,  # type: ignore
        confidence=conf_result.confidence, components=conf_result.components,
        grade=conf_result.grade, regime=regime.regime, regime_label=regime.rules.label,
        regime_rules=regime.rules, quality_filter=quality,
        blocked_reasons=[], reasoning=conf_result.reasoning,
        trade_params=trade_params,
        smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
        entry_filter=ef,
        divergence=divergence,
        dxy_signal=dxy_signal,
    )


def describe_strategy(decision: "DecisionResult") -> dict:
    """Build a human-readable summary of *why* this trade was taken.

    Purely derived from data the decision engine already computed — it adds
    no new signal logic and cannot change whether a trade is taken. Intended
    to travel alongside a just-opened trade (e.g. published to Redis by the
    live loop) so the Telegram panel can explain the trade in its
    "TRADE OPENED" notification instead of showing only price/volume/SL/TP.
    """
    ef = decision.entry_filter
    _ENGINE_NAMES = {
        "smc":          "Smart Money Concepts (structure)",
        "trend":        "Trend (EMA alignment)",
        "price_action": "Price Action",
        "wyckoff":      "Wyckoff",
    }
    if ef is not None:
        confirmations = [
            label for key, label in _ENGINE_NAMES.items() if getattr(ef, key)
        ]
        confirmation_count = ef.confirmation_count
    else:
        confirmations = []
        confirmation_count = 0

    return {
        "direction":           decision.direction,
        "grade":               decision.grade,
        "confidence":          round(decision.confidence, 1),
        "regime":              decision.regime,
        "regime_label":        decision.regime_label,
        "confirmations":       confirmations,
        "confirmation_count":  confirmation_count,
        "confirmation_total":  4,
        # Top signal-level reasons behind the confidence score (e.g. "BOS
        # confirmed", "Strong EMA alignment (50/100/200)", "Spring confirmed").
        "signals":             list(decision.reasoning[:6]),
    }
