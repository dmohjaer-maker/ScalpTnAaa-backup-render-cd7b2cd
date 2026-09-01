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
    assert at_high == after_retrace
    assert should_apply("BUY", at_high, after_retrace, config.min_step_price) is False


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


def test_should_apply_only_moves_in_trade_direction():
    assert should_apply("BUY", 105, 105.05, 0.05)
    assert not should_apply("BUY", 105, 104.99, 0.05)
    assert should_apply("SELL", 95, 94.95, 0.05)
    assert not should_apply("SELL", 95, 95.01, 0.05)