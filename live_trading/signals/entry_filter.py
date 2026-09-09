"""
Entry Filter — Minimum confirmation gate across directional engines.

SMC, EMA Trend, Price Action, and Wyckoff each provide one directional vote.
A trade requires the configured minimum number of aligned engine votes; the
normal profile uses two while the explicit SMC-only profile uses one. Other
safety gates remain independent.
"""
from dataclasses import dataclass
from typing import Literal

MIN_CONFIRMATIONS = 2


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
    require_smc_confirmation: bool = False,
    require_smc_or_pa_trigger: bool = False,
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

    # Every directional engine contributes one vote. The configured minimum
    # decides whether the active profile requires one or multiple confirmations.
    count = sum([smc_ok, trend_ok, pa_ok, wyc_ok])
    # When requested, trend alignment remains a separate hard safety rule.
    # When disabled, any two aligned engine votes can authorize the setup.
    if require_trend_alignment and not trend_ok:
        allowed = False
    elif require_smc_price_action_wyckoff:
        # Backward-compatible option name. Preserve its explicit legacy
        # Price Action + Wyckoff requirement when callers enable this flag.
        allowed = pa_ok and wyc_ok
    else:
        no_opposing_trigger = not (
            (smc_signal in {"BUY", "SELL"} and not smc_ok)
            or (pa_signal in {"BUY", "SELL"} and not pa_ok)
        )
        trigger_ok = smc_ok or pa_ok
        allowed = count >= min_confirmations and (
            not require_smc_or_pa_trigger or (trigger_ok and no_opposing_trigger)
        ) and (
            not require_price_action or pa_ok
        ) and (
            not require_smc_confirmation or smc_ok
        )

    return EntryFilterResult(
        allowed=allowed,
        direction=direction if allowed else "NEUTRAL",  # type: ignore
        confirmation_count=count,
        smc=smc_ok, trend=trend_ok, price_action=pa_ok, wyckoff=wyc_ok,
    )
