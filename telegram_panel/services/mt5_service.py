"""
MT5 Service — interface to MetaTrader 5 account data.

Reads live account and position data from MT5 via:
  - Python MetaTrader5 library (if available and on Windows/Wine)
  - JSON state files written by the robot (default, cross-platform)
  - Mock data for testing without an MT5 connection

The robot engine is the authoritative source of live MT5 data.
This service reads the account snapshot file the robot maintains.

FIX: Added base_url parameter and _read_snapshot_http() so the panel
can fetch live MT5 snapshot data via HTTP when Redis is unavailable.
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Any
from ..config.constants import ConnectionStatus, TradeDirection, TradeStatus
from ..models.account import Account
from ..models.trade import Trade, Position, PendingOrder

logger = logging.getLogger(__name__)


class MT5Service:
    """
    Reads live MT5 data from the account snapshot file.
    Falls back to stub data gracefully — never raises to callers.
    """

    def __init__(
        self,
        snapshot_path: str = "robot_mt5_snapshot.json",
        base_url: str = "",
    ) -> None:
        self._snapshot_path = snapshot_path
        self._base_url: str = base_url.rstrip("/") if base_url else ""
        self._cache: dict[str, Any] = {}
        self._cache_ts: Optional[float] = None
        self._cache_ttl: float = 3.0

    async def get_account_info(self, account: Account) -> dict[str, Any]:
        """Return live balance/equity/margin from MT5 snapshot."""
        snapshot = await self._read_snapshot()
        info = snapshot.get("account_info", {})
        return {
            "balance": info.get("balance", account.balance),
            "equity": info.get("equity", account.equity),
            "margin": info.get("margin", account.margin),
            "free_margin": info.get("free_margin", account.free_margin),
            "margin_level": info.get("margin_level", account.margin_level),
            "floating_profit": info.get("floating_profit", account.floating_profit),
            "currency": info.get("currency", account.currency),
            "leverage": info.get("leverage", account.leverage),
            "broker": info.get("broker", account.broker),
            "server": info.get("server", account.server),
            "login": info.get("login", account.login),
            "connection_status": info.get("connection_status", "disconnected"),
        }

    async def get_open_positions(self) -> list[Position]:
        snapshot = await self._read_snapshot()
        positions_raw = snapshot.get("open_positions", [])
        positions = []
        for raw in positions_raw:
            try:
                pos = Position(
                    ticket=raw.get("ticket", 0),
                    symbol=raw.get("symbol", "XAUUSD"),
                    direction=TradeDirection(raw.get("type", "BUY")),
                    volume=raw.get("volume", 0.01),
                    open_price=raw.get("open_price", 0.0),
                    current_price=raw.get("current_price", 0.0),
                    stop_loss=raw.get("sl"),
                    take_profit=raw.get("tp"),
                    open_time=datetime.fromisoformat(raw["open_time"])
                        if raw.get("open_time") else datetime.now(timezone.utc),
                    profit=raw.get("profit", 0.0),
                    commission=raw.get("commission", 0.0),
                    swap=raw.get("swap", 0.0),
                    status=TradeStatus.OPEN,
                    comment=raw.get("comment"),
                    magic=raw.get("magic", 0),
                    floating_profit=raw.get("profit", 0.0),
                    breakeven_activated=raw.get("be_done", False),
                    trailing_stop_active=raw.get("trail_active", False),
                )
                positions.append(pos)
            except Exception as e:
                logger.warning(f"Failed to parse position: {e}")
        return positions

    async def get_pending_orders(self) -> list[PendingOrder]:
        snapshot = await self._read_snapshot()
        orders_raw = snapshot.get("pending_orders", [])
        orders = []
        for raw in orders_raw:
            try:
                order = PendingOrder(
                    ticket=raw.get("ticket", 0),
                    symbol=raw.get("symbol", "XAUUSD"),
                    order_type=raw.get("order_type", "BUY_LIMIT"),
                    volume=raw.get("volume", 0.01),
                    open_price=raw.get("price", 0.0),
                    stop_loss=raw.get("sl"),
                    take_profit=raw.get("tp"),
                    placed_time=datetime.fromisoformat(raw["placed_time"])
                        if raw.get("placed_time") else datetime.now(timezone.utc),
                    comment=raw.get("comment"),
                    magic=raw.get("magic", 0),
                )
                orders.append(order)
            except Exception as e:
                logger.warning(f"Failed to parse pending order: {e}")
        return orders

    async def get_recent_trades(self, limit: int = 20) -> list[Trade]:
        snapshot = await self._read_snapshot()
        trades_raw = snapshot.get("recent_trades", [])
        trades = []
        for raw in trades_raw[-limit:]:
            try:
                trade = Trade(
                    ticket=raw.get("ticket", 0),
                    symbol=raw.get("symbol", "XAUUSD"),
                    direction=TradeDirection(raw.get("type", "BUY")),
                    volume=raw.get("volume", 0.01),
                    open_price=raw.get("open_price", 0.0),
                    close_price=raw.get("close_price", 0.0),
                    stop_loss=raw.get("sl"),
                    take_profit=raw.get("tp"),
                    open_time=datetime.fromisoformat(raw["open_time"])
                        if raw.get("open_time") else datetime.now(timezone.utc),
                    close_time=datetime.fromisoformat(raw["close_time"])
                        if raw.get("close_time") else None,
                    profit=raw.get("profit", 0.0),
                    commission=raw.get("commission", 0.0),
                    swap=raw.get("swap", 0.0),
                    status=TradeStatus.CLOSED,
                    comment=raw.get("comment"),
                    magic=raw.get("magic", 0),
                )
                trades.append(trade)
            except Exception as e:
                logger.warning(f"Failed to parse trade: {e}")
        return trades

    async def get_today_profit(self) -> float:
        """Return today's realised profit from the MT5 snapshot."""
        snapshot = await self._read_snapshot()
        return float(snapshot.get("today_profit", 0.0))

    async def get_floating_profit(self) -> float:
        """Return current floating (unrealised) profit from the MT5 snapshot."""
        snapshot = await self._read_snapshot()
        return float(snapshot.get("floating_profit", 0.0))

    async def get_drawdown(self) -> dict[str, float]:
        snapshot = await self._read_snapshot()
        dd = snapshot.get("drawdown", {})
        return {
            "current_percent": dd.get("current_percent", 0.0),
            "max_percent": dd.get("max_percent", 0.0),
        }

    async def send_trade_command(self, command: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Send a trade command to the robot engine.
        Tries Redis first (works across separate Render containers), then falls
        back to a local file for single-machine deployments.
        """
        # ── Redis path — works cross-service on Render ──────────────────────
        try:
            from ..redis_ipc import redis_send_command, redis_available
            if redis_available():
                ok = redis_send_command(command, params)
                if ok:
                    logger.info(f"Trade command '{command}' sent via Redis")
                    return {"success": True}
                logger.warning(f"Redis send_command returned False for '{command}' — falling back to file")
        except Exception as _e:
            logger.warning(f"Redis send_command error ({_e}) — falling back to file")

        # ── File fallback — single-machine / Redis unavailable ──────────────
        cmd_path = self._snapshot_path.replace("snapshot", "trade_commands")
        cmd = {"command": command, "params": params, "issued_at": datetime.now(timezone.utc).isoformat()}
        try:
            loop = asyncio.get_event_loop()
            def _write():
                existing = []
                if os.path.exists(cmd_path):
                    try:
                        with open(cmd_path) as f:
                            existing = json.load(f)
                    except Exception:
                        existing = []
                existing.append(cmd)
                with open(cmd_path, "w") as f:
                    json.dump(existing, f)
            await loop.run_in_executor(None, _write)
            return {"success": True}
        except Exception as e:
            logger.error(f"Trade command failed: {e}")
            return {"success": False, "error": str(e)}

    async def get_fresh_snapshot(self) -> dict[str, Any]:
        """Force a fresh read bypassing cache. Used for live connection testing."""
        self._cache_ts = None
        return await self._read_snapshot()

    async def _read_snapshot(self) -> dict[str, Any]:
        now = asyncio.get_event_loop().time()
        if self._cache_ts and (now - self._cache_ts) < self._cache_ttl:
            return self._cache

        # 1. Try Redis first — works across separate Render services.
        # Without REDIS_URL this is a no-op and falls through to file reads.
        redis_data = self._read_from_redis()
        if redis_data:
            self._cache = redis_data
            self._cache_ts = now
            return self._cache

        # 2. Try HTTP endpoint on robot (FIX: new fallback when Redis is unavailable)
        if self._base_url:
            http_data = await self._read_snapshot_http()
            if http_data:
                self._cache = http_data
                self._cache_ts = now
                return self._cache

        # 3. Local snapshot file
        if not os.path.exists(self._snapshot_path):
            # Also try the robot state file (has account balance data)
            state_path = self._snapshot_path.replace("mt5_snapshot", "state")
            if os.path.exists(state_path):
                try:
                    loop = asyncio.get_event_loop()
                    def _read_state():
                        with open(state_path) as f:
                            return json.load(f)
                    state_data = await loop.run_in_executor(None, _read_state)
                    data = self._normalize_state_to_snapshot(state_data)
                    self._cache = data
                    self._cache_ts = now
                    return data
                except Exception:
                    pass
            return {}
        try:
            loop = asyncio.get_event_loop()
            def _read():
                with open(self._snapshot_path) as f:
                    return json.load(f)
            data = await loop.run_in_executor(None, _read)
            # If snapshot lacks account_info, try merging from state file
            if "account_info" not in data:
                state_path = self._snapshot_path.replace("mt5_snapshot", "state")
                if os.path.exists(state_path):
                    try:
                        with open(state_path) as f:
                            state_data = json.load(f)
                        data.update(self._normalize_state_to_snapshot(state_data))
                    except Exception:
                        pass
            self._cache = data
            self._cache_ts = now
            return data
        except Exception as e:
            logger.warning(f"Failed to read MT5 snapshot: {e}")
            return {}

    def _read_from_redis(self) -> dict[str, Any]:
        """Read MT5 snapshot from Redis — works across separate Render services."""
        try:
            from ..redis_ipc import (
                redis_read_snapshot, redis_read_state,
                redis_available,
            )
            if not redis_available():
                return {}
            # Snapshot key — written by live_trading per bar
            snap = redis_read_snapshot()
            if snap and "account_info" in snap:
                return snap
            # State key — has account balance / equity written every bar
            state = redis_read_state()
            if state:
                merged = self._normalize_state_to_snapshot(state)
                if snap:
                    # Preserve market data fields from snapshot
                    snap.update(merged)
                    return snap
                return merged
            return snap or {}
        except Exception as e:
            logger.debug(f"Redis snapshot read failed: {e}")
            return {}

    async def _read_snapshot_http(self) -> dict[str, Any]:
        """Fetch MT5 snapshot from the robot's /snapshot HTTP endpoint.

        This is the HTTP fallback when Redis is unavailable or empty.
        The /snapshot endpoint is read-only and requires no authentication.
        """
        if not self._base_url:
            return {}
        url = f"{self._base_url}/snapshot"
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        if isinstance(data, dict):
                            logger.debug("MT5 snapshot fetched via HTTP /snapshot")
                            return data
        except Exception as exc:
            logger.debug(f"HTTP /snapshot fetch failed: {exc}")
        return {}

    @staticmethod
    def _normalize_state_to_snapshot(state: dict) -> dict:
        """Convert robot_state format into the mt5_snapshot format expected by get_account_info()."""
        account = state.get("account", {})
        raw_status = state.get("status", "stopped").upper()
        # PAUSED is intentionally excluded: a paused robot may have lost its MT5
        # connection and must not show "connected" in the panel.  Prefer the
        # explicit connection_status field written by state_writer when present.
        explicit = state.get("connection_status", "").lower()
        if explicit in ("connected", "disconnected", "reconnecting", "failed"):
            conn_status = explicit
        else:
            connected = raw_status in ("RUNNING", "WAITING", "SCANNING", "HOLDING")
            conn_status = "connected" if connected else "disconnected"
        return {
            "account_info": {
                "balance":          account.get("balance", 0.0),
                "equity":           account.get("equity",  0.0),
                "margin":           account.get("margin",  0.0),
                "free_margin":      account.get("margin_free", 0.0),
                "floating_profit":  account.get("profit",  0.0),
                "currency":         account.get("currency", "USD"),
                "leverage":         account.get("leverage", 0),
                "connection_status": conn_status,
            },
            "connection_status": conn_status,
            "today_profit":      account.get("profit", 0.0),
            "floating_profit":   account.get("profit", 0.0),
        }
