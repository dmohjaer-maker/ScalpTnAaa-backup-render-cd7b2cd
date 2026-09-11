"""
Price Action Engine — Patterns, S/R Levels, Breakouts
Ported from priceActionEngine.ts — confirmation only.
"""
from dataclasses import dataclass
from typing import List, Literal
from live_trading.signals.gold_engine import OHLCV


@dataclass
class PaConfig:
    pin_bar_wick_ratio: float
    pin_bar_body_max_ratio: float
    strong_body_ratio: float
    strong_body_atr_mult: float
    engulf_body_ratio: float
    level_lookback: int
    level_tolerance: float
    min_level_touches: int
    breakout_min_body: float
    breakout_body_ratio: float
    fake_retrace_bars: int
    pullback_zone_pct: float
    atr_period: int


CFG_M5 = PaConfig(
    pin_bar_wick_ratio=2.0, pin_bar_body_max_ratio=0.30,
    strong_body_ratio=0.60, strong_body_atr_mult=0.40,
    engulf_body_ratio=1.05,
    level_lookback=30, level_tolerance=0.35, min_level_touches=2,
    breakout_min_body=0.20, breakout_body_ratio=0.50,
    fake_retrace_bars=3, pullback_zone_pct=0.0015, atr_period=10,
)


@dataclass
class PriceActionResult:
    bullish_engulf: bool
    bearish_engulf: bool
    bullish_pin_bar: bool
    bearish_pin_bar: bool
    strong_bullish: bool
    strong_bearish: bool
    near_demand_zone: bool
    near_supply_zone: bool
    near_support: bool
    near_resistance: bool
    valid_bull_breakout: bool
    valid_bear_breakout: bool
    fake_bull_breakout: bool
    fake_bear_breakout: bool
    bullish_pullback: bool
    bearish_pullback: bool
    pa_signal: Literal["BUY", "SELL", "NEUTRAL"]
    pa_score: float


def _calc_atr(candles: List[OHLCV], period: int) -> float:
    sl = candles[-(period + 1):]
    total = 0.0
    for i in range(1, len(sl)):
        c, p = sl[i], sl[i - 1]
        total += max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
    return total / period if period > 0 else 0.0


def _body(c: OHLCV) -> float: return abs(c.close - c.open)
def _range(c: OHLCV) -> float: return c.high - c.low
def _body_ratio(c: OHLCV) -> float: r = _range(c); return _body(c) / r if r > 0 else 0.0
def _is_bull(c: OHLCV) -> bool: return c.close > c.open
def _is_bear(c: OHLCV) -> bool: return c.close < c.open
def _upper_wick(c: OHLCV) -> float: return c.high - max(c.open, c.close)
def _lower_wick(c: OHLCV) -> float: return min(c.open, c.close) - c.low


def _detect_patterns(candles: List[OHLCV], cfg: PaConfig, atr: float):
    n  = len(candles)
    c0 = candles[n - 1]
    c1 = candles[n - 2]

    c0b, c1b = _body(c0), _body(c1)

    bull_engulf = (_is_bull(c0) and _is_bear(c1) and
                   c0.open <= c1.close and c0.close >= c1.open and
                   c0b >= c1b * cfg.engulf_body_ratio)
    bear_engulf = (_is_bear(c0) and _is_bull(c1) and
                   c0.open >= c1.close and c0.close <= c1.open and
                   c0b >= c1b * cfg.engulf_body_ratio)

    r0      = _range(c0)
    min_sz  = atr * 0.25

    bull_pin = (r0 >= min_sz and
                _body_ratio(c0) <= cfg.pin_bar_body_max_ratio and
                _lower_wick(c0) >= c0b * cfg.pin_bar_wick_ratio and
                _upper_wick(c0) <= _lower_wick(c0) * 0.4)
    bear_pin = (r0 >= min_sz and
                _body_ratio(c0) <= cfg.pin_bar_body_max_ratio and
                _upper_wick(c0) >= c0b * cfg.pin_bar_wick_ratio and
                _lower_wick(c0) <= _upper_wick(c0) * 0.4)

    top_of_range    = c0.low + r0 * 0.80
    bottom_of_range = c0.low + r0 * 0.20

    strong_bull = (_is_bull(c0) and
                   _body_ratio(c0) >= cfg.strong_body_ratio and
                   c0b >= atr * cfg.strong_body_atr_mult and
                   c0.close >= top_of_range)
    strong_bear = (_is_bear(c0) and
                   _body_ratio(c0) >= cfg.strong_body_ratio and
                   c0b >= atr * cfg.strong_body_atr_mult and
                   c0.close <= bottom_of_range)

    return bull_engulf, bear_engulf, bull_pin, bear_pin, strong_bull, strong_bear


