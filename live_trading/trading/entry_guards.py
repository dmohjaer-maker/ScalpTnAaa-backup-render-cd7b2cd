"""Pure guards for turning a candle signal into an executable quote."""

from math import isfinite


def validate_entry_quote(
    direction: str,
    bid: float,
    ask: float,
    signal_close: float,
    atr: float,
    max_spread_atr: float,
    max_drift_atr: float,
    enforce_limits: bool = True,
) -> tuple[bool, str, float]:
    """Validate the live quote and return the correct executable entry price."""
    if direction == "BUY":
        market_entry = ask
    elif direction == "SELL":
        market_entry = bid
    else:
        return False, "invalid trade direction", 0.0

    values = (bid, ask, signal_close, atr, max_spread_atr, max_drift_atr)
    if not all(isfinite(float(value)) for value in values):
        return False, "non-finite quote or guard input", 0.0
    if bid <= 0 or ask <= 0 or ask <= bid or atr <= 0:
        return False, "invalid bid/ask or ATR", 0.0

    spread = ask - bid
    spread_limit = atr * max_spread_atr
    drift = abs(market_entry - signal_close)
    drift_limit = atr * max_drift_atr

    if enforce_limits and spread > spread_limit:
        return False, f"spread {spread:.2f} exceeds limit {spread_limit:.2f}", market_entry
    if enforce_limits and drift > drift_limit:
        return False, f"price drift {drift:.2f} exceeds limit {drift_limit:.2f}", market_entry

    warnings = []
    if spread > spread_limit:
        warnings.append(f"spread {spread:.2f} above soft limit {spread_limit:.2f}")
    if drift > drift_limit:
        warnings.append(f"price drift {drift:.2f} above soft limit {drift_limit:.2f}")
    return True, "; ".join(warnings), market_entry


def validate_protection_levels(
    direction: str,
    entry: float,
    stop_loss: float,
    take_profit: float,
    required_rr: float = 2.0,
) -> tuple[bool, str, float]:
    """Fail closed when executable SL/TP levels violate the risk policy."""
    if direction not in ("BUY", "SELL"):
        return False, "invalid trade direction", 0.0

    values = (entry, stop_loss, take_profit, required_rr)
    if not all(isfinite(float(value)) for value in values):
        return False, "non-finite entry, SL, TP, or R:R policy", 0.0
    if entry <= 0 or required_rr <= 0:
        return False, "invalid entry or R:R policy", 0.0

    if direction == "BUY":
        risk = entry - stop_loss
        reward = take_profit - entry
    else:
        risk = stop_loss - entry
        reward = entry - take_profit

    if risk <= 0:
        return False, "stop loss is not on the protective side of entry", 0.0
    if reward <= 0:
        return False, "take profit is not on the profitable side of entry", 0.0

    rr = reward / risk
    if rr < required_rr:
        return (
            False,
            f"R:R {rr:.2f} < required {required_rr:.2f}",
            rr,
        )
    return True, "", rr
