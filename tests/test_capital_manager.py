"""Regression coverage for the structure-aware initial stop envelope."""

from live_trading.risk.capital_manager import (
    CapitalInput,
    calc_trade_parameters,
    validate_trade_risk,
    validate_total_open_risk,
)


def _input(**overrides):
    values = {
        "direction": "SELL",
        "entry_price": 4328.61,
        "atr": 1.62,
        "account_balance": 1000.0,
    }
    values.update(overrides)
    return CapitalInput(**values)


def test_initial_sell_stop_clears_normal_m5_noise():
    result = calc_trade_parameters(_input())

    # The hard 1.80 ATR floor keeps ordinary XAUUSD M5 noise from taking out
    # a newly opened position.
    assert result.sl_distance_usd >= 1.62 * 1.80
    assert result.stop_loss > result.entry_price


def test_wider_stop_reduces_lot_size_to_keep_risk_constant():
    result = calc_trade_parameters(_input())

    # With a 1.15 ATR stop the same account would size at roughly 0.0537 lots;
    # the wider stop must reduce that size instead of increasing account risk.
    old_floor_distance = 1.62 * 1.15
    old_lot = 1000.0 / (old_floor_distance * 100)
    assert result.lot_size < old_lot
    assert result.risk_amount == round(
        result.lot_size * result.sl_distance_usd * 100, 2
    )


def test_structure_buffer_is_outside_selected_sell_resistance():
    result = calc_trade_parameters(
        _input(resistance_level=4332.00),
    )

    # A valid resistance anchor remains outside the structure, but the
    # minimum envelope still applies if the structure is too close.
    assert result.stop_loss > 4332.00
    assert result.sl_distance_usd >= 1.62 * 1.80


def test_nearby_structure_cannot_collapse_stop():
    result = calc_trade_parameters(
        _input(
            entry_price=4402.79,
            atr=0.40,
            micro_swing_high=4402.95,
            spread=0.08,
        ),
    )

    assert result.stop_loss > result.entry_price
    assert result.sl_distance_usd >= round(0.40 * 1.80, 2)


def test_buy_stop_has_the_same_floor():
    result = calc_trade_parameters(
        _input(
            direction="BUY",
            entry_price=4402.31,
            atr=1.10,
            micro_swing_low=4402.10,
        ),
    )

    assert result.stop_loss < result.entry_price
    assert result.sl_distance_usd >= round(1.10 * 1.80, 2)


def test_high_volatility_expands_floor_without_changing_entry():
    result = calc_trade_parameters(
        _input(
            entry_price=4402.79,
            atr=2.40,
            atr_mean=1.20,
        ),
    )

    assert result.entry_price == 4402.79
    assert result.sl_distance_usd >= round(2.40 * 2.15, 2)


def test_every_fallback_target_is_exactly_two_r():
    result = calc_trade_parameters(_input())

    assert result.risk_reward_ratio == 2.0
    assert result.take_profit < result.entry_price


def test_minimum_lot_excess_risk_is_blocked_instead_of_forced():
    result = calc_trade_parameters(
        _input(account_balance=440.0, atr=15.8),
    )

    assert result.lot_size == 0.01
    assert result.risk_amount / 440.0 * 100.0 > 6.0
    allowed, reason = validate_trade_risk(result, 440.0, 1.0)

    assert allowed is False
    assert "entry blocked" in reason


def test_total_open_risk_includes_existing_positions():
    positions = [
        {
            "id": "one",
            "symbol": "XAUUSD",
            "volume": 0.01,
            "open_price": 4400.0,
            "sl": 4390.0,
        },
        {
            "id": "two",
            "symbol": "XAUUSD",
            "volume": 0.01,
            "open_price": 4400.0,
            "sl": 4390.0,
        },
    ]

    allowed, reason = validate_total_open_risk(
        positions, new_trade_risk=10.0, account_balance=440.0,
        max_total_risk_percent=3.0,
    )

    assert allowed is False
    assert "Aggregate stop risk" in reason


def test_unprotected_position_blocks_new_entry():
    allowed, reason = validate_total_open_risk(
        [{"id": "unprotected", "symbol": "XAUUSD", "volume": 0.01,
          "open_price": 4400.0, "sl": 0.0}],
        new_trade_risk=1.0, account_balance=440.0,
        max_total_risk_percent=3.0,
    )

    assert allowed is False
    assert "protective SL" in reason