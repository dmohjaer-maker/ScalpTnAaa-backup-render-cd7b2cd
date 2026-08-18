"""
Decision Engine — Central orchestrator of all 7 signal engines.
Ported from decisionEngine.ts
"""
from dataclasses import dataclass, field, replace
from typing import List, Literal, Optional
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import (
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
from live_trading.risk.capital_manager import CapitalInput, CapitalOutput, calc_trade_parameters
from live_trading.config import (
    CONF_HARD_MIN,
    RANGE_MIN_CONFIRMATIONS,
    RANGE_REQUIRE_PRICE_ACTION,
    REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
    ENTRY_OBSTACLE_CLEARANCE_ATR,
    ENTRY_RECENT_OBSTACLE_LOOKBACK,
    ENTRY_RECENT_OBSTACLE_TOLERANCE_ATR,
    ENTRY_RECENT_OBSTACLE_MIN_TOUCHES,
)

# Marginal confidence R:R floor for the few non-trend, non-strict regimes where
# the legacy marginal path remains valid. Trend, RANGE, and HIGH_VOLATILITY
# entries must reach their real regime threshold and never use this path.
CONF_MARGINAL_RR = 1.3
CONF_BORDERLINE_MARGIN = 5.0

# Trend regimes preserve the balanced two-confirmation policy. High volatility
# remains strict; the selected aggressive M1 profile allows the configured
# minimum in RANGE while retaining every downstream quality/risk gate.
TREND_REGIMES = frozenset({
    "STRONG_TREND_BULL",
    "STRONG_TREND_BEAR",
    "WEAK_TREND_BULL",
    "WEAK_TREND_BEAR",
    "PULLBACK_BULL",
    "PULLBACK_BEAR",
})
RANGE_REGIMES = frozenset({"RANGE"})
HIGH_VOLATILITY_REGIMES = frozenset({"HIGH_VOLATILITY"})
STRICT_ENTRY_REGIMES = HIGH_VOLATILITY_REGIMES


def evaluate_regime_entry_policy(
    regime: str,
    regime_min_confidence: float,
    confidence: float,
    confirmation_count: int,
    has_price_action: bool,
    candidate: str,
    trend_direction: str,
    configured_min_confirmations: int = 2,
    range_min_confirmations: int = RANGE_MIN_CONFIRMATIONS,
    require_price_action: bool = False,
) -> List[str]:
    """Return policy violations before quality/R:R checks are evaluated.

    This is intentionally pure so the adaptive gate can be tested without
    market-data fixtures. SMC remains the candidate direction; the other
    engines contribute confirmations through ``confirmation_count``.
    """
    reasons: List[str] = []
    counter_trend = (
        (candidate == "BUY" and trend_direction == "SELL")
        or (candidate == "SELL" and trend_direction == "BUY")
    )
    if counter_trend:
        reasons.append(
            f"Counter-trend entry blocked: EMA trend is {trend_direction} "
            f"against SMC direction {candidate}"
        )

    if regime in RANGE_REGIMES:
        required = max(configured_min_confirmations, range_min_confirmations)
        if confirmation_count < required:
            reasons.append(
                f"{regime} requires at least {required} confirmations "
                f"(got {confirmation_count})"
            )
        if require_price_action and not has_price_action:
            reasons.append(f"{regime} requires Price Action confirmation")
        if confidence < regime_min_confidence:
            reasons.append(
                f"{regime} requires confidence ≥ {regime_min_confidence:.1f}% "
                f"(got {confidence:.1f}%)"
            )
        return reasons

    if regime in HIGH_VOLATILITY_REGIMES:
        required = max(configured_min_confirmations, 3)
        if confirmation_count < required:
            reasons.append(
                f"{regime} requires at least {required} confirmations "
                f"(got {confirmation_count})"
            )
        if not has_price_action:
            reasons.append(f"{regime} requires Price Action confirmation")
        if confidence < regime_min_confidence:
            reasons.append(
                f"{regime} requires confidence ≥ {regime_min_confidence:.1f}% "
                f"(got {confidence:.1f}%)"
            )
        return reasons

    if regime in TREND_REGIMES:
        if confidence < regime_min_confidence:
            reasons.append(
                f"{regime} requires confidence ≥ {regime_min_confidence:.1f}% "
                f"(got {confidence:.1f}%)"
            )
        elif confidence < regime_min_confidence + CONF_BORDERLINE_MARGIN and not has_price_action:
            reasons.append(
                f"Borderline trend confidence {confidence:.1f}% requires "
                f"Price Action confirmation (< "
                f"{regime_min_confidence + CONF_BORDERLINE_MARGIN:.1f}%)"
            )

    return reasons


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


def _candidate_direction(smc: SmcResult) -> str:
    # Use the newest event across both lists. Prioritising the last CHoCH
    # unconditionally can resurrect an old reversal against a newer BOS.
    latest_structure = get_latest_structure_event(smc)
    if latest_structure is not None:
        return latest_structure.type
    if smc.trend == "BULLISH": return "BUY"
    if smc.trend == "BEARISH": return "SELL"
    return "NEUTRAL"


def _entry_obstacle_block_reason(
    candles: List[OHLCV],
    smc: SmcResult,
    direction: str,
    entry: float,
    atr: float,
) -> Optional[str]:
    """Reject entries with too little room before the next structure level.

    Equal levels and BOS/CHoCH prices are useful but can be absent around a
    fresh intraday floor. Confirmed five-bar swing pivots and nearby aligned
    order blocks provide a conservative fallback for the exact failure mode
    where a SELL is opened directly above support. A repeated recent floor or
    ceiling is also treated as a provisional obstacle: it does not need to be
    a fully confirmed pivot, but it must have multiple touches inside a tight
    ATR-scaled zone so a single trend-extending wick cannot block an entry.
    """
    if atr <= 0 or ENTRY_OBSTACLE_CLEARANCE_ATR <= 0:
        return None

    lookback = 5
    recent_start = max(lookback, len(candles) - 120)
    supports: List[float] = []
    resistances: List[float] = []

    for level in smc.equal_lows:
        if any(index >= recent_start for index in level.bar_indices):
            supports.append(level.price)
    for level in smc.equal_highs:
        if any(index >= recent_start for index in level.bar_indices):
            resistances.append(level.price)

    for event in [*smc.bos_signals, *smc.choch_signals]:
        if event.bar_index >= recent_start:
            (supports if event.price < entry else resistances).append(event.price)

    for ob in smc.order_blocks:
        if ob.bar_index < recent_start:
            continue
        if ob.type == "BULLISH":
            supports.extend((ob.low, ob.high))
        else:
            resistances.extend((ob.low, ob.high))

    # Confirmed pivots require candles on both sides, so the newest unfinished
    # swing cannot be mistaken for a support/resistance level.
    for index in range(recent_start, len(candles) - lookback):
        candle = candles[index]
        if all(
            candles[index - offset].low > candle.low
            and candles[index + offset].low > candle.low
            for offset in range(1, lookback + 1)
        ):
            supports.append(candle.low)
        if all(
            candles[index - offset].high < candle.high
            and candles[index + offset].high < candle.high
            for offset in range(1, lookback + 1)
        ):
            resistances.append(candle.high)

    # Detect a still-forming support/resistance zone near the current price.
    # The normal pivot scan intentionally excludes the newest five candles;
    # that is correct for confirmed structure but leaves a blind spot when
    # price prints two or more defended lows/highs right before a signal.
    # Repeated touches are required to avoid mistaking a single lower-low or
    # higher-high for a meaningful obstacle.
    recent = candles[-ENTRY_RECENT_OBSTACLE_LOOKBACK:]
    if len(recent) >= ENTRY_RECENT_OBSTACLE_MIN_TOUCHES:
        zone_tolerance = atr * ENTRY_RECENT_OBSTACLE_TOLERANCE_ATR
        recent_floor = min(candle.low for candle in recent)
        recent_floor_touches = sum(
            abs(candle.low - recent_floor) <= zone_tolerance
            for candle in recent
        )
        if recent_floor_touches >= ENTRY_RECENT_OBSTACLE_MIN_TOUCHES:
            supports.append(recent_floor)

        recent_ceiling = max(candle.high for candle in recent)
        recent_ceiling_touches = sum(
            abs(candle.high - recent_ceiling) <= zone_tolerance
            for candle in recent
        )
        if recent_ceiling_touches >= ENTRY_RECENT_OBSTACLE_MIN_TOUCHES:
            resistances.append(recent_ceiling)

    clearance = atr * ENTRY_OBSTACLE_CLEARANCE_ATR
    if direction == "SELL":
        # A close exactly on support is the most dangerous case, not a valid
        # exception. Include equality so the zero-distance obstacle blocks.
        path_levels = [level for level in supports if level <= entry]
        nearest = max(path_levels) if path_levels else None
        if nearest is not None and entry - nearest < clearance:
            return (
                f"SELL blocked: support {nearest:.2f} is only "
                f"{entry - nearest:.2f} away (< {clearance:.2f}, "
                f"{ENTRY_OBSTACLE_CLEARANCE_ATR:.2f} ATR)"
            )
    elif direction == "BUY":
        # Mirror the SELL rule: a close exactly on resistance has no room.
        path_levels = [level for level in resistances if level >= entry]
        nearest = min(path_levels) if path_levels else None
        if nearest is not None and nearest - entry < clearance:
            return (
                f"BUY blocked: resistance {nearest:.2f} is only "
                f"{nearest - entry:.2f} away (< {clearance:.2f}, "
                f"{ENTRY_OBSTACLE_CLEARANCE_ATR:.2f} ATR)"
            )
    return None


def _make_neutral(smc, wyckoff, pa, trend, blocked_reasons, reasoning=None) -> DecisionResult:
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
    )


