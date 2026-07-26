"""
Robot Service — interface between Telegram panel and the trading engine.

Communication strategy (configurable via ROBOT_INTERFACE_MODE):
  'file'   — reads robot state from a JSON file that the engine writes (safest,
              zero coupling to the engine process)
  'http'   — calls an HTTP status endpoint on the engine (optional)
  'socket' — connects to a Unix domain socket (optional)

The file-based mode is the default and safest because it requires zero
modification to the trading engine. The robot writes its state to a JSON file;
this service reads it. Control commands are written to a command inbox file
that the robot polls.
"""

import os
import json
import asyncio
import logging
from datetime import datetime
from typing import Optional, Any
from ..config.constants import RobotStatus, ConnectionStatus

logger = logging.getLogger(__name__)

_DEFAULT_STATE: dict[str, Any] = {
    "status": "stopped",
    "version": "unknown",
    "uptime_seconds": 0,
    "last_heartbeat": None,
    "connection_status": "disconnected",
    "mt5_status": "disconnected",
    "vps_status": "unknown",
    "active_trades": 0,
    "pending_orders": 0,
    "last_error": None,
}


class RobotService:
    """
    Read-only interface to the trading robot state.
    Control commands are written to a command file (inbox).
    All reads are non-blocking — never affects trading performance.
    """

    def __init__(
        self,
        state_path: str = "robot_state.json",
        config_path: str = "robot_config.json",
        interface_mode: str = "file",
    ) -> None:
        self._state_path = state_path
        self._config_path = config_path
        self._cmd_path = state_path.replace("state", "commands")
        self._interface_mode = interface_mode
        self._http_port: int = 0  # not used when Redis IPC is active; prevents AttributeError
        self._cached_state: dict[str, Any] = dict(_DEFAULT_STATE)
        self._cache_ts: Optional[float] = None
        self._cache_ttl: float = 5.0    # seconds

    # ─── Status ──────────────────────────────────────────────────────────────

    async def get_status(self) -> RobotStatus:
        state = await self._read_state()
        raw = state.get("status", "stopped")
        try:
            return RobotStatus(raw)
        except ValueError:
            return RobotStatus.STOPPED

    async def get_state(self) -> dict[str, Any]:
        return await self._read_state()

    async def get_version(self) -> str:
        state = await self._read_state()
        return state.get("version", "v4.0.0")

    async def get_uptime(self) -> int:
        state = await self._read_state()
        return state.get("uptime_seconds", 0)

    async def get_last_heartbeat(self) -> Optional[datetime]:
        state = await self._read_state()
        raw = state.get("last_heartbeat")
        if raw:
            try:
                return datetime.fromisoformat(raw)
            except (ValueError, TypeError):
                pass
        return None

    async def get_mt5_status(self) -> ConnectionStatus:
        state = await self._read_state()
        raw = state.get("mt5_status", "disconnected")
        try:
            return ConnectionStatus(raw)
        except ValueError:
            return ConnectionStatus.DISCONNECTED

    async def get_connection_status(self) -> ConnectionStatus:
        state = await self._read_state()
        raw = state.get("connection_status", "disconnected")
        try:
            return ConnectionStatus(raw)
        except ValueError:
            return ConnectionStatus.DISCONNECTED

    async def get_active_trades_count(self) -> int:
        state = await self._read_state()
        return state.get("active_trades", 0)

    async def get_pending_orders_count(self) -> int:
        state = await self._read_state()
        return state.get("pending_orders", 0)

    # ─── Control Commands ────────────────────────────────────────────────────

    async def start(self) -> bool:
        return await self._send_command("START")

    async def pause(self) -> bool:
        return await self._send_command("PAUSE")

    async def resume(self) -> bool:
        return await self._send_command("RESUME")

    async def emergency_stop(self) -> bool:
        return await self._send_command("EMERGENCY_STOP")

    async def restart_engine(self) -> bool:
        return await self._send_command("RESTART_ENGINE")

    async def restart_telegram(self) -> bool:
        return await self._send_command("RESTART_TELEGRAM")

    async def restart_mt5(self) -> bool:
        return await self._send_command("RESTART_MT5")

    async def safe_shutdown(self) -> bool:
        return await self._send_command("SAFE_SHUTDOWN")

    # ─── Config Push ─────────────────────────────────────────────────────────

    async def push_risk_config(self, config: dict[str, Any]) -> bool:
        return await self._send_command("UPDATE_RISK", payload=config)

    async def push_strategy_config(self, config: dict[str, Any]) -> bool:
        return await self._send_command("UPDATE_STRATEGY", payload=config)

    # ─── Private ─────────────────────────────────────────────────────────────

    async def _read_state(self) -> dict[str, Any]:
        now = asyncio.get_event_loop().time()
        if self._cache_ts and (now - self._cache_ts) < self._cache_ttl:
            return self._cached_state

        # Try Redis first — required when robot and panel run as separate Render services.
        # Without REDIS_URL this is a no-op and falls through to file/http.
        redis_state = await self._read_state_redis()
        if redis_state is not None:
            self._cached_state = self._normalize_state(redis_state)
            self._cache_ts = now
            return self._cached_state

        if self._interface_mode == "file":
            state = await self._read_state_file()
        elif self._interface_mode == "http":
            state = await self._read_state_http()
        else:
            state = dict(_DEFAULT_STATE)

        self._cached_state = self._normalize_state(state)
        self._cache_ts = now
        return self._cached_state

    @staticmethod
    def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
        """Normalize status strings to lowercase for RobotStatus/ConnectionStatus enum compat.

        The trading robot writes uppercase statuses ("RUNNING", "PAUSED", "SCANNING", etc.)
        but the RobotStatus enum uses lowercase values ("running", "paused", "scanning", etc.).
        Without this normalization get_status() always raises ValueError and returns STOPPED,
        the heartbeat monitor never fires RUNNING events, and the dashboard always shows ⚪.
        """
        out = dict(state)
        for key in ("status", "mt5_status", "connection_status"):
            if key in out and isinstance(out[key], str):
                out[key] = out[key].lower()
        return out

    async def _read_state_file(self) -> dict[str, Any]:
        if not os.path.exists(self._state_path):
            return dict(_DEFAULT_STATE)
        try:
            loop = asyncio.get_event_loop()
            def _read():
                with open(self._state_path, "r") as f:
                    return json.load(f)
            state = await loop.run_in_executor(None, _read)
            return {**_DEFAULT_STATE, **state}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read robot state file: {e}")
            return dict(_DEFAULT_STATE)

    async def _read_state_redis(self) -> Optional[dict[str, Any]]:
        """Read robot state from Redis (works across separate Render services)."""
        try:
            from telegram_panel.redis_ipc import redis_read_state as _redis_state, redis_available
            if redis_available():
                result = _redis_state()
                if result is not None:
                    return {**_DEFAULT_STATE, **result}
        except Exception as exc:
            logger.debug(f"Redis read_state failed: {exc}")
        return None

    async def _read_state_http(self) -> dict[str, Any]:
        try:
            import aiohttp
            url = f"http://127.0.0.1:{self._http_port}/status"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return {**_DEFAULT_STATE, **data}
        except Exception as e:
            logger.debug(f"HTTP state read failed: {e}")
        return dict(_DEFAULT_STATE)

    async def _send_command(
        self, command: str, payload: Optional[dict] = None
    ) -> bool:
        """
        Write a command to the robot.
        Tries Redis first (cross-service on Render), falls back to file IPC.
        """        # Try Redis first — required when robot and panel are separate Render services
        if await self._send_command_redis(command, payload):
            return True
        # Fall back to file IPC (single-machine / local deployments)
        cmd_entry = {
            "command": command,
            "payload": payload or {},
            "issued_at": datetime.utcnow().isoformat(),
        }
        try:
            loop = asyncio.get_event_loop()
            def _write():
                os.makedirs(os.path.dirname(self._cmd_path) or ".", exist_ok=True)
                # Read existing commands (queue-style)
                existing = []
                if os.path.exists(self._cmd_path):
                    try:
                        with open(self._cmd_path, "r") as f:
                            existing = json.load(f)
                    except Exception:
                        existing = []
                existing.append(cmd_entry)
                with open(self._cmd_path, "w") as f:
                    json.dump(existing, f, indent=2)
            await loop.run_in_executor(None, _write)
            # Invalidate cache
            self._cache_ts = None
            logger.info(f"Sent command: {command}")
            return True
        except Exception as e:
            logger.error(f"Failed to send command {command}: {e}")
            return False

    async def _send_command_redis(self, command: str, payload: Optional[dict] = None) -> bool:
        """Write a command to Redis (works across separate Render services)."""
        try:
            from telegram_panel.redis_ipc import redis_send_command as _redis_cmd
            ok = _redis_cmd(command, payload)
            if ok:
                self._cache_ts = None
                logger.info(f"Sent command via Redis: {command}")
            return ok
        except Exception as exc:
            logger.warning(f"Redis send_command failed: {exc}")
            return False
