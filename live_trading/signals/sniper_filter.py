"""
Sniper Entry Filter — GoldScalperPro v4

Three-gate precision entry system that transforms the robot from
"enter on bar close" to "wait for institutional-grade confluence":

  Gate 1 — Displacement Candle
    The trigger candle must close strong in the trade direction.
    A weak / indecisive close = market not yet committed.
    Threshold: body ≥ SNIPER_DISPLACEMENT_MIN of total candle range.

  Gate 2 — Structural Zone (FVG or Order Block)
    Price must be AT or INSIDE a smart money zone in the trade direction:
      • Fair Value Gap (FVG) — unfilled imbalance where institutions left
        limit orders.  Price returning here = highest-probability entry.
      • Order Block (OB)  — last bearish candle before a BUY BOS, or last
        bullish candle before a SELL BOS.  The "origin" of the move.
    If no zones exist in that direction, the gate is skipped (fail-safe):
    we never block a trade because the market lacks structure; we only
    block when structure clearly says "not yet".

  Gate 3 — Recent Liquidity Sweep Bonus (non-blocking)
    If there was a liquidity sweep in the trade direction within the last
    SNIPER_SWEEP_LOOKBACK bars, that's extra confluence.  Logged but does
    not block entries that passed gates 1 + 2 without it.

CHoCH Fast-Pass:
    A Change of Character (CHoCH) in the last 3 bars is the strongest SMC
    signal — it represents an actual structural reversal, not just a break.
    When a recent CHoCH is present, gates 1 + 2 are relaxed (only the
    displacement candle direction must agree) because the CHoCH itself
    provides the structural confirmation.

Fail-safe:
    Any exception → SniperResult(allowed=True) — never silently blocks a
    valid trade due to a code error.

Design principles (same as MTF filter):
    • SNIPER_ENABLED=false → identical behaviour to pre-sniper codebase.
    • Purely additive: imports from existing engines, never mutates them.
    • All thresholds are env-configurable (no redeploy needed to tune).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from live_trading.signals.gold_engine import OHLCV
from live_trading.signals.smc_engine import SmcResult
from live_trading.config import (
    SNIPER_DISPLACEMENT_MIN,
    SNIPER_REQUIRE_ZONE,
    SNIPER_SWEEP_LOOKBACK,
)


# ── Result ────────────────────────────────────────────────────────────────────

@dataclass
class SniperResult:
    """
    Outcome of sniper_entry_allowed().

    allowed          : True = trade may proceed.
    reason           : Human-readable block reason when allowed=False, else "".
    displacement_ok  : Whether the trigger candle was a strong displacement.
    in_fvg           : Whether current price is inside an active FVG.
    in_ob            : Whether current price is inside an active Order Block.
    has_recent_sweep : Whether a liquidity sweep occurred in recent bars.
    choch_fastpass   : True if a recent CHoCH relaxed gates 1+2.
    details          : Ordered list of explanation strings (for logging).
    """
    allowed:           bool
    reason:            str            = ""
    displacement_ok:   bool           = False
    in_fvg:            bool           = False
    in_ob:             bool           = False
    has_recent_sweep:  bool           = False
    choch_fastpass:    bool           = False
    details:           List[str]      = field(default_factory=list)


def _pass(details: List[str], **kw) -> SniperResult:
    return SniperResult(allowed=True, reason="", details=details, **kw)

def _block(reason: str, details: List[str], **kw) -> SniperResult:
    return SniperResult(allowed=False, reason=reason, details=details, **kw)


# ── Gate helpers ──────────────────────────────────────────────────────────────

def _displacement_ok(candles: List[OHLCV], direction: str) -> tuple[bool, str]:
    """
    Gate 1: is the trigger candle a displacement candle?

    A displacement candle:
      • closes in the trade direction
      • body ≥ SNIPER_DISPLACEMENT_MIN of (high - low)
      • body is at least as large as 60% of the 10-bar average body
        (eliminates tiny candles that happen to have a high ratio)
    """
    if len(candles) < 11:
        return True, "Insufficient candles for displacement check — skipped"

    c      = candles[-1]
    body   = abs(c.close - c.open)
    rng    = c.high - c.low

    # Micro candle guard
    if rng < 0.05:
        return False, f"Micro candle (range={rng:.3f}) — no displacement"

    # Direction: close must agree with trade
    if direction == "BUY" and c.close < c.open:
        return False, f"Displacement FAIL: last candle is bearish (close={c.close:.2f} < open={c.open:.2f}) for BUY"
    if direction == "SELL" and c.close > c.open:
        return False, f"Displacement FAIL: last candle is bullish (close={c.close:.2f} > open={c.open:.2f}) for SELL"

    # Body ratio
    ratio = body / rng
    if ratio < SNIPER_DISPLACEMENT_MIN:
        return False, (
            f"Displacement FAIL: body ratio {ratio:.2f} < {SNIPER_DISPLACEMENT_MIN:.2f} "
            f"(body={body:.2f}, range={rng:.2f})"
        )

    # Minimum body size relative to recent average
    avg_body = sum(abs(x.close - x.open) for x in candles[-11:-1]) / 10
    if avg_body > 0 and body < avg_body * 0.55:
        return False, (
            f"Displacement FAIL: body {body:.2f} < 55% of avg body {avg_body:.2f} "
            f"— candle too small to signal institutional entry"
        )

    return True, (
        f"Displacement OK: body_ratio={ratio:.2f}  "
        f"body={body:.2f}  avg_body={avg_body:.2f}  dir={direction}"
    )


def _zone_check(
    candles: List[OHLCV],
    smc: SmcResult,
    direction: str,
) -> tuple[bool, bool, bool, str]:
    """
    Gate 2: is price in a structural zone (FVG or Order Block)?

    Returns (zone_ok, in_fvg, in_ob, explanation).
    zone_ok=True when at least one zone is hit, or when no zones exist at all.
    """
    price = candles[-1].close

    # ── FVG check ─────────────────────────────────────────────────────────────
    active_fvgs = [f for f in smc.fair_value_gaps if not f.filled]
    dir_fvgs = [
        f for f in active_fvgs
        if (direction == "BUY"  and f.type == "BULLISH")
        or (direction == "SELL" and f.type == "BEARISH")
    ]
    in_fvg = any(f.bottom <= price <= f.top for f in dir_fvgs)

    # ── Order Block check ──────────────────────────────────────────────────────
    active_obs = [ob for ob in smc.order_blocks if not ob.mitigated]
    dir_obs = [
        ob for ob in active_obs
        if (direction == "BUY"  and ob.type == "BULLISH")
        or (direction == "SELL" and ob.type == "BEARISH")
    ]
    in_ob = any(ob.low <= price <= ob.high for ob in dir_obs)

    # ── No zones at all — fail-safe pass ──────────────────────────────────────
    if not dir_fvgs and not dir_obs:
        return True, False, False, (
            f"Zone check SKIPPED: no active FVGs or OBs for {direction} "
            f"(fail-safe — gate not applicable)"
        )

    if in_fvg:
        # Find which FVG
        fvg = next(f for f in dir_fvgs if f.bottom <= price <= f.top)
        msg = (
            f"Zone OK: price {price:.2f} inside {direction} FVG "
            f"[{fvg.bottom:.2f}–{fvg.top:.2f}]"
        )
        return True, True, in_ob, msg

    if in_ob:
        ob = next(o for o in dir_obs if o.low <= price <= o.high)
        msg = (
            f"Zone OK: price {price:.2f} inside {direction} OB "
            f"[{ob.low:.2f}–{ob.high:.2f}]"
        )
        return True, False, True, msg

    # Zones exist but price not in any of them
    fvg_summary = ", ".join(
        f"[{f.bottom:.2f}–{f.top:.2f}]" for f in dir_fvgs[:3]
    ) or "none"
    ob_summary  = ", ".join(
        f"[{o.low:.2f}–{o.high:.2f}]" for o in dir_obs[:3]
    ) or "none"
    msg = (
        f"Zone FAIL: price {price:.2f} not in any active zone — "
        f"FVGs: {fvg_summary}  OBs: {ob_summary}"
    )
    return False, False, False, msg


def _sweep_bonus(candles: List[OHLCV], smc: SmcResult, direction: str) -> tuple[bool, str]:
    """Gate 3 (non-blocking): recent liquidity sweep in trade direction."""
    n             = len(candles)
    lookback      = SNIPER_SWEEP_LOOKBACK
    recent_sweeps = [
        s for s in smc.liquidity_sweeps
        if n - 1 - s.bar_index <= lookback
    ]
    dir_sweeps = [
        s for s in recent_sweeps
        if (direction == "BUY"  and s.type == "BULLISH")
        or (direction == "SELL" and s.type == "BEARISH")
    ]
    if dir_sweeps:
        s = dir_sweeps[-1]
        bars_ago = n - 1 - s.bar_index
        return True, (
            f"Liquidity sweep {bars_ago} bar(s) ago "
            f"at {s.swept_level:.2f} — institutional trap confirmed"
        )
    return False, f"No recent {direction} liquidity sweep in last {lookback} bars (non-critical)"


def _recent_choch(candles: List[OHLCV], smc: SmcResult, direction: str) -> bool:
    """True if there's a CHoCH in the last 3 bars matching the trade direction."""
    n = len(candles)
    return any(
        ch.type == direction and (n - 1 - ch.bar_index) <= 3
        for ch in smc.choch_signals
    )


