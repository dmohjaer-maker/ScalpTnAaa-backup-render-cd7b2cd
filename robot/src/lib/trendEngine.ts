// Trend Engine — EMA 50 / 100 / 200
// Optimized for XAUUSD scalping on M1 and M5 timeframes.
//
// Role: GATE — not a confirmation weight.
//       analyzeSmcStructure applies this result as a hard filter:
//       if the EMA trend contradicts the SMC+PA signal, the trade is blocked.
//
// Trend classification:
//   BULLISH  — price > EMA50 AND EMA50 > EMA100 (short/medium alignment)
//   BEARISH  — price < EMA50 AND EMA50 < EMA100 (short/medium alignment)
//   NEUTRAL  — mixed (choppy or transitional market)
//
//   EMA200 is used as a secondary bias layer (strength modifier):
//     STRONG   — all three EMAs are aligned with price direction
//     MODERATE — EMA50/100 aligned but EMA200 contradicts
//     WEAK     — only EMA50 aligned
//
//   The gate blocks trades only when trend === 'BEARISH' vs a BUY signal
//   or 'BULLISH' vs a SELL signal. NEUTRAL allows the trade through with a
//   reduced score (handled in smcEngine).

import { OHLCV } from './goldEngine.js';

// ===== EMA CALCULATION =====
// Standard Exponential Moving Average.
// Uses the first `period` closes as a simple average seed, then applies
// the multiplier forward — same convention as MetaTrader iEMA().

function calcEMA(closes: number[], period: number): number {
  if (closes.length < period) return closes[closes.length - 1]; // not enough data

  const k = 2 / (period + 1);

  // Seed: SMA of the first `period` closes
  let ema = closes.slice(0, period).reduce((a, b) => a + b, 0) / period;

  // Propagate EMA forward
  for (let i = period; i < closes.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
  }

  return +ema.toFixed(4);
}

// ===== OUTPUT =====

export interface TrendResult {
  ema50:    number;
  ema100:   number;
  ema200:   number;
  trend:    'BULLISH' | 'BEARISH' | 'NEUTRAL';
  strength: 'STRONG' | 'MODERATE' | 'WEAK';
}

// ===== MAIN ENTRY POINT =====

export function analyzeTrend(candles: OHLCV[]): TrendResult {
  const closes = candles.map(c => c.close);
  const n      = closes.length;

  // Minimum data requirement: enough for EMA200 to be meaningful
  if (n < 210) {
    // Return a neutral result — the gate will not block anything without enough data
    const last = closes[n - 1] ?? 0;
    return { ema50: last, ema100: last, ema200: last, trend: 'NEUTRAL', strength: 'WEAK' };
  }

  const ema50  = calcEMA(closes, 50);
  const ema100 = calcEMA(closes, 100);
  const ema200 = calcEMA(closes, 200);

  const price = closes[n - 1];

  // ── Primary trend (EMA50 / EMA100 alignment) ──────────────────────────
  // Requires BOTH conditions simultaneously:
  //   1. Price is on the correct side of EMA50 (short-term bias)
  //   2. EMA50 is on the correct side of EMA100 (medium-term bias)
  // This filters out counter-trend pullbacks that could fake the direction.

  let trend: 'BULLISH' | 'BEARISH' | 'NEUTRAL' = 'NEUTRAL';

  const bullish50_100 = price > ema50 && ema50 > ema100;
  const bearish50_100 = price < ema50 && ema50 < ema100;

  if (bullish50_100)       trend = 'BULLISH';
  else if (bearish50_100)  trend = 'BEARISH';

  // ── Strength (EMA200 as multi-session bias) ────────────────────────────
  // STRONG:   All three aligned (price, EMA50, EMA100, EMA200)
  // MODERATE: EMA50/100 aligned, EMA200 lags behind (early trend)
  // WEAK:     NEUTRAL trend, or single EMA alignment only

  let strength: 'STRONG' | 'MODERATE' | 'WEAK' = 'WEAK';

  if (trend === 'BULLISH') {
    strength = ema100 > ema200 ? 'STRONG' : 'MODERATE';
  } else if (trend === 'BEARISH') {
    strength = ema100 < ema200 ? 'STRONG' : 'MODERATE';
  }

  return { ema50, ema100, ema200, trend, strength };
}
