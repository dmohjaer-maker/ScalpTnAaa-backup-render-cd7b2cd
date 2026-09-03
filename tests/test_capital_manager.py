"""Regression coverage for the structure-aware initial stop envelope."""

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters


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