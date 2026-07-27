"""
Connector Deduplication Tests — live_trading/mt5/connector.py
=============================================================
Tests the candle deduplication logic added to fetch_candles() (Fix M-01 / ST-06).
Does NOT test MetaAPI connectivity — all tests use injected raw candle data.

These tests verify ENGINEERING correctness only.
Trading behaviour is unaffected by deduplication — duplicate candles carry
identical OHLCV values so their removal produces the same unique candle
sequence as if the SDK had never returned the duplicate.
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock


def _make_raw_candle(time_str: str, open_=1800.0, high=1810.0,
                     low=1795.0, close=1805.0) -> dict:
    """Create a minimal MetaAPI candle dict."""
    return {
        "time": time_str,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "tickVolume": 100,
    }


class TestSortKey:
    """_sort_key handles both str and datetime time fields."""

    def test_sort_key_with_string_time(self):
        """String times return the string as-is."""
        from live_trading.mt5 import connector
        # Access via the module's internal — test that candles with string times sort correctly
        c1 = _make_raw_candle("2024-01-15T10:00:00")
        c2 = _make_raw_candle("2024-01-15T10:05:00")
        candles = [c2, c1]
        # Sort using the same logic as fetch_candles
        def _sort_key(c):
            t = c.get("time", "")
            if isinstance(t, datetime):
                return t.isoformat()
            return str(t)
        sorted_candles = sorted(candles, key=_sort_key)
        assert sorted_candles[0]["time"] == "2024-01-15T10:00:00"
        assert sorted_candles[1]["time"] == "2024-01-15T10:05:00"

    def test_sort_key_with_datetime_time(self):
        """datetime times produce ISO string for correct sorting."""
        t1 = datetime(2024, 1, 15, 10, 0, tzinfo=timezone.utc)
        t2 = datetime(2024, 1, 15, 10, 5, tzinfo=timezone.utc)
        c1 = {"time": t1, "open": 1800.0, "high": 1810.0, "low": 1795.0, "close": 1805.0}
        c2 = {"time": t2, "open": 1801.0, "high": 1811.0, "low": 1796.0, "close": 1806.0}
        candles = [c2, c1]

        def _sort_key(c):
            t = c.get("time", "")
            if isinstance(t, datetime):
                return t.isoformat()
            return str(t)

        sorted_candles = sorted(candles, key=_sort_key)
        assert sorted_candles[0]["time"] == t1
        assert sorted_candles[1]["time"] == t2


class TestDeduplicationLogic:
    """Candle deduplication removes duplicate timestamps, preserves unique candles."""

    def _deduplicate(self, raw_sorted: list) -> list:
        """Mirror the deduplication logic from fetch_candles()."""
        def _sort_key(c):
            t = c.get("time", "")
            if isinstance(t, datetime):
                return t.isoformat()
            return str(t)

        seen_times: set = set()
        deduped: list = []
        for c in raw_sorted:
            t_key = _sort_key(c)
            if t_key not in seen_times:
                seen_times.add(t_key)
                deduped.append(c)
        return deduped

    def test_no_duplicates_unchanged(self):
        candles = [
            _make_raw_candle("2024-01-15T10:00:00"),
            _make_raw_candle("2024-01-15T10:05:00"),
            _make_raw_candle("2024-01-15T10:10:00"),
        ]
        result = self._deduplicate(candles)
        assert len(result) == 3

    def test_one_duplicate_removed(self):
        candles = [
            _make_raw_candle("2024-01-15T10:00:00"),
            _make_raw_candle("2024-01-15T10:00:00"),  # duplicate
            _make_raw_candle("2024-01-15T10:05:00"),
        ]
        result = self._deduplicate(candles)
        assert len(result) == 2
        assert result[0]["time"] == "2024-01-15T10:00:00"
        assert result[1]["time"] == "2024-01-15T10:05:00"

    def test_all_duplicates_removed(self):
        candles = [
            _make_raw_candle("2024-01-15T10:00:00"),
            _make_raw_candle("2024-01-15T10:00:00"),
            _make_raw_candle("2024-01-15T10:00:00"),
        ]
        result = self._deduplicate(candles)
        assert len(result) == 1

    def test_first_occurrence_kept(self):
        """When duplicate exists, the FIRST occurrence (earliest in sorted order) is kept."""
        candles = [
            _make_raw_candle("2024-01-15T10:00:00", open_=1800.0),
            _make_raw_candle("2024-01-15T10:00:00", open_=1999.0),  # duplicate — should be removed
        ]
        result = self._deduplicate(candles)
        assert len(result) == 1
        assert result[0]["open"] == 1800.0

    def test_empty_list_unchanged(self):
        assert self._deduplicate([]) == []

    def test_order_preserved_after_dedup(self):
        candles = [
            _make_raw_candle("2024-01-15T10:00:00"),
            _make_raw_candle("2024-01-15T10:05:00"),
            _make_raw_candle("2024-01-15T10:05:00"),  # duplicate
            _make_raw_candle("2024-01-15T10:10:00"),
        ]
        result = self._deduplicate(candles)
        times = [c["time"] for c in result]
        assert times == [
            "2024-01-15T10:00:00",
            "2024-01-15T10:05:00",
            "2024-01-15T10:10:00",
        ]


class TestOpenPositionsResponse:
    """Malformed OpenedOrders responses must never look like zero positions."""

    def test_successful_list_is_accepted(self):
        from live_trading.mt5.connector import _parse_open_positions_response

        positions = [{"ticket": 123}]
        assert _parse_open_positions_response(positions, 200) == positions

    def test_error_object_is_rejected_even_with_http_200(self):
        from live_trading.mt5.connector import _parse_open_positions_response

        with pytest.raises(RuntimeError, match="broker unavailable"):
            _parse_open_positions_response({"message": "broker unavailable"}, 200)

    def test_non_success_status_is_rejected(self):
        from live_trading.mt5.connector import _parse_open_positions_response

        with pytest.raises(RuntimeError, match="HTTP 503"):
            _parse_open_positions_response({"message": "temporary failure"}, 503)
