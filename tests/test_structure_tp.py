from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters


def test_buy_tp_sits_before_nearest_resistance():
    params = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=2.0,
            account_balance=10_000.0,
            resistance_levels=(112.0, 108.0),
        )
    )

    assert params.take_profit == 107.7
    assert params.take_profit < 108.0
    assert params.risk_reward_ratio < 2.0


def test_sell_tp_sits_before_nearest_support():
    params = calc_trade_parameters(
        CapitalInput(
            direction="SELL",
            entry_price=100.0,
            atr=2.0,
            account_balance=10_000.0,
            support_levels=(88.0, 93.0),
        )
    )

    assert params.take_profit == 93.3
    assert params.take_profit > 93.0


def test_tp_falls_back_to_two_r_without_structure():
    params = calc_trade_parameters(
        CapitalInput(
            direction="BUY",
            entry_price=100.0,
            atr=2.0,
            account_balance=10_000.0,
        )
    )

    assert params.take_profit == 106.0
    assert params.risk_reward_ratio == 2.0