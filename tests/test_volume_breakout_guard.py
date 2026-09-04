"""Tests for the weak-volume breakout veto."""

from live_trading.signals.decision_engine import _weak_volume_breakout_reason
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.price_action_engine import PriceActionResult
from live_trading.signals.smc_engine import SmcBos, SmcResult


def _pa(candidate: str, breakout: bool = True) -> PriceActionResult:
    return PriceActionResult(
        bullish_engulf=False,
        bearish_engulf=False,
        bullish_pin_bar=False,
        bearish_pin_bar=False,
        strong_bullish=False,
        strong_bearish=False,
        near_demand_zone=False,
        near_supply_zone=False,
        near_support=False,
        near_resistance=False,
        valid_bull_breakout=breakout and candidate == "BUY",
        valid_bear_breakout=breakout and candidate == "SELL",
        fake_bull_breakout=False,
        fake_bear_breakout=False,
        bullish_pullback=False,
        bearish_pullback=False,
        pa_signal=candidate,
        pa_score=0.8,
    )


def _smc(bos: SmcBos | None = None) -> SmcResult:
    return SmcResult(
        timeframe="M5",
        timestamp="2026-09-04T12:00:00+00:00",
        current_price=100.0,
        trend="NEUTRAL",
        bos_signals=[bos] if bos else [],
        choch_signals=[],
        order_blocks=[],
        fair_value_gaps=[],
        liquidity_sweeps=[],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal=bos.type if bos else "NEUTRAL",
        smc_score=0.8 if bos else 0.0,
    )


def _candles(count: int = 25) -> list[OHLCV]:
    return [
        OHLCV(
            time="2026-09-04T12:00:00+00:00",
            open=100.0,
            high=100.2,
            low=99.8,
            close=100.0,
            volume=100.0,
        )
        for _ in range(count)
    ]


def test_weak_volume_blocks_price_action_breakout():
    reason = _weak_volume_breakout_reason(
        _candles(), _smc(), _pa("BUY"), "BUY", is_weak_volume=True
    )
    assert reason is not None
    assert "Very weak volume" in reason


def test_weak_volume_blocks_recent_bos_breakout():
    reason = _weak_volume_breakout_reason(
        _candles(), _smc(SmcBos("SELL", 100.0, 23, "2026-09-04T12:00:00+00:00")),
        _pa("SELL", breakout=False), "SELL", is_weak_volume=True
    )
    assert reason is not None
    assert "breakout" in reason


def test_weak_volume_does_not_block_non_breakout_setup():
    reason = _weak_volume_breakout_reason(
        _candles(), _smc(), _pa("BUY", breakout=False), "BUY", is_weak_volume=True
    )
    assert reason is None


def test_normal_volume_does_not_block_breakout():
    reason = _weak_volume_breakout_reason(
        _candles(), _smc(), _pa("BUY"), "BUY", is_weak_volume=False
    )
    assert reason is None