"""Regression tests for independent XAUUSD/EURUSD strategy calibration."""

from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters
from live_trading.signals.wyckoff_engine import (
    _get_cfg,
    calibrate_wyckoff,
    set_calibrated_config,
)
from live_trading.symbols import smc_thresholds


def test_smc_absolute_thresholds_are_not_shared():
    xau = smc_thresholds("XAUUSD")
    eur = smc_thresholds("EURUSD")
    assert xau["min_break_distance"] == 0.20
    assert eur["min_break_distance"] == 0.00040
    assert xau["price_digits"] == 2
    assert eur["price_digits"] == 5


def test_wyckoff_calibration_isolated_by_symbol():
    xau = calibrate_wyckoff([], "XAUUSD")
    eur = calibrate_wyckoff([], "EURUSD")
    assert xau.spring_margin == 0.20
    assert eur.spring_margin == 0.00040

    set_calibrated_config(xau, "XAUUSD")
    set_calibrated_config(eur, "EURUSD")
    assert _get_cfg("XAUUSD").spring_margin == 0.20
    assert _get_cfg("EURUSD").spring_margin == 0.00040


def test_eurusd_capital_output_preserves_broker_precision():
    result = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=1.10000,
            atr=0.00100,
            account_balance=1000.0,
            risk_percent=1.0,
            order_block_bottom=1.09750,
            spread=0.00010,
            symbol="EURUSD",
        )
    )
    assert result.stop_loss == 1.09750
    assert result.take_profit == 1.10500
    assert result.trailing_activation_at == 1.10250
    assert result.risk_amount == 10.0