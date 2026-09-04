from live_trading.risk.capital_manager import CapitalInput, calc_trade_parameters


def _input(symbol=None):
    is_eurusd = bool(symbol and symbol.upper().startswith("EURUSD"))
    values = dict(
        direction="BUY",
        entry_price=1.10000 if is_eurusd else 2500.0,
        atr=0.00100 if is_eurusd else 5.0,
        account_balance=1000.0,
        risk_percent=1.0,
        order_block_bottom=1.09750 if is_eurusd else 2488.0,
        spread=0.00010 if is_eurusd else 0.5,
    )
    if symbol is not None:
        values["symbol"] = symbol
    return CapitalInput(**values)


def test_xau_legacy_default_matches_explicit_symbol():
    legacy = calc_trade_parameters(_input())
    explicit = calc_trade_parameters(_input("XAUUSD"))
    assert vars(legacy) == vars(explicit)


def test_eurusd_uses_fx_precision_and_contract_value():
    result = calc_trade_parameters(_input("EURUSD"))

    assert result.stop_loss != result.entry_price
    assert result.sl_distance_pips >= 1.0
    # A 100,000-unit EURUSD contract keeps one-percent account risk near $10.
    assert 9.0 <= result.risk_amount <= 10.0
    assert result.lot_size < 1.0


def test_symbol_suffixes_keep_eurusd_risk_path():
    plain = calc_trade_parameters(_input("EURUSD"))
    broker_suffix = calc_trade_parameters(_input("EURUSDm"))
    assert vars(plain) == vars(broker_suffix)