def _detect_sr_levels(candles: List[OHLCV], cfg: PaConfig):
    sl = candles[-cfg.level_lookback:]
    n  = len(sl)
    tol = cfg.level_tolerance

    swing_highs, swing_lows = [], []
    for i in range(2, n - 2):
        if (sl[i].high > sl[i-1].high and sl[i].high > sl[i-2].high and
                sl[i].high > sl[i+1].high and sl[i].high > sl[i+2].high):
            swing_highs.append(sl[i].high)
        if (sl[i].low < sl[i-1].low and sl[i].low < sl[i-2].low and
                sl[i].low < sl[i+1].low and sl[i].low < sl[i+2].low):
            swing_lows.append(sl[i].low)

    def cluster(prices):
        confirmed, used = [], set()
        for i, p in enumerate(prices):
            if i in used: continue
            grp = [p]
            for j in range(i + 1, len(prices)):
                if j not in used and abs(prices[j] - p) <= tol:
                    grp.append(prices[j])
                    used.add(j)
            used.add(i)
            if len(grp) >= cfg.min_level_touches:
                confirmed.append(sum(grp) / len(grp))
        return confirmed

    return cluster(swing_lows), cluster(swing_highs)


def _detect_supply_demand(candles: List[OHLCV], cfg: PaConfig, atr: float):
    sl = candles[-cfg.level_lookback:]
    n  = len(sl)
    demand_zones, supply_zones = [], []

    for i in range(3, n - 1):
        impulse   = sl[i]
        base_cands = sl[i - 3:i]
        base_high  = max(c.high for c in base_cands)
        base_low   = min(c.low  for c in base_cands)
        base_range = base_high - base_low
        if base_range > atr * 1.5: continue

        imp_b = _body(impulse)
        imp_r = _range(impulse)
        is_strong = (imp_r > 0 and
                     imp_b / imp_r >= cfg.strong_body_ratio and
                     imp_b >= atr * 0.5)
        if not is_strong: continue

        if _is_bull(impulse):
            demand_zones.append({"top": round(base_high, 2), "bottom": round(base_low, 2)})
        elif _is_bear(impulse):
            supply_zones.append({"top": round(base_high, 2), "bottom": round(base_low, 2)})

    return demand_zones[-3:], supply_zones[-3:]


def _detect_breakout_pullback(
    candles, support_lvls, resistance_lvls, demand_zones, supply_zones,
    cfg: PaConfig, atr: float
):
    n = len(candles)
    curr = candles[n - 1]
    cp   = curr.close

    vbull = vbear = fbull = fbear = bull_pb = bear_pb = False

    for level in resistance_lvls:
        if (cp > level and _body(curr) >= cfg.breakout_min_body and
                _body_ratio(curr) >= cfg.breakout_body_ratio and _is_bull(curr)):
            vbull = True
        recent = candles[n - cfg.fake_retrace_bars - 1: n - 1]
        for rc in recent:
            if rc.close > level and cp < level: fbull = True

    for level in support_lvls:
        if (cp < level and _body(curr) >= cfg.breakout_min_body and
                _body_ratio(curr) >= cfg.breakout_body_ratio and _is_bear(curr)):
            vbear = True
        recent = candles[n - cfg.fake_retrace_bars - 1: n - 1]
        for rc in recent:
            if rc.close < level and cp > level: fbear = True

    look = min(10, n - 1)
    prior = candles[n - 1 - look].close
    trend_up   = cp > prior * 1.002
    trend_down = cp < prior * 0.998
    pull_zone  = cp * cfg.pullback_zone_pct

    if trend_up:
        near_sup = any(abs(cp - l) <= pull_zone for l in support_lvls)
        near_dem = any(z["bottom"] - pull_zone <= cp <= z["top"] + pull_zone
                       for z in demand_zones)
        if near_sup or near_dem: bull_pb = True

    if trend_down:
        near_res = any(abs(cp - l) <= pull_zone for l in resistance_lvls)
        near_sup_z = any(z["bottom"] - pull_zone <= cp <= z["top"] + pull_zone
                         for z in supply_zones)
        if near_res or near_sup_z: bear_pb = True

    return vbull, vbear, fbull, fbear, bull_pb, bear_pb


