"""Regression tests for broker-local PriceHistoryV2 timestamps."""

from datetime import datetime, timezone

from live_trading.mt5.connector import _normalise_mt5_candles


def _bar(timestamp: str) -> dict:
    return {
        "time": timestamp,
        "openPrice": 1.0,
        "highPrice": 2.0,
        "lowPrice": 0.5,
        "closePrice": 1.5,
        "tickVolume": 10,
    }


def test_auto_detection_normalises_four_hour_broker_clock():
    now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    candles = _normalise_mt5_candles(
        [_bar("2026-08-17T03:55:00"), _bar("2026-08-17T04:00:00")],
        now=now,
    )

    assert [candle.time for candle in candles] == [
        "2026-08-16T23:55:00Z",
        "2026-08-17T00:00:00Z",
    ]


def test_explicit_utc_timestamp_is_not_shifted():
    now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    candles = _normalise_mt5_candles(
        [_bar("2026-08-17T00:00:00Z")],
        now=now,
    )

    assert candles[0].time == "2026-08-17T00:00:00Z"


def test_numeric_epoch_is_normalised_to_utc():
    now = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
    candles = _normalise_mt5_candles(
        [{**_bar(""), "time": 1786924800}],
        now=now,
    )

    assert candles[0].time == "2026-08-17T00:00:00Z"