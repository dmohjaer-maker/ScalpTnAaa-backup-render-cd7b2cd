from types import SimpleNamespace

from live_trading.signals.decision_engine import _entry_obstacle_block_reason
from live_trading.signals.gold_engine import OHLCV


def _candles_with_confirmed_floor() -> list[OHLCV]:
    lows = [105, 104, 103, 102, 101, 100, 101, 102, 103, 104, 105, 106, 107]
    return [
        OHLCV(
            time=f"2026-08-18T00:{index:02d}:00+00:00",
            open=low + 0.4,
            high=low + 1.2,
            low=low,
            close=low + 0.6,
            volume=1000,
        )
        for index, low in enumerate(lows)
    ]


def test_sell_is_blocked_inside_support_clearance():
    smc = SimpleNamespace(
        equal_lows=[],
        equal_highs=[],
        bos_signals=[],
        choch_signals=[],
        order_blocks=[],
    )
    reason = _entry_obstacle_block_reason(
        _candles_with_confirmed_floor(), smc, "SELL", entry=100.5, atr=1.0
    )
    assert reason is not None
    assert "SELL blocked" in reason
    assert "support" in reason


def test_sell_is_allowed_with_room_below_support():
    smc = SimpleNamespace(
        equal_lows=[],
        equal_highs=[],
        bos_signals=[],
        choch_signals=[],
        order_blocks=[],
    )
    reason = _entry_obstacle_block_reason(
        _candles_with_confirmed_floor(), smc, "SELL", entry=102.0, atr=1.0
    )
    assert reason is None