from live_trading.server import (
    _annotate_status_scan_telemetry,
    _build_snapshot_from_state,
)


def test_status_does_not_call_connected_waiting_a_ready_scan():
    state = {
        "status": "WAITING",
        "connection_status": "connected",
        "last_decision": None,
        "last_signal_time": None,
    }

    result = _annotate_status_scan_telemetry(state)

    assert result["scan_status"] == "NO_FRESH_SCAN"
    assert result["signal_confirmation_available"] is False


def test_snapshot_marks_account_only_state_as_missing_market_scan():
    result = _build_snapshot_from_state(
        {
            "status": "WAITING",
            "connection_status": "connected",
            "account_info": {"balance": 100.0, "equity": 100.0},
            "last_decision": None,
            "last_signal_time": None,
        }
    )

    assert result["scan_status"] == "NO_FRESH_SCAN"
    assert result["signal_confirmation_available"] is False
    assert "price" in result["scan_missing_fields"]
    assert "timestamp" in result["scan_missing_fields"]


def test_snapshot_allows_confirmation_only_for_complete_scan_with_decision():
    state = {
        "status": "WAITING",
        "connection_status": "connected",
        "account_info": {"balance": 100.0, "equity": 100.0},
        "last_decision": {"allowed": True},
        "last_signal_time": "2026-09-06T22:35:00+00:00",
    }
    signal_snapshot = {
        "candle_time": "2026-09-06T22:35:00+00:00",
        "timestamp": "2026-09-06T22:35:02+00:00",
        "price": 4420.25,
        "regime": "TRENDING",
        "adx": 24.5,
        "atr": 5.125,
        "smc_signal": "BUY",
        "trend": "BULLISH",
    }

    result = _build_snapshot_from_state(state, signal_snapshot)

    assert result["scan_status"] == "READY"
    assert result["scan_missing_fields"] == []
    assert result["signal_confirmation_available"] is True


def test_snapshot_preserves_all_live_positions_and_scan_identity():
    positions = [
        {"ticket": 101, "symbol": "XAUUSD"},
        {"ticket": 102, "symbol": "XAUUSD"},
        {"ticket": 103, "symbol": "XAUUSD"},
    ]
    result = _build_snapshot_from_state(
        {
            "status": "WAITING",
            "connection_status": "connected",
            "account_info": {"balance": 100.0, "equity": 100.0},
            "open_position": positions[0],
            "open_positions": positions,
            "last_decision": {"allowed": False},
        },
        {
            "symbol": "XAUUSD",
            "timeframe": "5m",
            "candle_count": 300,
            "candle_time": "2026-09-07T01:00:00+00:00",
            "timestamp": "2026-09-07T01:00:02+00:00",
            "price": 4400.0,
            "regime": "RANGE",
            "adx": 12.0,
            "atr": 8.0,
            "smc_signal": "NEUTRAL",
            "trend": "BEARISH",
        },
    )

    assert result["open_positions"] == positions
    assert result["symbol"] == "XAUUSD"
    assert result["timeframe"] == "5m"