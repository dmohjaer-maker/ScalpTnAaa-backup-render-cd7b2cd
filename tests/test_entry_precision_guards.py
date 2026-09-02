"""Regression tests for precise entry timing and HTF alignment."""

from live_trading.signals.decision_engine import _has_fresh_entry_trigger
from live_trading.signals.mtf_filter import MtfBias, mtf_allows_trade
from live_trading.signals.smc_engine import SmcBos, SmcResult


def _smc(bos=None, sweeps=None):
    return SmcResult(
        timeframe="M5", timestamp="2026-08-11T12:00:00+00:00",
        current_price=100.0, trend="NEUTRAL",
        bos_signals=[] if bos is None else bos, choch_signals=[],
        order_blocks=[], fair_value_gaps=[],
        liquidity_sweeps=[] if sweeps is None else sweeps,
        equal_highs=[], equal_lows=[], mitigation_blocks=[],
        smc_signal="BUY", smc_score=1.0,
    )


def _htf(direction="BUY", strength="MODERATE"):
    return MtfBias(
        direction=direction, trend="BULLISH" if direction == "BUY" else "BEARISH",
        smc_signal=direction, regime="TREND", strength=strength,
    )


def test_stale_structure_is_not_an_entry_trigger():
    stale_bos = SmcBos("BUY", 100.0, 5, "2026-08-11T12:05:00+00:00")
    assert _has_fresh_entry_trigger(_smc([stale_bos]), "NEUTRAL", "BUY", 20, 2) is False


def test_recent_structure_is_an_entry_trigger():
    recent_bos = SmcBos("BUY", 100.0, 19, "2026-08-11T13:35:00+00:00")
    assert _has_fresh_entry_trigger(_smc([recent_bos]), "NEUTRAL", "BUY", 20, 2) is True


def test_moderate_opposing_htf_bias_blocks_counter_trend_trade():
    allowed, reason = mtf_allows_trade(_htf("BUY", "MODERATE"), "SELL")
    assert allowed is False
    assert "HTF bias is BUY" in reason


def test_aligned_htf_bias_allows_trade():
    assert mtf_allows_trade(_htf("SELL", "WEAK"), "SELL") == (True, "")
