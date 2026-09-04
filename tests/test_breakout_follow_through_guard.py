"""Tests for breakout retest / consecutive-close confirmation."""

from live_trading.signals.decision_engine import _breakout_follow_through_reason
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.price_action_engine import PriceActionResult


def _candle(close: float, open_: float | None = None,
            high: float | None = None, low: float | None = None) -> OHLCV:
    open_price = close if open_ is None else open_
    return OHLCV(
        time="2026-09-04T12:00:00+00:00",
        open=open_price,
        high=max(open_price, close) + 0.20 if high is None else high,
        low=min(open_price, close) - 0.20 if low is None else low,
        close=close,
        volume=100.0,
    )


def _pa(candidate: str, level: float = 100.0) -> PriceActionResult:
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
        valid_bull_breakout=candidate == "BUY",
        valid_bear_breakout=candidate == "SELL",
        fake_bull_breakout=False,
        fake_bear_breakout=False,
        bullish_pullback=False,
        bearish_pullback=False,
        pa_signal=candidate,
        pa_score=0.8,
        bull_breakout_level=level if candidate == "BUY" else None,
        bear_breakout_level=level if candidate == "SELL" else None,
    )


def test_breakout_waits_for_second_close_or_retest():
    reason = _breakout_follow_through_reason(
        [_candle(99.6), _candle(100.4, 99.8)],
        _pa("BUY"),
        "BUY",
    )
    assert reason is not None
    assert "retest or two consecutive closes" in reason


def test_breakout_allows_two_consecutive_closes():
    reason = _breakout_follow_through_reason(
        [_candle(100.2, 99.8), _candle(100.5, 100.1)],
        _pa("BUY"),
        "BUY",
    )
    assert reason is None


def test_breakout_allows_successful_bullish_retest():
    reason = _breakout_follow_through_reason(
        [_candle(99.6), _candle(100.4, 99.8),
         _candle(100.3, 99.9, high=100.5, low=99.95)],
        _pa("BUY"),
        "BUY",
    )
    assert reason is None


def test_breakout_blocks_failed_retest():
    reason = _breakout_follow_through_reason(
        [_candle(99.6), _candle(100.4, 99.8),
         _candle(99.8, 100.3, high=100.5, low=99.6)],
        _pa("BUY"),
        "BUY",
    )
    assert reason is not None


def test_bearish_breakout_requires_same_confirmation():
    reason = _breakout_follow_through_reason(
        [_candle(100.4), _candle(99.6, 100.2)],
        _pa("SELL"),
        "SELL",
    )
    assert reason is not None