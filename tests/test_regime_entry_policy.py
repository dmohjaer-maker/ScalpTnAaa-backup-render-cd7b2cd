"""Tests for the adaptive regime-aware entry gate."""

from live_trading.signals.decision_engine import evaluate_regime_entry_policy


def test_trend_allows_two_confirmations_when_confidence_is_clear():
    reasons = evaluate_regime_entry_policy(
        regime="STRONG_TREND_BULL",
        regime_min_confidence=40,
        confidence=52,
        confirmation_count=2,
        has_price_action=False,
        candidate="BUY",
        trend_direction="BUY",
    )

    assert reasons == []


def test_trend_requires_price_action_only_in_borderline_confidence_band():
    reasons = evaluate_regime_entry_policy(
        regime="STRONG_TREND_BULL",
        regime_min_confidence=40,
        confidence=43,
        confirmation_count=2,
        has_price_action=False,
        candidate="BUY",
        trend_direction="BUY",
    )

    assert any("Borderline trend confidence" in reason for reason in reasons)


def test_trend_allows_borderline_confidence_with_price_action():
    reasons = evaluate_regime_entry_policy(
        regime="STRONG_TREND_BULL",
        regime_min_confidence=40,
        confidence=43,
        confirmation_count=2,
        has_price_action=True,
        candidate="BUY",
        trend_direction="BUY",
    )

    assert reasons == []


def test_counter_trend_is_always_rejected():
    reasons = evaluate_regime_entry_policy(
        regime="STRONG_TREND_BULL",
        regime_min_confidence=40,
        confidence=85,
        confirmation_count=4,
        has_price_action=True,
        candidate="BUY",
        trend_direction="SELL",
    )

    assert any("Counter-trend entry blocked" in reason for reason in reasons)


def test_aggressive_range_allows_two_confirmations_without_price_action():
    reasons = evaluate_regime_entry_policy(
        regime="RANGE",
        regime_min_confidence=40,
        confidence=52,
        confirmation_count=2,
        has_price_action=False,
        candidate="BUY",
        trend_direction="NEUTRAL",
    )

    assert reasons == []


def test_range_can_still_require_price_action_when_configured():
    reasons = evaluate_regime_entry_policy(
        regime="RANGE",
        regime_min_confidence=40,
        confidence=52,
        confirmation_count=2,
        has_price_action=False,
        candidate="BUY",
        trend_direction="NEUTRAL",
        require_price_action=True,
    )

    assert any("requires Price Action" in reason for reason in reasons)


def test_high_volatility_accepts_only_three_confirmations_with_price_action():
    reasons = evaluate_regime_entry_policy(
        regime="HIGH_VOLATILITY",
        regime_min_confidence=65,
        confidence=66,
        confirmation_count=3,
        has_price_action=True,
        candidate="SELL",
        trend_direction="NEUTRAL",
    )

    assert reasons == []


def test_range_blocks_marginal_confidence_even_with_three_confirmations():
    reasons = evaluate_regime_entry_policy(
        regime="RANGE",
        regime_min_confidence=60,
        confidence=55,
        confirmation_count=3,
        has_price_action=True,
        candidate="BUY",
        trend_direction="NEUTRAL",
    )

    assert any("requires confidence" in reason for reason in reasons)