# ── Main public function ──────────────────────────────────────────────────────

def sniper_entry_allowed(
    candles: List[OHLCV],
    smc: SmcResult,
    direction: str,
) -> SniperResult:
    """
    Run all three sniper gates and return the combined result.

    Parameters
    ----------
    candles   : Closed OHLCV candles on the trade timeframe.
    smc       : SmcResult from analyze_smc_structure() — already computed by
                the decision engine, so this function adds zero extra latency.
    direction : "BUY" or "SELL" (from decision.direction).

    Returns
    -------
    SniperResult — always non-None, never raises.
    """
    if direction == "NEUTRAL":
        return _pass(["Direction NEUTRAL — sniper gate not applicable"])

    try:
        details: List[str] = []

        # ── CHoCH fast-pass ───────────────────────────────────────────────────
        choch_pass = _recent_choch(candles, smc, direction)
        if choch_pass:
            details.append(
                "CHoCH FAST-PASS: structural reversal in last 3 bars — "
                "gates 1+2 relaxed"
            )
            # Still require candle direction; skip body-ratio check
            c = candles[-1]
            candle_dir_ok = (
                (direction == "BUY"  and c.close >= c.open) or
                (direction == "SELL" and c.close <= c.open)
            )
            if not candle_dir_ok:
                return _block(
                    f"CHoCH fast-pass: candle direction disagrees with {direction}",
                    details, choch_fastpass=True,
                )
            details.append(f"Candle direction OK for {direction}")
            # Sweep bonus (informational)
            has_sweep, sweep_msg = _sweep_bonus(candles, smc, direction)
            details.append(sweep_msg)
            return _pass(
                details,
                displacement_ok=True,
                choch_fastpass=True,
                has_recent_sweep=has_sweep,
            )

        # ── Gate 1: Displacement candle ───────────────────────────────────────
        disp_ok, disp_msg = _displacement_ok(candles, direction)
        details.append(disp_msg)
        if not disp_ok:
            return _block(disp_msg, details, displacement_ok=False)

        # ── Gate 2: Structural zone (FVG or OB) ───────────────────────────────
        if SNIPER_REQUIRE_ZONE:
            zone_ok, in_fvg, in_ob, zone_msg = _zone_check(candles, smc, direction)
            details.append(zone_msg)
            if not zone_ok:
                return _block(zone_msg, details, displacement_ok=True, in_fvg=False, in_ob=False)
        else:
            in_fvg = in_ob = False
            details.append("Zone check DISABLED (SNIPER_REQUIRE_ZONE=false)")

        # ── Gate 3: Liquidity sweep bonus ─────────────────────────────────────
        has_sweep, sweep_msg = _sweep_bonus(candles, smc, direction)
        details.append(sweep_msg)

        return _pass(
            details,
            displacement_ok=True,
            in_fvg=in_fvg,
            in_ob=in_ob,
            has_recent_sweep=has_sweep,
        )

    except Exception as exc:  # noqa: BLE001
        # Fail-safe: never block a trade due to a sniper logic error.
        return SniperResult(
            allowed=True,
            reason="",
            details=[f"Sniper filter error (fail-safe pass): {exc}"],
        )
