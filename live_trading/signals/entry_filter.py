"""
Entry Filter — Minimum confirmation gate with advisory SMC context.

SMC is intentionally a soft confirmation: an aligned SMC signal strengthens a
setup, but a neutral/missing SMC signal must not veto an otherwise-confirmed
trade. Trend alignment remains a hard safety gate.
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
    candidate_direction: str | None = None,
) -> EntryFilterResult:

    blocked = EntryFilterResult(
        allowed=False, direction="NEUTRAL", confirmation_count=0,
        smc=False, trend=False, price_action=False, wyckoff=False,
    )

    # The candidate is selected by the decision engine from the non-SMC
    # directional context when SMC is neutral or disagrees. Keep accepting the
    # old call shape for standalone callers by falling back to smc_signal.
    direction = candidate_direction or smc_signal
    if direction not in {"BUY", "SELL"}:
        return blocked

    trend_vote = ("BUY" if ema_trend == "BULLISH" else
                  "SELL" if ema_trend == "BEARISH" else "NEUTRAL")

    # SMC is a real vote only when it explicitly agrees with the candidate.
    # The decision engine applies the global hard veto for opposing SMC; this
    # helper remains a confirmation counter and supports neutral SMC.
    smc_ok   = smc_signal == direction
    trend_ok = trend_vote     == direction
    pa_ok    = pa_signal      == direction
    wyc_ok   = wyckoff_signal == direction

    count = sum([smc_ok, trend_ok, pa_ok, wyc_ok])
    # Trend alignment is a hard safety rule.  No confirmation count or
    # alternate entry mode may authorize a counter-trend trade.
    if require_trend_alignment and not trend_ok:
        allowed = False
    elif require_smc_price_action_wyckoff:
        # Backward-compatible option name. SMC is optional when neutral; this
        # legacy strict mode requires the two non-SMC confirmations.
        allowed = pa_ok and wyc_ok
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
