"""Safe AI shadow scorer for GoldScalperPro.

The scorer observes every completed decision but cannot authorize or place trades.
A trained joblib model can be supplied later through AI_MODEL_PATH after validation.
"""
from __future__ import annotations

import os
from typing import Any

try:
    import joblib
except Exception:
    joblib = None

AI_FEATURE_ORDER = (
    "confidence", "trend_component", "smc_component", "pa_component",
    "wyckoff_component", "liquidity_component", "volatility_component",
    "adx", "direction_buy", "direction_sell", "regime_range",
    "dxy_bearish", "baseline_allowed",
)

def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _component(components: Any, name: str) -> float:
    return _number(getattr(components, name, 0.0))

def build_features(decision: Any) -> dict[str, float]:
    components = getattr(decision, "components", None)
    quality = getattr(decision, "quality_filter", None)
    direction = str(getattr(decision, "direction", "NEUTRAL"))
    regime = str(getattr(decision, "regime", ""))
    dxy = str(getattr(decision, "dxy_signal", "NEUTRAL"))
    return {
        "confidence": _number(getattr(decision, "confidence", 0.0)) / 100.0,
        "trend_component": _component(components, "trend"),
        "smc_component": _component(components, "smc"),
        "pa_component": _component(components, "pa"),
        "wyckoff_component": _component(components, "wyckoff"),
        "liquidity_component": _component(components, "liquidity"),
        "volatility_component": _component(components, "volatility"),
        "adx": _number(getattr(quality, "adx", 0.0)),
        "direction_buy": 1.0 if direction == "BUY" else 0.0,
        "direction_sell": 1.0 if direction == "SELL" else 0.0,
        "regime_range": 1.0 if regime == "RANGE" else 0.0,
        "dxy_bearish": 1.0 if dxy == "BEARISH_DXY" else 0.0,
        "baseline_allowed": 1.0 if bool(getattr(decision, "allowed", False)) else 0.0,
    }

def score_decision(decision: Any) -> dict[str, Any]:
    enabled = os.getenv("AI_SHADOW_MODE", "true").strip().lower() not in {"0", "false", "no", "off"}
    features = build_features(decision)
    result: dict[str, Any] = {
        "enabled": enabled,
        "mode": "shadow",
        "model_status": "DISABLED" if not enabled else "NOT_TRAINED",
        "prediction": "UNAVAILABLE",
        "score": None,
        "feature_order": list(AI_FEATURE_ORDER),
        "features": features,
    }
    if not enabled or joblib is None:
        return result
    model_path = os.getenv("AI_MODEL_PATH", "live_trading/models/signal_model.joblib")
    if not os.path.exists(model_path):
        result["model_path"] = model_path
        return result
    try:
        model = joblib.load(model_path)
        vector = [[features[name] for name in AI_FEATURE_ORDER]]
        if not hasattr(model, "predict_proba"):
            result["model_status"] = "INVALID_MODEL"
            return result
        probabilities = model.predict_proba(vector)[0]
        score = float(probabilities[-1])
        result.update({
            "model_status": "READY",
            "score": round(score, 6),
            "prediction": "FAVORABLE" if score >= 0.5 else "UNFAVORABLE",
            "model_path": model_path,
        })
    except Exception as exc:
        result.update({"model_status": "LOAD_ERROR", "error": type(exc).__name__})
    return result