def _compute_pa_signal(
    bull_engulf, bear_engulf, bull_pin, bear_pin, strong_bull, strong_bear,
    vbull, vbear, fbull, fbear, bull_pb, bear_pb,
    near_demand, near_supply, near_support, near_resist,
):
    buy = sell = 0.0

    if bull_engulf: buy  += 1.5
    if bear_engulf: sell += 1.5
    if bull_pin:    buy  += 1.5 if (near_support or near_demand) else 0.8
    if bear_pin:    sell += 1.5 if (near_resist or near_supply)  else 0.8
    if strong_bull: buy  += 1.0
    if strong_bear: sell += 1.0
    if vbull:       buy  += 1.0
    if vbear:       sell += 1.0
    if bull_pb:     buy  += 0.8
    if bear_pb:     sell += 0.8
    if near_demand or near_support: buy  += 0.5
    if near_supply or near_resist:  sell += 0.5
    if fbull: buy  -= 1.5
    if fbear: sell -= 1.5

    buy  = max(0.0, buy)
    sell = max(0.0, sell)

    max_possible = 5.3
    dominant = max(buy, sell)
    score = round(dominant / max_possible, 2)

    if buy > sell and score >= 0.30:   signal = "BUY"
    elif sell > buy and score >= 0.30: signal = "SELL"
    else:                               signal = "NEUTRAL"
    return signal, min(1.0, score)


_NEUTRAL_PA = PriceActionResult(
    bullish_engulf=False, bearish_engulf=False,
    bullish_pin_bar=False, bearish_pin_bar=False,
    strong_bullish=False, strong_bearish=False,
    near_demand_zone=False, near_supply_zone=False,
    near_support=False, near_resistance=False,
    valid_bull_breakout=False, valid_bear_breakout=False,
    fake_bull_breakout=False, fake_bear_breakout=False,
    bullish_pullback=False, bearish_pullback=False,
    pa_signal="NEUTRAL", pa_score=0.0,
)


def analyze_price_action(candles: List[OHLCV]) -> PriceActionResult:
    cfg = CFG_M5
    if len(candles) < cfg.level_lookback + cfg.atr_period + 5:
        return _NEUTRAL_PA

    atr = _calc_atr(candles, cfg.atr_period)
    if atr <= 0:
        return _NEUTRAL_PA

    be, bae, bp, bap, sb, sbe = _detect_patterns(candles, cfg, atr)
    support_lvls, resistance_lvls = _detect_sr_levels(candles, cfg)
    demand_zones, supply_zones    = _detect_supply_demand(candles, cfg, atr)

    cp  = candles[-1].close
    tol = cfg.level_tolerance
    near_sup  = any(abs(cp - l) <= tol for l in support_lvls)
    near_res  = any(abs(cp - l) <= tol for l in resistance_lvls)
    near_dem  = any(z["bottom"] - tol <= cp <= z["top"] + tol for z in demand_zones)
    near_supl = any(z["bottom"] - tol <= cp <= z["top"] + tol for z in supply_zones)

    vbull, vbear, fbull, fbear, bull_pb, bear_pb = _detect_breakout_pullback(
        candles, support_lvls, resistance_lvls, demand_zones, supply_zones, cfg, atr)

    signal, score = _compute_pa_signal(
        be, bae, bp, bap, sb, sbe,
        vbull, vbear, fbull, fbear, bull_pb, bear_pb,
        near_dem, near_supl, near_sup, near_res,
    )

    return PriceActionResult(
        bullish_engulf=be, bearish_engulf=bae,
        bullish_pin_bar=bp, bearish_pin_bar=bap,
        strong_bullish=sb, strong_bearish=sbe,
        near_demand_zone=near_dem, near_supply_zone=near_supl,
        near_support=near_sup, near_resistance=near_res,
        valid_bull_breakout=vbull, valid_bear_breakout=vbear,
        fake_bull_breakout=fbull, fake_bear_breakout=fbear,
        bullish_pullback=bull_pb, bearish_pullback=bear_pb,
        pa_signal=signal,  # type: ignore
        pa_score=score,
    )
