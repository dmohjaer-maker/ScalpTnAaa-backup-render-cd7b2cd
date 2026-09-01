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

    # The old 1.15 ATR floor would have produced about 1.86 USD here, which
    # matches the live screenshot and is too close for ordinary XAUUSD noise.
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

    # Structure-derived distance is resistance-to-entry plus the new 0.50 ATR
    # buffer, but still obeys the volatility envelope.
    assert result.stop_loss == 4332.81
    assert result.sl_distance_usd == 4.20