def run_decision_engine(
    candles:           List[OHLCV],
    account_balance:   float,
    risk_percent:      float = 1.0,
    min_confirmations: int   = 2,
    use_atr_high_vol:  bool  = False,
    dxy_signal:        str   = "NEUTRAL",
    require_price_action: bool = False,
    require_smc_price_action_wyckoff: bool = REQUIRE_SMC_PRICE_ACTION_WYCKOFF,
    range_min_confidence: Optional[float] = None,
    range_min_rr: Optional[float] = None,
) -> DecisionResult:

    smc     = analyze_smc_structure(candles)
    wyckoff = analyze_wyckoff(candles)
    pa      = analyze_price_action(candles)
    trend   = analyze_trend(candles)

    candidate = _candidate_direction(smc)
    if candidate == "NEUTRAL":
        return _make_neutral(smc, wyckoff, pa, trend, ["No SMC signal"])

    # EMA direction is also used by the regime-aware policy below. A direct
    # opposite-direction setup must never reach the order path.
    trend_dir = ("BUY" if trend.trend == "BULLISH" else
                 "SELL" if trend.trend == "BEARISH" else "NEUTRAL")

    # Detect regime early — needed to set the adaptive confirmation threshold.
    # High volatility remains strict. The aggressive M1 RANGE profile uses the
    # configured minimum (normally two confirmations) and relies on the
    # downstream confidence, quality, R:R, MTF, retest, and risk gates.
    regime = detect_market_regime(candles, trend, wyckoff, use_atr_high_vol)
    effective_rules = regime.rules
    if regime.regime in RANGE_REGIMES:
        effective_rules = replace(
            regime.rules,
            min_confidence=(
                range_min_confidence
                if range_min_confidence is not None
                else regime.rules.min_confidence
            ),
            min_rr=(
                range_min_rr
                if range_min_rr is not None
                else regime.rules.min_rr
            ),
        )

    strict_regime = regime.regime in HIGH_VOLATILITY_REGIMES
    effective_min_confirmations = (
        max(min_confirmations, 3)
        if strict_regime
        else max(min_confirmations, RANGE_MIN_CONFIRMATIONS)
        if regime.regime in RANGE_REGIMES
        else min_confirmations
    )

    # Entry filter — minimum N-of-4 vote gate (SMC always required)
    ef = apply_entry_filter(
        smc_signal      = candidate,
        ema_trend       = trend.trend,
        pa_signal       = pa.pa_signal,
        wyckoff_signal  = wyckoff.wyckoff_signal,
        min_confirmations = effective_min_confirmations,
        require_price_action=(
            require_price_action
            or RANGE_REQUIRE_PRICE_ACTION
            or strict_regime
        ),
        require_smc_price_action_wyckoff = require_smc_price_action_wyckoff,
    )
    if not ef.allowed:
        votes = (f"SMC={'✓' if ef.smc else '✗'}  "
                 f"Trend={'✓' if ef.trend else '✗'}  "
                 f"PA={'✓' if ef.price_action else '✗'}  "
                 f"Wyckoff={'✓' if ef.wyckoff else '✗'}")
        if (
            require_smc_price_action_wyckoff
            and not (ef.smc and ef.price_action and ef.wyckoff)
        ):
            reason = (
                "Entry filter: Option 1 requires SMC + Price Action + Wyckoff — "
                f"{votes}  [regime={regime.regime}]"
            )
        elif require_price_action and not ef.price_action:
            reason = (f"Entry filter: Price Action confirmation required — "
                      f"{votes}  [regime={regime.regime}]")
        else:
            reason = (f"Entry filter: only {ef.confirmation_count}/{effective_min_confirmations} "
                      f"confirmations — {votes}  [regime={regime.regime}]")
        return _make_neutral(smc, wyckoff, pa, trend, [reason], [reason])

    if candidate == "BUY"  and not regime.rules.allow_long:
        return _make_neutral(smc, wyckoff, pa, trend,
                             [f'Regime "{regime.rules.label}" does not allow LONG'])
    if candidate == "SELL" and not regime.rules.allow_short:
        return _make_neutral(smc, wyckoff, pa, trend,
                             [f'Regime "{regime.rules.label}" does not allow SHORT'])

    last_candle  = candles[-1]
    session      = get_session_quality(last_candle.time)
    divergence   = analyze_divergence(candles)
    # Option 3: DXY is retained as telemetry only and cannot affect entry
    # confidence or the decision. The confidence engine explicitly ignores
    # this legacy compatibility argument.
    conf_result  = calc_confidence(
        smc, wyckoff, pa, trend, regime, session, candidate,
        divergence_signal=divergence.signal,
    )

    policy_reasons = evaluate_regime_entry_policy(
        regime=regime.regime,
        regime_min_confidence=effective_rules.min_confidence,
        confidence=conf_result.confidence,
        confirmation_count=ef.confirmation_count,
        has_price_action=ef.price_action,
        candidate=candidate,
        trend_direction=trend_dir,
        configured_min_confirmations=min_confirmations,
        range_min_confirmations=RANGE_MIN_CONFIRMATIONS,
        require_price_action=(
            require_price_action
            or RANGE_REQUIRE_PRICE_ACTION
            or strict_regime
        ),
    )
    confidence_block_reason = (
        f"Confidence {conf_result.confidence:.1f}% < {CONF_HARD_MIN}% hard minimum"
        if conf_result.confidence < CONF_HARD_MIN
        else (policy_reasons[0] if policy_reasons else None)
    )
    if confidence_block_reason:
        all_reasons = (
            [confidence_block_reason]
            if confidence_block_reason in policy_reasons
            else [confidence_block_reason, *policy_reasons]
        )
        n = DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=(
                "REJECTED"
                if conf_result.confidence < CONF_HARD_MIN or policy_reasons
                else "MARGINAL"
            ),
            regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=effective_rules,
            quality_filter=QualityFilterResult(
                False, all_reasons, session, regime.adx, False, False,
                conf_result.confidence < CONF_HARD_MIN, False, False, False),
            blocked_reasons=all_reasons,
            reasoning=conf_result.reasoning, trade_params=None,
            smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
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
            regime_rules=effective_rules, quality_filter=quality,
            blocked_reasons=quality.blocked_reasons, reasoning=conf_result.reasoning,
            trade_params=None, smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
            entry_filter=ef,
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

    entry = last_candle.close
    obstacle_reason = _entry_obstacle_block_reason(
        candles, smc, candidate, entry, regime.atr
    )
    if obstacle_reason:
        quality.allowed = False
        quality.blocked_reasons.append(obstacle_reason)
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime,
            regime_label=regime.rules.label, regime_rules=effective_rules,
            quality_filter=quality, blocked_reasons=[obstacle_reason],
            reasoning=conf_result.reasoning + [obstacle_reason],
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

    # H-1 FIX: use most-recent directionally-valid BOS price as the SL anchor,
    # not the global max/min across all time.
    # BUY SL anchor: most recent SELL-BOS price below entry (= broken swing low)
    # SELL SL anchor: most recent BUY-BOS price above entry (= broken swing high)
    sell_bos_below = [b.price for b in smc.bos_signals if b.type == "SELL" and b.price < entry]
    buy_bos_above  = [b.price for b in smc.bos_signals if b.type == "BUY"  and b.price > entry]

    # H-2 FIX: populate support/resistance from SMC equal levels (previously always None).
    # Equal lows = institutional demand / support; equal highs = supply / resistance.
    # Use the nearest confirmed equal level in the trade's path.  The newest
    # detected level is not always the most relevant obstacle.
    _support_levels = [level.price for level in smc.equal_lows if level.price < entry]
    _resistance_levels = [level.price for level in smc.equal_highs if level.price > entry]
    eq_support = max(_support_levels) if _support_levels else None
    eq_resistance = min(_resistance_levels) if _resistance_levels else None

    # BOS/CHoCH prices are also confirmed market-structure levels.  Supplying
    # all valid levels allows the TP engine to choose the nearest obstacle,
    # while the SL engine still uses the strongest nearby anchor.
    _structure_events = [*smc.bos_signals, *smc.choch_signals]
    _structure_supports = [
        event.price for event in _structure_events if event.price < entry
    ]
    _structure_resistances = [
        event.price for event in _structure_events if event.price > entry
    ]

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
        support_levels=(*_support_levels, *_structure_supports),
        resistance_levels=(*_resistance_levels, *_structure_resistances),
    )
    trade_params = calc_trade_parameters(cap_input)

    # Marginal confidence is intentionally retained only for regimes outside
    # the trend/range policy. Those regimes are not covered by the adaptive
    # strict gate above and still retain the legacy R:R safeguard.
    min_conf = effective_rules.min_confidence
    if conf_result.confidence < min_conf:
        if trade_params.risk_reward_ratio < CONF_MARGINAL_RR:
            return DecisionResult(
                allowed=False, direction=candidate,  # type: ignore
                confidence=conf_result.confidence, components=conf_result.components,
                grade="MARGINAL", regime=regime.regime, regime_label=regime.rules.label,
                regime_rules=effective_rules, quality_filter=quality,
                blocked_reasons=[
                    f"Marginal conf {conf_result.confidence:.1f}% requires R:R ≥ {CONF_MARGINAL_RR} "
                    f"(got {trade_params.risk_reward_ratio:.2f})"
                ],
                reasoning=conf_result.reasoning, trade_params=None,
                smc=smc, wyckoff=wyckoff, pa=pa, trend=trend,
                entry_filter=ef,
            )

    # R:R gate
    if trade_params.risk_reward_ratio < effective_rules.min_rr:
        return DecisionResult(
            allowed=False, direction=candidate,  # type: ignore
            confidence=conf_result.confidence, components=conf_result.components,
            grade=conf_result.grade, regime=regime.regime, regime_label=regime.rules.label,
            regime_rules=effective_rules, quality_filter=quality,
            blocked_reasons=[
                f"R:R {trade_params.risk_reward_ratio:.2f} < {effective_rules.min_rr} "
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
        regime_rules=effective_rules, quality_filter=quality,
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
