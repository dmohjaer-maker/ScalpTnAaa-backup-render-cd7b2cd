"""
Decision Engine — Central orchestrator of all 7 signal engines.
Ported from decisionEngine.ts
"""
from dataclasses import dataclass, field
from typing import List, Literal, Optional
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import SmcResult, analyze_smc_structure
from live_trading.signals.wyckoff_engine import WyckoffResult, analyze_wyckoff
from live_trading.signals.price_action_engine import PriceActionResult, analyze_price_action
from live_trading.signals.trend_engine import TrendResult, analyze_trend
from live_trading.signals.market_regime import RegimeResult, RegimeEntryRules, detect_market_regime
from live_trading.signals.confidence_engine import ConfidenceResult, ConfidenceComponents, calc_confidence
from live_trading.signals.quality_filter import QualityFilterResult, apply_quality_filter, get_session_quality
from live_trading.signals.entry_filter import apply_entry_filter, EntryFilterResult
from live_trading.signals.divergence_engine import analyze_divergence, DivergenceResult
from live_trading.risk.capital_manager import CapitalInput, CapitalOutput, calc_trade_parameters
from live_trading.config import CONF_HARD_MIN

# Marginal confidence R:R floor: trades with confidence between CONF_HARD_MIN
# and the regime minimum must still achieve this R:R to be allowed.
# 1.3 = profitable in expectancy even at 45% win rate (1.3 × 0.45 > 0.55).
CONF_MARGINAL_RR = 1.3


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
    if smc.choch_signals:
        return smc.choch_signals[-1].type
    if smc.bos_signals:
        return smc.bos_signals[-1].type
    if smc.trend == "BULLISH": return "BUY"
    if smc.trend == "BEARISH": return "SELL"
    return "NEUTRAL"


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
    min_confirmations: int   = 1,
    use_atr_high_vol:  bool  = False,
    dxy_signal:        str   = "NEUTRAL",
) -> DecisionResult:

    smc     = analyze_smc_structure(candles)
    wyckoff = analyze_wyckoff(candles)
    pa      = analyze_price_action(candles)
    trend   = analyze_trend(candles)

    candidate = _candidate_direction(smc)
    if candidate == "NEUTRAL":
        return _make_neutral(smc, wyckoff, pa, trend, ["No SMC signal"])

    # Soft EMA gate — counter-trend trades are allowed but need 3 confirmations
    trend_dir = ("BUY" if trend.trend == "BULLISH" else
                 "SELL" if trend.trend == "BEARISH" else "NEUTRAL")
    _counter_trend = (candidate == "BUY" and trend_dir == "SELL") or \
                     (candidate == "SELL" and trend_dir == "BUY")

    # Detect regime early — needed to set the adaptive confirmation threshold.
    # RANGE / ACCUMULATION / DISTRIBUTION / HIGH_VOLATILITY markets suppress
    # PA and Wyckoff signals by design, so we lower the bar to 2 in those
    # regimes. Trending regimes keep the stricter operator-configured value.
    regime = detect_market_regime(candles, trend, wyckoff, use_atr_high_vol)

    _RANGE_REGIMES = {"RANGE", "ACCUMULATION", "DISTRIBUTION", "HIGH_VOLATILITY"}
    if _counter_trend:
        # Counter-trend: one extra confirmation required — EMA opposes direction.
        effective_min_confirmations = min(min_confirmations + 1, 4)
    elif regime.regime in _RANGE_REGIMES:
        # Range/volatile regimes: require one extra confirmation over the base
        # minimum.  Structural signals alone (e.g. SMC + Wyckoff without EMA
        # trend or PA) are insufficient in choppy/ranging markets — at least
        # one momentum engine must also agree to avoid repeated SL hits.
        effective_min_confirmations = min(min_confirmations + 1, 4)
    else:
        effective_min_confirmations = min_confirmations

    # Entry filter — minimum N-of-4 vote gate (SMC always required)
    ef = apply_entry_filter(
        smc_signal      = candidate,
        ema_trend       = trend.trend,
        pa_signal       = pa.pa_signal,
        wyckoff_signal  = wyckoff.wyckoff_signal,
        min_confirmations = effective_min_confirmations,
    )
    if not ef.allowed:
        votes = (f"SMC={'✓' if ef.smc else '✗'}  "
                 f"Trend={'✓' if ef.trend else '✗'}  "
                 f"PA={'✓' if ef.price_action else '✗'}  "
                 f"Wyckoff={'✓' if ef.wyckoff else '✗'}")
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
        )
        return n

    last_bos = smc.bos_signals[-1].bar_index if smc.bos_signals else None
    quality  = apply_quality_filter(candles, candidate, conf_result.confidence,
                                    last_bos, regime.adx, regime.atr_ratio)
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

    # Capital manager inputs
    aligned_obs = [ob for ob in smc.order_blocks
                   if ob.type == ("BULLISH" if candidate == "BUY" else "BEARISH")]
    latest_ob = aligned_obs[-1] if aligned_obs else None

    entry = last_candle.close

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
    )
    trade_params = calc_trade_parameters(cap_input)

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
