"""Regression tests for the global counter-trend entry barrier."""

from live_trading.trading.entry_guards import validate_directional_alignment


def test_smc_bearish_structure_blocks_buy():
    allowed, reason = validate_directional_alignment(
        "BUY",
        local_trend="BULLISH",
        smc_trend="BEARISH",
        smc_signal="NEUTRAL",
    )
    assert not allowed
    assert "SMC structure BEARISH" in reason


def test_smc_bearish_composite_blocks_buy():
    allowed, reason = validate_directional_alignment(
        "BUY",
        local_trend="BULLISH",
        smc_trend="NEUTRAL",
        smc_signal="SELL",
    )
    assert not allowed
    assert "SMC composite SELL" in reason


def test_smc_bullish_context_blocks_sell():
    allowed, reason = validate_directional_alignment(
        "SELL",
        local_trend="BEARISH",
        smc_trend="BULLISH",
        smc_signal="BUY",
    )
    assert not allowed
    assert "SMC structure BULLISH" in reason


def test_neutral_smc_does_not_create_a_false_counter_trend_block():
    assert validate_directional_alignment(
        "BUY",
        local_trend="BULLISH",
        smc_trend="NEUTRAL",
        smc_signal="NEUTRAL",
    ) == (True, "")


def test_higher_timeframe_bias_is_a_final_veto():
    allowed, reason = validate_directional_alignment(
        "SELL",
        local_trend="BEARISH",
        smc_trend="NEUTRAL",
        smc_signal="NEUTRAL",
        htf_direction="BUY",
    )
    assert not allowed
    assert "higher-timeframe bias BUY" in reason