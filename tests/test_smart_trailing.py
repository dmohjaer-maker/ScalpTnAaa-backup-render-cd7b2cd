import unittest

from live_trading.risk.trailing_stop import (
    TrailingConfig,
    compute_smart_trailing_sl,
    should_apply,
)


def candle(open_price, high, low, close):
    return {
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
    }


class SmartTrailingTests(unittest.TestCase):
    def test_buy_trailing_follows_peak_and_locks_profit(self):
        cfg = TrailingConfig(
            activation_r=0.8,
            peak_atr_gap_mult=1.1,
            peak_r_gap_mult=0.6,
            atr_gap_mult=0.5,
        )

        candidate = compute_smart_trailing_sl(
            direction="BUY",
            entry=100.0,
            risk_distance=5.0,
            current_price=118.0,
            peak_price=118.0,
            current_sl=95.0,
            atr=2.0,
            candles=[],
            cfg=cfg,
        )

        self.assertIsNotNone(candidate)
        self.assertGreater(candidate, 100.0)
        self.assertLessEqual(candidate, 117.0)  # close-side ATR safety gap
        self.assertTrue(should_apply("BUY", 95.0, candidate, 0.05))

    def test_buy_peak_is_not_lost_during_a_small_pullback(self):
        cfg = TrailingConfig(peak_atr_gap_mult=1.1, peak_r_gap_mult=0.6)

        first = compute_smart_trailing_sl(
            "BUY", 100.0, 5.0, 118.0, 118.0, 95.0, 2.0, [], cfg,
        )
        after_pullback = compute_smart_trailing_sl(
            "BUY", 100.0, 5.0, 117.0, 118.0, first or 95.0, 2.0, [], cfg,
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(after_pullback)
        self.assertFalse(should_apply("BUY", first, after_pullback, 0.05))

    def test_reversal_confirmation_tightens_candidate(self):
        cfg = TrailingConfig(
            peak_atr_gap_mult=1.1,
            peak_r_gap_mult=0.6,
            atr_gap_mult=0.5,
            reversal_confirmation_bars=2,
            reversal_tighten_atr_mult=0.75,
        )
        candles = [
            candle(100, 103, 99, 102),
            candle(102, 106, 101, 105),
            candle(105, 120, 104, 119),
            candle(120, 121, 118, 119.8),
            candle(119.8, 121, 118.5, 119.6),
        ]

        without_confirmation = compute_smart_trailing_sl(
            "BUY", 100.0, 5.0, 121.0, 121.0, 95.0, 2.0,
            candles[:-2], cfg,
        )
        with_confirmation = compute_smart_trailing_sl(
            "BUY", 100.0, 5.0, 121.0, 121.0, 95.0, 2.0,
            candles, cfg,
        )

        self.assertIsNotNone(without_confirmation)
        self.assertIsNotNone(with_confirmation)
        self.assertGreater(with_confirmation, without_confirmation)

    def test_sell_trailing_moves_down_and_never_locks_a_loss(self):
        cfg = TrailingConfig(activation_r=0.8)
        candidate = compute_smart_trailing_sl(
            "SELL",
            entry=200.0,
            risk_distance=10.0,
            current_price=180.0,
            peak_price=180.0,
            current_sl=210.0,
            atr=3.0,
            candles=[],
            cfg=cfg,
        )

        self.assertIsNotNone(candidate)
        self.assertLess(candidate, 200.0)
        self.assertTrue(should_apply("SELL", 210.0, candidate, 0.05))


if __name__ == "__main__":
    unittest.main()