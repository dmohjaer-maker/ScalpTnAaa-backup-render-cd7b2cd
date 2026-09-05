"""Instrument-specific market conventions.

The strategy engines were originally calibrated for XAUUSD and several of
their thresholds are expressed in absolute price units.  Reusing those
values for EURUSD silently makes structure, order-block, breakout, and
Wyckoff events impossible to detect.  This module is the single, small
boundary between shared strategy logic and instrument conventions.
"""

from __future__ import annotations


def normalize_symbol(symbol: str | None) -> str:
    return (symbol or "XAUUSD").strip().upper()


def is_eurusd(symbol: str | None) -> bool:
    value = normalize_symbol(symbol).replace(".", "")
    return value.startswith("EURUSD")


def price_digits(symbol: str | None) -> int:
    """Digits used by the broker for the supported instruments."""
    return 5 if is_eurusd(symbol) else 2


def price_round(value: float, symbol: str | None) -> float:
    return round(value, price_digits(symbol))


def pip_size(symbol: str | None) -> float:
    return 0.0001 if is_eurusd(symbol) else 0.01


def execution_step(symbol: str | None) -> float:
    """Minimum meaningful price improvement for entry/trailing telemetry."""
    return pip_size(symbol)


def smc_thresholds(symbol: str | None) -> dict[str, float | int]:
    """Absolute SMC thresholds, calibrated independently per instrument."""
    if is_eurusd(symbol):
        return {
            "fvg_min_size": 0.00020,
            "equal_level_tolerance": 0.00030,
            "liquidity_sweep_min": 0.00040,
            "min_sweep_close_margin": 0.00020,
            "near_ob_threshold": 0.00080,
            "near_fvg_threshold": 0.00040,
            "min_ob_body_size": 0.00030,
            "min_break_distance": 0.00040,
            "price_digits": 5,
        }
    return {
        "fvg_min_size": 0.10,
        "equal_level_tolerance": 0.15,
        "liquidity_sweep_min": 0.15,
        "min_sweep_close_margin": 0.10,
        "near_ob_threshold": 0.50,
        "near_fvg_threshold": 0.30,
        "min_ob_body_size": 0.15,
        "min_break_distance": 0.20,
        "price_digits": 2,
    }


def price_action_thresholds(symbol: str | None) -> dict[str, float | int]:
    """Absolute Price Action thresholds, calibrated independently per instrument."""
    if is_eurusd(symbol):
        return {
            "level_tolerance": 0.00035,
            "breakout_min_body": 0.00025,
            "price_digits": 5,
        }
    return {
        "level_tolerance": 0.35,
        "breakout_min_body": 0.20,
        "price_digits": 2,
    }


def wyckoff_baseline(symbol: str | None) -> dict[str, float | int]:
    """Baseline values used before enough candles exist for calibration."""
    if is_eurusd(symbol):
        return {
            "spring_margin": 0.00040,
            "upthrust_margin": 0.00040,
        }
    return {
        "spring_margin": 0.20,
        "upthrust_margin": 0.20,
    }