"""Tests for closed-candle liquidity-sweep confirmation."""

from live_trading.signals.decision_engine import _liquidity_sweep_reason
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import SmcLiquiditySweep, SmcResult


def _candle(close: float, open_: float | None = None) -> OHLCV:
    open_price = close if open_ is None else open_
    return OHLCV(
        time="2026-09-04T12:00:00+00:00",
        open=open_price,
        high=max(open_price, close) + 0.20,
        low=min(open_price, close) - 0.20,
        close=close,
        volume=100.0,
    )


def _smc(sweep: SmcLiquiditySweep) -> SmcResult:
    return SmcResult(
        timeframe="M5",
        timestamp="2026-09-04T12:00:00+00:00",
        current_price=sweep.swept_level,
        trend="NEUTRAL",
        bos_signals=[],
        choch_signals=[],
        order_blocks=[],
        fair_value_gaps=[],
        liquidity_sweeps=[sweep],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal="BUY" if sweep.type == "BULLISH" else "SELL",
        smc_score=0.8,
    )


def test_sweep_waits_for_one_closed_confirmation_candle():
    sweep = SmcLiquiditySweep("BULLISH", 100.0, 99.5, 2, "2026-09-04T12:00:00+00:00")
    reason = _liquidity_sweep_reason(
        [_candle(99.4), _candle(99.6), _candle(100.3, 99.7)],
        _smc(sweep),
        "BUY",
    )
    assert reason is not None
    assert "pending confirmation" in reason


def test_sweep_blocks_without_directional_follow_through():
    sweep = SmcLiquiditySweep("BULLISH", 100.0, 99.5, 1, "2026-09-04T12:00:00+00:00")
    reason = _liquidity_sweep_reason(
        [_candle(99.4), _candle(100.3, 99.7), _candle(99.8, 100.2)],
        _smc(sweep),
        "BUY",
    )
    assert reason is not None
    assert "False liquidity sweep" in reason


def test_sweep_allows_directional_follow_through():
    sweep = SmcLiquiditySweep("BEARISH", 100.0, 100.5, 1, "2026-09-04T12:00:00+00:00")
    reason = _liquidity_sweep_reason(
        [_candle(100.6), _candle(99.7, 100.3), _candle(99.4, 99.8)],
        _smc(sweep),
        "SELL",
    )
    assert reason is None


def test_sweep_blocks_when_price_later_reclaims_swept_level():
    sweep = SmcLiquiditySweep("BULLISH", 100.0, 99.5, 1, "2026-09-04T12:00:00+00:00")
    reason = _liquidity_sweep_reason(
        [_candle(99.4), _candle(100.3, 99.7), _candle(100.6, 100.2),
         _candle(99.8, 100.1)],
        _smc(sweep),
        "BUY",
    )
    assert reason is not None
    assert "closed back across" in reason


def test_old_confirmed_sweep_does_not_block_newer_setup():
    sweep = SmcLiquiditySweep("BULLISH", 100.0, 99.5, 1, "2026-09-04T12:00:00+00:00")
    reason = _liquidity_sweep_reason(
        [_candle(99.4), _candle(100.3, 99.7), _candle(100.6, 100.2),
         _candle(100.8, 100.5), _candle(101.0, 100.7), _candle(101.1, 100.9)],
        _smc(sweep),
        "BUY",
    )
    assert reason is None