"""
Wyckoff Analysis Engine
Ported from wyckoffEngine.ts — confirmation only, never triggers alone.
"""
from dataclasses import dataclass
from typing import List, Literal, Optional
from live_trading.signals.gold_engine import OHLCV
from live_trading.symbols import is_eurusd, wyckoff_baseline

# ── Config ────────────────────────────────────────────────────────────────────

@dataclass
class WyckoffConfig:
    range_bars: int
    trend_bars: int
    spring_margin: float
    upthrust_margin: float
    min_range_touches: int
    max_range_pct: float
    min_range_pct: float
    recent_bars: int


CFG_M5 = WyckoffConfig(
    range_bars=20, trend_bars=12,
    spring_margin=0.20, upthrust_margin=0.20,
    min_range_touches=2,
    max_range_pct=0.010, min_range_pct=0.001,
    recent_bars=6,
)

# Runtime-calibrated config (set by calibrate_wyckoff()), isolated by symbol.
_calibrated_by_symbol: dict[str, WyckoffConfig] = {}


def _base_config(symbol: str) -> WyckoffConfig:
    values = wyckoff_baseline(symbol)
    return WyckoffConfig(
        range_bars=20,
        trend_bars=12,
        spring_margin=float(values["spring_margin"]),
        upthrust_margin=float(values["upthrust_margin"]),
        min_range_touches=2,
        max_range_pct=0.010,
        min_range_pct=0.001,
        recent_bars=6,
    )


def calibrate_wyckoff(candles: List[OHLCV], symbol: str = "XAUUSD") -> WyckoffConfig:
    """Derive WyckoffConfig from real OHLCV data (mirrors calibrateM5Config)."""
    n = len(candles)
    if n < 200:
        return _base_config(symbol)

    # Median 14-bar ATR (sampled every 30 bars)
    atrs = []
    for i in range(20, n, 30):
        lo = max(1, i - 13)
        total, cnt = 0.0, 0
        for j in range(lo, i + 1):
            c, p = candles[j], candles[j - 1]
            total += max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
            cnt += 1
        if cnt > 0:
            atrs.append(total / cnt)
    atrs.sort()
    median_atr = atrs[int(len(atrs) * 0.50)] if atrs else 5.0

    # Rolling 20-bar range/price percentiles
    range_pcts = []
    for i in range(32, n, 10):
        sl = candles[i - 20:i]
        hi = max(c.high for c in sl)
        lo_p = min(c.low for c in sl)
        price = sl[-1].close
        if price > 0:
            range_pcts.append((hi - lo_p) / price)
    range_pcts.sort()

    p85 = range_pcts[int(len(range_pcts) * 0.85)] if range_pcts else 0.010
    margin = round(median_atr * 0.80, 5 if is_eurusd(symbol) else 2)

    return WyckoffConfig(
        range_bars=20, trend_bars=12,
        spring_margin=margin, upthrust_margin=margin,
        min_range_touches=2,
        max_range_pct=round(p85, 5), min_range_pct=0.0005,
        recent_bars=8,
    )


def set_calibrated_config(cfg: WyckoffConfig, symbol: str = "XAUUSD") -> None:
    _calibrated_by_symbol[symbol.upper()] = cfg


def _get_cfg(symbol: str = "XAUUSD") -> WyckoffConfig:
    return _calibrated_by_symbol.get(symbol.upper(), _base_config(symbol))


# ── Output ────────────────────────────────────────────────────────────────────

@dataclass
class WyckoffResult:
    phase: Literal["ACCUMULATION", "DISTRIBUTION", "NEUTRAL"]
    spring: bool
    upthrust: bool
    volume_confirmed: bool
    wyckoff_signal: Literal["BUY", "SELL", "NEUTRAL"]
    wyckoff_score: float   # 0–1


# ── Helpers ───────────────────────────────────────────────────────────────────

