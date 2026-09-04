"""Regression tests for false-positive strategy authorization.

These tests protect the two strategy-level failure modes found in production:
1. a structure event being promoted despite a NEUTRAL/conflicting composite SMC;
2. Wyckoff phase context being promoted to a trade without event + volume proof.
"""

from live_trading.signals import decision_engine
from live_trading.signals.decision_engine import run_decision_engine
from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import SmcBos, SmcResult
from live_trading.signals.trend_engine import TrendResult
from live_trading.signals.wyckoff_engine import analyze_wyckoff


def _smc_with_candidate(candidate: str, composite: str) -> SmcResult:
    event = SmcBos(candidate, 100.0, 20, "2026-09-04T12:00:00+00:00")
    return SmcResult(
        timeframe="M5",
        timestamp=event.time,
        current_price=100.0,
        trend="NEUTRAL",
        bos_signals=[event],
        choch_signals=[],
        order_blocks=[],
        fair_value_gaps=[],
        liquidity_sweeps=[],
        equal_highs=[],
        equal_lows=[],
        mitigation_blocks=[],
        smc_signal=composite,
        smc_score=0.0,
    )


def test_decision_engine_blocks_structure_candidate_when_smc_is_neutral(monkeypatch):
    smc = _smc_with_candidate("BUY", "NEUTRAL")
    monkeypatch.setattr(decision_engine, "analyze_smc_structure", lambda _: smc)
    monkeypatch.setattr(decision_engine, "analyze_wyckoff", lambda _: None)
    monkeypatch.setattr(decision_engine, "analyze_price_action", lambda _: None)
    monkeypatch.setattr(decision_engine, "analyze_trend", lambda _: None)

    result = run_decision_engine([], account_balance=10_000.0)

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert "SMC composite is NEUTRAL" in result.blocked_reasons[0]


def test_decision_engine_blocks_counter_trend_candidate(monkeypatch):
    smc = _smc_with_candidate("BUY", "BUY")
    bearish_trend = TrendResult(
        ema50=101.0, ema100=102.0, ema200=103.0,
        trend="BEARISH", strength="STRONG",
    )
    monkeypatch.setattr(decision_engine, "analyze_smc_structure", lambda _: smc)
    monkeypatch.setattr(decision_engine, "analyze_wyckoff", lambda _: None)
    monkeypatch.setattr(decision_engine, "analyze_price_action", lambda _: None)
    monkeypatch.setattr(decision_engine, "analyze_trend", lambda _: bearish_trend)

    result = run_decision_engine([], account_balance=10_000.0)

    assert result.allowed is False
    assert result.direction == "NEUTRAL"
    assert "counter-trend entry blocked" in result.blocked_reasons[0]


def _phase_only_candles(prior_direction: str) -> list[OHLCV]:
    candles: list[OHLCV] = []
    for i in range(12):
        if prior_direction == "DOWN":
            close = 102.0 - i * 0.025
        else:
            close = 100.0 + i * 0.025
        candles.append(
            OHLCV(
                time=f"2026-09-04T12:{i:02d}:00+00:00",
                open=close + 0.01,
                high=close + 0.03,
                low=close - 0.03,
                close=close,
                volume=100.0,
            )
        )

    # A narrow 20-bar range with repeated touches, but no Spring/Upthrust.
    # Alternating candle bodies keep directional volume exactly balanced.
    for j in range(20):
        close = 100.5 if j % 2 == 0 else 100.7
        high = 101.0 if j in (1, 5, 9, 13, 17) else 100.8
        low = 100.0 if j in (0, 4, 8, 12, 16) else 100.3
        bullish_body = j % 2 == 0
        open_price = close - 0.01 if bullish_body else close + 0.01
        candles.append(
            OHLCV(
                time=f"2026-09-04T12:{12 + j:02d}:00+00:00",
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=100.0,
            )
        )
    return candles


def test_wyckoff_accumulation_phase_alone_is_not_a_buy_vote():
    result = analyze_wyckoff(_phase_only_candles("DOWN"))

    assert result.phase == "ACCUMULATION"
    assert result.spring is False
    assert result.volume_confirmed is False
    assert result.wyckoff_signal == "NEUTRAL"


def test_wyckoff_distribution_phase_alone_is_not_a_sell_vote():
    result = analyze_wyckoff(_phase_only_candles("UP"))

    assert result.phase == "DISTRIBUTION"
    assert result.upthrust is False
    assert result.volume_confirmed is False
    assert result.wyckoff_signal == "NEUTRAL"
