"""Tests for closed-candle BOS follow-through confirmation."""

from live_trading.signals.decision_engine import (
    _bos_failure_allows_independent_setup,
    _bos_follow_through_reason,
)
from live_trading.signals.entry_filter import EntryFilterResult
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import SmcBos, SmcResult


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


def _smc(bos: SmcBos) -> SmcResult:
    return SmcResult(
        timeframe="M5",
        timestamp="2026-09-04T12:00:00+00:00",
        current_price=bos.price,
        trend="NEUTRAL",
        bos_signals=[bos],
        choch_signals=[],
        order_blocks=[],
        fair_value_gaps=[],
        liquidity_sweeps=[],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal=bos.type,
        smc_score=0.8,
    )


def _filter(*, trend=False, price_action=False) -> EntryFilterResult:
    return EntryFilterResult(
        allowed=True,
        direction="SELL",
        confirmation_count=int(trend) + int(price_action),
        smc=False,
        trend=trend,
        price_action=price_action,
        wyckoff=False,
    )


def test_bos_waits_for_one_closed_confirmation_candle():
    bos = SmcBos("BUY", 100.0, 2, "2026-09-04T12:00:00+00:00")
    reason = _bos_follow_through_reason(
        [_candle(99.4), _candle(99.6), _candle(100.3, 99.7)],
        _smc(bos),
        "BUY",
    )
    assert reason is not None
    assert "pending confirmation" in reason


def test_bos_blocks_when_confirmation_loses_broken_level():
    bos = SmcBos("BUY", 100.0, 1, "2026-09-04T12:00:00+00:00")
    reason = _bos_follow_through_reason(
        [_candle(99.4), _candle(100.3, 99.7), _candle(99.8, 100.2)],
        _smc(bos),
        "BUY",
    )
    assert reason is not None
    assert "False BOS" in reason


def test_bos_allows_directional_follow_through():
    bos = SmcBos("SELL", 100.0, 1, "2026-09-04T12:00:00+00:00")
    reason = _bos_follow_through_reason(
        [_candle(100.6), _candle(99.7, 100.3), _candle(99.4, 99.8)],
        _smc(bos),
        "SELL",
    )
    assert reason is None


def test_bos_blocks_when_price_later_reclaims_broken_level():
    bos = SmcBos("BUY", 100.0, 1, "2026-09-04T12:00:00+00:00")
    reason = _bos_follow_through_reason(
        [_candle(99.4), _candle(100.3, 99.7), _candle(100.6, 100.2),
         _candle(99.8, 100.1)],
        _smc(bos),
        "BUY",
    )
    assert reason is not None
    assert "closed back across" in reason


def test_old_confirmed_bos_does_not_block_newer_setup():
    bos = SmcBos("BUY", 100.0, 1, "2026-09-04T12:00:00+00:00")
    reason = _bos_follow_through_reason(
        [_candle(99.4), _candle(100.3, 99.7), _candle(100.6, 100.2),
         _candle(100.8, 100.5), _candle(101.0, 100.7), _candle(101.1, 100.9)],
        _smc(bos),
        "BUY",
    )
    assert reason is None


def test_expired_bos_can_fall_back_to_aligned_htf_trend():
    assert _bos_failure_allows_independent_setup(
        "BOS expired: SELL breakout had no accepted continuation within 3 closed bars",
        "SELL",
        _filter(trend=True),
        "SELL",
        "SELL",
        "STRONG",
    )


def test_false_bos_still_requires_independent_price_action():
    assert not _bos_failure_allows_independent_setup(
        "False BOS detected: SELL breakout closed decisively back through the level",
        "SELL",
        _filter(trend=True),
        "SELL",
        "SELL",
        "STRONG",
    )
    assert _bos_failure_allows_independent_setup(
        "False BOS detected: SELL breakout closed decisively back through the level",
        "SELL",
        _filter(trend=True, price_action=True),
        "SELL",
        "SELL",
        "STRONG",
    )