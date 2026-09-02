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
    if spread > spread_limit:
        return False, f"spread {spread:.2f} exceeds limit {spread_limit:.2f}", market_entry

    drift = abs(market_entry - signal_close)
    drift_limit = atr * max_drift_atr
    if drift > drift_limit:
        return False, f"price drift {drift:.2f} exceeds limit {drift_limit:.2f}", market_entry

    return True, "", market_entry
