"""
State Writer — writes robot_state.json and robot_mt5_snapshot.json
so the Telegram Control Panel can display live status and take commands.
"""
import json
import os
from datetime import datetime, timezone
from typing import List, Optional
from live_trading.signals.decision_engine import DecisionResult
from live_trading.logger import get_logger

# Import env-configured paths from config so that STATE_FILE, MT5_SNAPSHOT,
# and COMMANDS_FILE env-var overrides are honoured here as well as in live_loop.
# config.py has no imports that could cause circular dependency.
from live_trading.config import (
    STATE_FILE    as _CFG_STATE_FILE,
    MT5_SNAPSHOT  as _CFG_SNAPSHOT_FILE,
    COMMANDS_FILE as _CFG_COMMANDS_FILE,
)

log = get_logger()

STATE_FILE    = _CFG_STATE_FILE
SNAPSHOT_FILE = _CFG_SNAPSHOT_FILE
COMMANDS_FILE = _CFG_COMMANDS_FILE

# Track last N trade log entries in state
MAX_TRADE_HISTORY = 50

# Maximum age (in seconds) for a queued command before it is considered stale.
# Prevents commands that were issued before a crash from replaying on restart.
_COMMAND_MAX_AGE_SECONDS = int(os.getenv("COMMAND_MAX_AGE_SECONDS", "300"))

