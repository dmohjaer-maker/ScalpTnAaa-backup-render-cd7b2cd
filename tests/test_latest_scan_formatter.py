from telegram_panel.api.formatters.messages import MessageFormatter


def _complete_snapshot(**overrides):
    snapshot = {
        "connection_status": "connected",
        "candle_time": "2026-09-06T22:35:00+00:00",
        "timestamp": "2026-09-06T22:35:02+00:00",
        "price": 4420.25,
        "trend": "BULLISH",
        "regime": "TRENDING",
        "smc_signal": "BUY",
        "adx": 24.5,
        "atr": 5.125,
        "open_positions": [],
        "recent_trades": [],
    }
    snapshot.update(overrides)
    return snapshot


def test_connected_account_without_market_data_is_not_reported_as_valid_signal():
    text = MessageFormatter.latest_scan(
        {
            "connection_status": "connected",
            "status": "WAITING",
            "_fetched_at": "2026-09-06T22:36:43+00:00",
        }
    )

    assert "No fresh market scan is available." in text
    assert "Signal confirmation is disabled" in text
    assert "Valid signal; entry conditions confirmed." not in text


def test_complete_scan_without_decision_does_not_confirm_entry():
    text = MessageFormatter.latest_scan(_complete_snapshot())

    assert "No signal decision was recorded for this scan." in text
    assert "Valid signal; entry conditions confirmed." not in text


def test_complete_scan_with_allowed_decision_can_confirm_entry():
    text = MessageFormatter.latest_scan(
        _complete_snapshot(
            symbol="XAUUSD",
            timeframe="M5",
            last_decision={
                "allowed": True,
                "blocked_reasons": [],
                "reasoning": [],
            }
        )
    )

    assert "Valid signal; entry conditions confirmed." in text
    assert "Symbol:" in text
    assert "XAUUSD" in text
    assert "M5" in text


def test_incomplete_scan_explains_candle_warmup_and_symbol():
    text = MessageFormatter.latest_scan(
        {
            "connection_status": "connected",
            "symbol": "XAUUSD",
            "timeframe": "M5",
            "scan_telemetry": {
                "status": "INSUFFICIENT_CANDLES",
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "candle_count": 6,
            },
        }
    )

    assert "No fresh market scan is available." in text
    assert "XAUUSD" in text
    assert "6/50" in text