def _avg(values: List[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _detect_phase(candles: List[OHLCV], cfg: WyckoffConfig):
    n = len(candles)
    if n < cfg.range_bars + cfg.trend_bars:
        return "NEUTRAL", 0.0, 0.0, 0

    range_start = n - cfg.range_bars
    range_candles = candles[range_start:]

    support    = min(c.low  for c in range_candles)
    resistance = max(c.high for c in range_candles)
    range_size = resistance - support
    mid_price  = candles[-1].close

    range_pct = range_size / mid_price if mid_price > 0 else 0
    if not (cfg.min_range_pct <= range_pct <= cfg.max_range_pct):
        return "NEUTRAL", support, resistance, range_start

    touch_band = range_size * 0.20
    top_band   = resistance - touch_band
    bot_band   = support    + touch_band

    top_touches = sum(1 for c in range_candles if c.high >= top_band)
    bot_touches = sum(1 for c in range_candles if c.low  <= bot_band)

    if top_touches < cfg.min_range_touches or bot_touches < cfg.min_range_touches:
        return "NEUTRAL", support, resistance, range_start

    trend_start   = range_start - cfg.trend_bars
    trend_candles = candles[max(0, trend_start):range_start]
    if len(trend_candles) < 4:
        return "NEUTRAL", support, resistance, range_start

    trend_move = trend_candles[-1].close - trend_candles[0].close
    trend_pct  = abs(trend_move) / trend_candles[0].close if trend_candles[0].close > 0 else 0

    if trend_pct >= 0.002:
        phase = "ACCUMULATION" if trend_move < 0 else "DISTRIBUTION"
    else:
        phase = "NEUTRAL"

    return phase, support, resistance, range_start


def _detect_spring(candles: List[OHLCV], range_start: int, cfg: WyckoffConfig) -> bool:
    n = len(candles)
    range_candles = candles[range_start:n]
    total_bars    = len(range_candles)

    establish_bars = max(4, total_bars - cfg.recent_bars)
    if establish_bars <= 0:
        return False

    early_range = range_candles[:establish_bars]
    support = min(c.low for c in early_range)
    avg_vol = _avg([c.volume for c in range_candles])

    scan_start = range_start + establish_bars
    for i in range(scan_start, n):
        c = candles[i]
        if (c.low < support and
                c.low >= support - cfg.spring_margin and
                c.close > support and
                c.volume > avg_vol):
            return True
    return False


def _detect_upthrust(candles: List[OHLCV], range_start: int, cfg: WyckoffConfig) -> bool:
    n = len(candles)
    range_candles = candles[range_start:n]
    total_bars    = len(range_candles)

    establish_bars = max(4, total_bars - cfg.recent_bars)
    if establish_bars <= 0:
        return False

    early_range  = range_candles[:establish_bars]
    resistance   = max(c.high for c in early_range)
    avg_vol      = _avg([c.volume for c in range_candles])

    scan_start = range_start + establish_bars
    for i in range(scan_start, n):
        c = candles[i]
        if (c.high > resistance and
                c.high <= resistance + cfg.upthrust_margin and
                c.close < resistance and
                c.volume > avg_vol):
            return True
    return False


def _confirm_volume(candles: List[OHLCV], phase: str, range_start: int) -> bool:
    if phase == "NEUTRAL":
        return False
    range_candles = candles[range_start:]
    up_vol = sum(c.volume for c in range_candles if c.close > c.open)
    dn_vol = sum(c.volume for c in range_candles if c.close <= c.open)
    total  = up_vol + dn_vol
    if total == 0:
        return False
    if phase == "ACCUMULATION":
        return (up_vol / total) > 0.55
    return (dn_vol / total) > 0.55


# ── Main ──────────────────────────────────────────────────────────────────────

_NEUTRAL = WyckoffResult(
    phase="NEUTRAL", spring=False, upthrust=False,
    volume_confirmed=False, wyckoff_signal="NEUTRAL", wyckoff_score=0.0
)


def analyze_wyckoff(candles: List[OHLCV], symbol: str = "XAUUSD") -> WyckoffResult:
    cfg = _get_cfg(symbol)

    if len(candles) < cfg.range_bars + cfg.trend_bars:
        return _NEUTRAL

    phase, _sup, _res, range_start = _detect_phase(candles, cfg)
    if phase == "NEUTRAL":
        return _NEUTRAL

    spring    = _detect_spring(candles, range_start, cfg)
    upthrust  = _detect_upthrust(candles, range_start, cfg)
    vol_conf  = _confirm_volume(candles, phase, range_start)

    # Contradiction check
    contradiction = (
        (phase == "ACCUMULATION" and upthrust and not spring) or
        (phase == "DISTRIBUTION" and spring and not upthrust)
    )
    if contradiction:
        return WyckoffResult(phase=phase, spring=spring, upthrust=upthrust,  # type: ignore
                             volume_confirmed=vol_conf,
                             wyckoff_signal="NEUTRAL", wyckoff_score=0.0)

    # A phase describes context, not an entry trigger. Require both the
    # directional event (Spring/Upthrust) and directional volume confirmation
    # before Wyckoff can cast a BUY/SELL vote. This prevents a phase-only
    # classification from authorizing an entry in a still-unresolved range.
    confirmed_event = spring if phase == "ACCUMULATION" else upthrust
    if not confirmed_event or not vol_conf:
        return WyckoffResult(
            phase=phase, spring=spring, upthrust=upthrust,  # type: ignore
            volume_confirmed=vol_conf,
            wyckoff_signal="NEUTRAL", wyckoff_score=0.0,
        )

    score_raw = 1.0
    signal = "BUY" if phase == "ACCUMULATION" else "SELL"

    return WyckoffResult(
        phase=phase, spring=spring, upthrust=upthrust,  # type: ignore
        volume_confirmed=vol_conf,
        wyckoff_signal=signal,  # type: ignore
        wyckoff_score=round(min(1.0, score_raw), 2),
    )