# Explicit mapping from Telegram panel command names to engine dict keys.
# Panel commands not present in this table are silently ignored.
_PANEL_COMMAND_MAP: dict = {
    "PAUSE":          "pause",
    "RESUME":         "resume",
    "EMERGENCY_STOP": "stop",
    "SAFE_SHUTDOWN":  "stop",
    "CLOSE_ALL":      "close_all",
    "RESET_GUARDIAN": "reset_guardian",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as exc:
        log.error(f"State write failed ({path}): {exc}")
    # Mirror to Redis for cross-service IPC on Render (no-op if REDIS_URL not set)
    try:
        from live_trading.redis_ipc import redis_write_state, redis_write_snapshot
        if path == STATE_FILE:
            redis_write_state(data)
        elif path == SNAPSHOT_FILE:
            redis_write_snapshot(data)
    except Exception as exc:
        log.debug(f"Redis mirror skipped ({path}): {exc}")


# ── Write robot_state.json ────────────────────────────────────────────────────

def write_robot_state(
    status:            str,        # "RUNNING", "PAUSED", "WAITING", "SCANNING"
    decision:          Optional[DecisionResult],
    open_position:     Optional[dict],
    account_info:      dict,
    trade_history:     List[dict],
    loop_count:        int,
    last_signal_time:  Optional[str] = None,
    extra:             Optional[dict] = None,
) -> None:

    pos_data = None
    if open_position:
        pos_data = {
            "ticket":    open_position.get("ticket"),
            "symbol":    open_position.get("symbol"),
            "direction": open_position.get("type"),
            "lot_size":  open_position.get("volume"),
            "entry":     open_position.get("open_price"),
            "sl":        open_position.get("sl"),
            "tp":        open_position.get("tp"),
            "profit":    open_position.get("profit"),
            "open_time": open_position.get("open_time"),
        }

    dec_data = None
    if decision:
        dec_data = {
            "allowed":    decision.allowed,
            "direction":  decision.direction,
            "confidence": decision.confidence,
            "grade":      decision.grade,
            "regime":     decision.regime,
            "regime_label": decision.regime_label,
            "reasoning":  decision.reasoning[:8],
            "blocked_reasons": decision.blocked_reasons,
            "components": {
                "smc":        decision.components.smc_score,
                "trend":      decision.components.trend_score,
                "pa":         decision.components.pa_score,
                "wyckoff":    decision.components.wyckoff_score,
                "liquidity":  decision.components.liquidity_score,
                "volatility": decision.components.volatility_score,
                "total":      decision.components.total,
            },
            "trade_params": {
                "entry":  decision.trade_params.entry_price,
                "sl":     decision.trade_params.stop_loss,
                "tp":     decision.trade_params.take_profit,
                "lot":    decision.trade_params.lot_size,
                "rr":     decision.trade_params.risk_reward_ratio,
                "risk_usd": decision.trade_params.risk_amount,
            } if decision.trade_params else None,
        }

    # Derive connection/MT5 status from the trading status string.
    # "RUNNING", "WAITING", "SCANNING", "HOLDING", "PAUSED" all mean the
    # engine is connected to the broker; anything else is disconnected.
    _connected = status.upper() in ("RUNNING", "WAITING", "SCANNING", "HOLDING", "PAUSED")
    _conn_str = "connected" if _connected else "disconnected"

    # Convenience account dict (legacy "account" key kept for backward compat)
    _balance = account_info.get("balance", 0)
    _equity  = account_info.get("equity",  0)
    _account_dict = {
        "balance":     _balance,
        "equity":      _equity,
        # connector.get_account_info() does NOT return a "profit" field.
        # Compute it as equity − balance (= floating P&L on open positions).
        # Fall back to the API's "profit" field if it IS present (e.g. future
        # mt5rest versions that expose it directly).
        "profit":      account_info.get("profit", _equity - _balance),
        "margin":      account_info.get("margin", 0),
        "margin_free": account_info.get("freeMargin", account_info.get("free_margin", 0)),
        "currency":    account_info.get("currency", "USD"),
        "leverage":    account_info.get("leverage", 100),
    }

    state = {
        "status":           status,
        # Fields read by the Telegram panel's robot_service.py
        "connection_status": _conn_str,
        "mt5_status":        _conn_str,
        "last_heartbeat":    _now_iso(),
        "last_update":       _now_iso(),
        "last_signal_time":  last_signal_time,
        "loop_count":        loop_count,
        # Legacy format (used by some panel views)
        "account": _account_dict,
        # account_info: format expected by telegram_panel/services/mt5_service.py
        "account_info": {
            "balance":          _account_dict["balance"],
            "equity":           _account_dict["equity"],
            "margin":           _account_dict["margin"],
            "free_margin":      _account_dict["margin_free"],
            "floating_profit":  _account_dict["profit"],
            "currency":         _account_dict["currency"],
            "leverage":         _account_dict["leverage"],
            "connection_status": _conn_str,
        },
        "open_position":    pos_data,
        "last_decision":    dec_data,
        "recent_trades":    trade_history[-MAX_TRADE_HISTORY:],
        "trade_count":      len(trade_history),
    }
    if extra:
        state.update(extra)

    _safe_write(STATE_FILE, state)


# ── Write robot_mt5_snapshot.json ─────────────────────────────────────────────

def write_mt5_snapshot(
    candle_time: str,
    price:       float,
    regime:      str,
    adx:         float,
    atr:         float,
    smc_signal:  str,
    trend:       str,
) -> None:
    snap = {
        "timestamp":   _now_iso(),
        "candle_time": candle_time,
        "price":       price,
        "regime":      regime,
        "adx":         round(adx, 1),
        "atr":         round(atr, 4),
        "smc_signal":  smc_signal,
        "trend":       trend,
    }
    _safe_write(SNAPSHOT_FILE, snap)


# ── Read robot_commands.json ──────────────────────────────────────────────────

def read_commands() -> dict:
    # Try Redis first — works across separate Render services
    try:
        from live_trading.redis_ipc import redis_read_commands, redis_available
        if redis_available():
            result = redis_read_commands()
            if result is not None:
                return result
    except Exception:
        pass
    """Read commands written by Telegram panel.  Returns {} if none.

    Supports two on-disk formats:
      • Dict format (legacy / future): ``{"pause": true, ...}`` — returned as-is.
      • List format (current panel):   ``[{"command": "PAUSE", "issued_at": "..."}]``
        — translated to engine dict keys; unknown commands and stale entries
        are silently discarded.

    Invalid or corrupted files always return {} without raising.
    """
    try:
        if not os.path.exists(COMMANDS_FILE):
            return {}
        with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}

    # ── Dict format — backward-compatible pass-through ────────────────────────
    if isinstance(data, dict):
        return data

    # ── List format — translate to engine dict keys ───────────────────────────
    if not isinstance(data, list):
        return {}

    now = datetime.now(timezone.utc)
    result: dict = {}

    for item in data:
        if not isinstance(item, dict):
            continue

        # Stale-command check — discard anything older than the age threshold
        issued_at_str = item.get("issued_at")
        if issued_at_str:
            try:
                issued_at = datetime.fromisoformat(issued_at_str)
                # Normalise naive timestamps to UTC (panel writes UTC without tz)
                if issued_at.tzinfo is None:
                    issued_at = issued_at.replace(tzinfo=timezone.utc)
                age_seconds = (now - issued_at).total_seconds()
                if age_seconds > _COMMAND_MAX_AGE_SECONDS:
                    log.warning(
                        f"Discarding stale command '{item.get('command')}' "
                        f"(age={age_seconds:.0f}s > {_COMMAND_MAX_AGE_SECONDS}s)"
                    )
                    continue
            except Exception:
                # Unparseable timestamp — process the command rather than silently drop it
                pass

        # Translate panel command name → engine dict key
        cmd_name   = str(item.get("command", "")).strip().upper()
        engine_key = _PANEL_COMMAND_MAP.get(cmd_name)
        if engine_key:
            result[engine_key] = True

    return result


def clear_command(key: str) -> None:
    # Clear from Redis too (cross-service IPC on Render)
    try:
        from live_trading.redis_ipc import redis_clear_command, redis_available
        if redis_available():
            redis_clear_command(key)
    except Exception:
        pass
    # Also clear from file (local / single-machine deployments)
    cmds = read_commands()
    if key in cmds:
        del cmds[key]
        _safe_write(COMMANDS_FILE, cmds)


def log_trade(trade_history: List[dict], entry: dict) -> None:
    entry["logged_at"] = _now_iso()
    trade_history.append(entry)
    if len(trade_history) > MAX_TRADE_HISTORY:
        trade_history[:] = trade_history[-MAX_TRADE_HISTORY:]
