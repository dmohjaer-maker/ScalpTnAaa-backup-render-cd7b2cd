"""Focused regression coverage for adaptive trailing-stop calculations."""

from live_trading.risk.trailing_stop import (
    TrailingConfig,
    compute_staircase_sl,
    should_apply,
)


def _config() -> TrailingConfig:
    return TrailingConfig(
        enabled=True,
        activation_r=0.75,
        step_r=0.5,
        lock_buffer_r=0.15,
        atr_gap_mult=0.8,
        min_step_price=0.05,
        min_gap_price=0.05,
    )


def test_buy_waits_until_activation():
    assert compute_staircase_sl(
        "BUY", entry=100, risk_distance=10, current_price=106, atr=2, cfg=_config()
    ) is None


def test_buy_locks_profit_from_favorable_extreme():
    candidate = compute_staircase_sl(
        "BUY",
        entry=100,
        risk_distance=10,
        current_price=110,
        atr=2,
        cfg=_config(),
        favorable_extreme=110,
    )
    # max(ATR gap 1.6, risk-relative gap 1.5) behind the high.
    assert candidate == 108.4


def test_buy_does_not_loosen_after_retracement():
    config = _config()
    at_high = compute_staircase_sl(
        "BUY", 100, 10, 110, 2, config, favorable_extreme=110
    )
    after_retrace = compute_staircase_sl(
        "BUY", 100, 10, 104, 2, config, favorable_extreme=110
    )
    assert at_high is not None
    assert after_retrace is None
    assert should_apply("BUY", at_high, after_retrace, config.min_step_price) is False


def test_buy_skips_invalid_candidate_after_deep_retracement():
    config = _config()
    candidate = compute_staircase_sl(
        "BUY",
        entry=100,
        risk_distance=10,
        current_price=104,
        atr=2,
        cfg=config,
        favorable_extreme=110,
    )
    # The high-water candidate would be above the live bid. Never send an
    # invalid broker-side stop and never loosen an already-protected trade.
    assert candidate is None


def test_sell_uses_low_water_mark_and_locks_profit():
    candidate = compute_staircase_sl(
        "SELL",
        entry=100,
        risk_distance=10,
        current_price=90,
        atr=2,
        cfg=_config(),
        favorable_extreme=90,
    )
    assert candidate == 91.6


def test_spread_is_included_in_execution_gap():
    candidate = compute_staircase_sl(
        "BUY",
        entry=100,
        risk_distance=10,
        current_price=112,
        atr=0.5,
        cfg=_config(),
        favorable_extreme=112,
        spread=1.0,
    )
    # 2.5 price units of spread protection dominates the small ATR gap.
    assert candidate == 109.5


def test_profit_lock_curve_increases_protection():
    config = _config()
    early = compute_staircase_sl(
        "BUY", 100, 10, 109, 1, config, favorable_extreme=109
    )
    later = compute_staircase_sl(
        "BUY", 100, 10, 125, 1, config, favorable_extreme=125
    )
    assert early is not None and later is not None
    assert early > 100
    assert later > early


def test_should_apply_only_moves_in_trade_direction():
    assert should_apply("BUY", 105, 105.05, 0.05)
    assert not should_apply("BUY", 105, 104.99, 0.05)
    assert should_apply("SELL", 95, 94.95, 0.05)
    assert not should_apply("SELL", 95, 95.01, 0.05)