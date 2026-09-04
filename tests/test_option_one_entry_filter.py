"""Entry-gate regression tests.

SMC is advisory rather than a mandatory gate. Trend alignment remains a hard
safety rule, while the legacy strict option requires Price Action + Wyckoff.
"""

from live_trading.signals.entry_filter import apply_entry_filter


def test_option_one_blocks_when_trend_is_neutral():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="NEUTRAL",
        pa_signal="BUY",
        wyckoff_signal="BUY",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert result.confirmation_count == 3


def test_option_one_allows_when_all_engines_and_trend_align():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BULLISH",
        pa_signal="BUY",
        wyckoff_signal="BUY",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.confirmation_count == 4


def test_legacy_strict_option_allows_without_smc_when_other_confirmations_align():
    result = apply_entry_filter(
        smc_signal="NEUTRAL",
        ema_trend="BULLISH",
        pa_signal="BUY",
        wyckoff_signal="BUY",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
        candidate_direction="BUY",
    )

    assert result.allowed is True
    assert result.direction == "BUY"
    assert result.smc is False


def test_option_one_blocks_when_trend_is_opposite():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BEARISH",
        pa_signal="BUY",
        wyckoff_signal="BUY",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"


def test_option_one_blocks_when_wyckoff_disagrees():
    result = apply_entry_filter(
        smc_signal="BUY",
        ema_trend="BULLISH",
        pa_signal="BUY",
        wyckoff_signal="SELL",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"


def test_option_one_blocks_when_price_action_is_missing():
    result = apply_entry_filter(
        smc_signal="SELL",
        ema_trend="BEARISH",
        pa_signal="NEUTRAL",
        wyckoff_signal="SELL",
        min_confirmations=2,
        require_smc_price_action_wyckoff=True,
    )

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
