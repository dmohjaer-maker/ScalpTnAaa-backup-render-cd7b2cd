"""
Entry Filter — Minimum N of 4 independent confirmations.
Ported from entryFilter.ts
"""
from dataclasses import dataclass
from typing import Literal

MIN_CONFIRMATIONS = 1


@dataclass
class EntryFilterResult:
    allowed: bool
    direction: Literal["BUY", "SELL", "NEUTRAL"]
    confirmation_count: int
    smc: bool
    trend: bool
    price_action: bool
    wyckoff: bool


def apply_entry_filter(
    smc_signal: str,
    ema_trend: str,        # BULLISH / BEARISH / NEUTRAL
    pa_signal: str,
    wyckoff_signal: str,
    min_confirmations: int = MIN_CONFIRMATIONS,
    require_price_action: bool = False,
    require_smc_price_action_wyckoff: bool = False,
    require_trend_alignment: bool = True,
) -> EntryFilterResult:

    blocked = EntryFilterResult(
        allowed=False, direction="NEUTRAL", confirmation_count=0,
        smc=False, trend=False, price_action=False, wyckoff=False,
    )

    if smc_signal == "NEUTRAL":
        return blocked

    direction = smc_signal
    trend_vote = ("BUY" if ema_trend == "BULLISH" else
                  "SELL" if ema_trend == "BEARISH" else "NEUTRAL")

    smc_ok   = True
    trend_ok = trend_vote     == direction
    pa_ok    = pa_signal      == direction
    wyc_ok   = wyckoff_signal == direction

    count = sum([smc_ok, trend_ok, pa_ok, wyc_ok])
    # Trend alignment is a hard safety rule.  No confirmation count or
    # alternate entry mode may authorize a counter-trend trade.
    if require_trend_alignment and not trend_ok:
        allowed = False
    elif require_smc_price_action_wyckoff:
        # Exact option 1: SMC + Price Action + Wyckoff are required, and the
        # hard trend gate above independently requires EMA alignment.
        allowed = smc_ok and pa_ok and wyc_ok
    else:
        allowed = count >= min_confirmations and (
            not require_price_action or pa_ok
        )

    return EntryFilterResult(
        allowed=allowed,
        direction=direction if allowed else "NEUTRAL",  # type: ignore
        confirmation_count=count,
        smc=smc_ok, trend=trend_ok, price_action=pa_ok, wyckoff=wyc_ok,
    )
