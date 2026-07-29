"""
Render web-service wrapper — GoldScalperPro v4 Live Trading Engine.

Design: health server runs forever (process never exits); robot loop
restarts with exponential backoff on any failure.
Self-ping keepalive task pings /health every 14 min to prevent Render
free-tier sleep (no external UptimeRobot required).
"""
import asyncio
import hmac
import json
import os
import sys
import traceback
from datetime import datetime, timezone

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

from aiohttp import web

PORT = int(os.environ.get("PORT", 10000))

# Module-level lock for thread-safe atomic writes to COMMANDS_FILE.
# Must be module-level — a local lock inside the handler provides zero
# mutual exclusion between concurrent requests (each call gets its own lock).
_commands_lock = asyncio.Lock()

# Allowlist for /command endpoint — validated once at import time.
_ALLOWED_COMMANDS = frozenset({
    "PAUSE", "RESUME", "EMERGENCY_STOP", "SAFE_SHUTDOWN",
    "CLOSE_ALL", "RESET_GUARDIAN", "START",
    "RESTART_ENGINE", "RESTART_MT5", "RESTART_TELEGRAM", "RECONNECT",
    "UPDATE_RISK", "UPDATE_STRATEGY",
})

_BACKOFF_BASE = 30
_BACKOFF_MAX  = 300
_backoff      = _BACKOFF_BASE
_robot_status = "STARTING"
# Only truly fatal states (config errors, unhandled crashes) return 503.
# DISCONNECTED is intentionally excluded: the robot is alive and actively
# trying to reconnect to the MT5 bridge.  Returning 503 for DISCONNECTED
# caused Render's health monitor to restart the service on every temporary
# MT5 connection loss, creating an infinite restart loop that prevented the
# robot from ever completing its reconnect backoff.
# RETRY_IN_* is also excluded for the same reason.
# STOPPED is excluded: the supervisor will restart the engine automatically.
_UNHEALTHY_STATUSES = {"CONFIG_ERROR", "ERROR"}
_HEARTBEAT_MAX_AGE_SECONDS = 180

# Self-ping keepalive: ping /health every 14 minutes so Render free-tier
# services never spin down.  14 min < Render's 15-min inactivity threshold.
_KEEPALIVE_INTERVAL_SECONDS = 840  # 14 minutes


def _parse_heartbeat(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        heartbeat = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        return heartbeat
    except (TypeError, ValueError):
        return None


def _health_response(status: str) -> web.Response:
    normalized = status.upper()
    # RETRY_IN_* removed from unhealthy check: process is alive and retrying.
    unhealthy = normalized in _UNHEALTHY_STATUSES
    return web.Response(
        status=503 if unhealthy else 200,
        text=f"OK status={status}",
        content_type="text/plain",
    )


def _heartbeat_is_fresh(value: object, now: datetime | None = None) -> bool:
    """Return whether a state heartbeat is recent enough to prove liveness."""
    heartbeat = _parse_heartbeat(value)
    if heartbeat is None:
        return False
    current = now or datetime.now(timezone.utc)
    age = (current - heartbeat).total_seconds()
    return 0 <= age <= _HEARTBEAT_MAX_AGE_SECONDS


def _command_authorized(req: web.Request) -> tuple[bool, int, str]:
    """Validate the shared secret used by the panel's HTTP command fallback."""
    configured_token = os.environ.get("ROBOT_COMMAND_TOKEN", "")
    if not configured_token:
        return False, 503, "command interface is not configured"

    supplied_token = req.headers.get("X-Robot-Command-Token", "")
    if not supplied_token:
        return False, 401, "missing command authorization"
    if not hmac.compare_digest(supplied_token, configured_token):
        return False, 403, "invalid command authorization"
    return True, 200, ""


def _read_local_state() -> dict | None:
    """Read the local state file when cross-service Redis is unavailable."""
    try:
        from live_trading.config import STATE_FILE
        with open(STATE_FILE, "r", encoding="utf-8") as state_file:
            state = json.load(state_file)
        return state if isinstance(state, dict) else None
    except (OSError, TypeError, ValueError):
        return None


def _read_local_snapshot() -> dict | None:
    """Read the local MT5 snapshot file."""
    try:
        from live_trading.config import MT5_SNAPSHOT
        with open(MT5_SNAPSHOT, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, TypeError, ValueError):
        return None


async def _health(_req):
    # Prefer state written by the engine. Redis is the cross-service source on
    # Render; the local file is the fallback for a Redis outage.
    try:
        from live_trading.redis_ipc import redis_read_state, redis_available
        states = []
        if redis_available():
            state = redis_read_state()
            if state:
                states.append(state)

        local_state = _read_local_state()
        if local_state:
            states.append(local_state)

        fresh_states = [
            state for state in states
            if "status" in state and _heartbeat_is_fresh(state.get("last_heartbeat"))
        ]
        if fresh_states:
            freshest = max(
                fresh_states,
                key=lambda state: _parse_heartbeat(state["last_heartbeat"])
                or datetime.min.replace(tzinfo=timezone.utc),
            )
            return _health_response(str(freshest["status"]))

        # A state exists but none is fresh: this is a real liveness failure
        # even if the Redis client itself is unavailable. When no state exists
        # yet, retain the startup status so the process can finish booting.
        if states:
            return _health_response("DISCONNECTED")
    except Exception:
        pass
    return _health_response(_robot_status)


async def _status(req: web.Request):
    """JSON status endpoint — consumed by the Telegram panel's HTTP fallback.

    FIX: Removed authentication requirement from this read-only endpoint.
    The /command endpoint (write) still requires ROBOT_COMMAND_TOKEN.
    This allows the panel to always read fresh robot state via HTTP fallback
    without depending on the token being configured in the panel environment.

    Priority:
      1. Redis (cross-service, always fresh when available)
      2. Local state file (same-process fallback when Redis is down)
      3. In-memory _robot_status (last resort: only status string, no trade data)
    """
    now = datetime.now(timezone.utc)

    # 1. Try Redis first (cross-service IPC on Render)
    try:
        from live_trading.redis_ipc import redis_read_state, redis_available
        if redis_available():
            state = redis_read_state()
            if state:
                # Annotate with staleness so panel can display a warning
                hb = _parse_heartbeat(state.get("last_heartbeat"))
                if hb:
                    age = (now - hb).total_seconds()
                    state["_data_age_seconds"] = int(age)
                    state["_data_fresh"] = age <= _HEARTBEAT_MAX_AGE_SECONDS
                    if age > _HEARTBEAT_MAX_AGE_SECONDS:
                        state["status"] = "disconnected"
                        state["connection_status"] = "disconnected"
                        state["mt5_status"] = "disconnected"
                else:
                    state["_data_fresh"] = False
                    state["_data_age_seconds"] = -1
                return web.Response(
                    status=200,
                    text=json.dumps(state, default=str),
                    content_type="application/json",
                )
    except Exception:
        pass

    # 2. Fall back to local state file (same container; written by the engine)
    local_state = _read_local_state()
    if local_state:
        hb = _parse_heartbeat(local_state.get("last_heartbeat"))
        if hb:
            age = (now - hb).total_seconds()
            local_state["_data_age_seconds"] = int(age)
            local_state["_data_fresh"] = age <= _HEARTBEAT_MAX_AGE_SECONDS
            if age > _HEARTBEAT_MAX_AGE_SECONDS:
                local_state["status"] = "disconnected"
                local_state["connection_status"] = "disconnected"
                local_state["mt5_status"] = "disconnected"
        else:
            local_state["_data_fresh"] = False
            local_state["_data_age_seconds"] = -1
        return web.Response(
            status=200,
            text=json.dumps(local_state, default=str),
            content_type="application/json",
        )

    # 3. Last resort: return in-memory supervisor status (no data = truly unknown)
    return web.Response(
        status=200,
        text=json.dumps({
            "status": _robot_status.lower(),
            "connection_status": "disconnected",
            "mt5_status": "disconnected",
            "last_heartbeat": None,
            "_data_fresh": False,
            "_data_age_seconds": -1,
        }),
        content_type="application/json",
    )


async def _snapshot(req: web.Request):
    """GET /snapshot — returns the live MT5 account snapshot.

    Read-only endpoint: no authentication required.
    Used by the Telegram panel's MT5Service HTTP fallback to get live
    account balance, positions, and trade data when Redis is unavailable.

    Priority:
      1. Redis snapshot key (cross-service IPC on Render)
      2. Local MT5 snapshot file
      3. Account data from robot state file (fallback)
    """
    now = datetime.now(timezone.utc)

    # 1. Try Redis snapshot key
    try:
        from live_trading.redis_ipc import redis_read_snapshot, redis_available
        if redis_available():
            snap = redis_read_snapshot()
            if snap:
                snap["_fetched_at"] = now.isoformat()
                return web.Response(
                    status=200,
                    text=json.dumps(snap, default=str),
                    content_type="application/json",
                )
    except Exception:
        pass

    # 2. Local MT5 snapshot file
    local_snap = _read_local_snapshot()
    if local_snap:
        local_snap["_fetched_at"] = now.isoformat()
        return web.Response(
            status=200,
            text=json.dumps(local_snap, default=str),
            content_type="application/json",
        )

    # 3. Derive account data from robot state
    local_state = _read_local_state()
    if local_state and "account" in local_state:
        account = local_state.get("account", {})
        derived = {
            "account_info": {
                "balance": account.get("balance", 0.0),
                "equity": account.get("equity", 0.0),
                "margin": account.get("margin", 0.0),
                "free_margin": account.get("margin_free", 0.0),
                "floating_profit": account.get("profit", 0.0),
                "currency": account.get("currency", "USD"),
                "leverage": account.get("leverage", 0),
                "connection_status": local_state.get("connection_status", "disconnected"),
            },
            "open_positions": [],
            "pending_orders": [],
            "_fetched_at": now.isoformat(),
        }
        return web.Response(
            status=200,
            text=json.dumps(derived, default=str),
            content_type="application/json",
        )

    return web.Response(
        status=200,
        text=json.dumps({"account_info": {}, "_fetched_at": now.isoformat()}),
        content_type="application/json",
    )


async def _command(req: web.Request) -> web.Response:
    """POST /command — receive a control command from the Telegram panel.

    Used as an HTTP fallback when Redis is unavailable.  The panel sends
    { "command": "PAUSE"|"RESUME"|"EMERGENCY_STOP"|…, "payload": {} }
    and this handler appends it to COMMANDS_FILE on the robot's local
    filesystem, where the live loop reads it on the next iteration.
    """
    authorized, status, message = _command_authorized(req)
    if not authorized:
        return web.Response(status=status, text=message)

    try:
        data = await req.json()
    except Exception:
        return web.Response(status=400, text="invalid JSON")

    command = str(data.get("command", "")).strip().upper()
    if not command:
        return web.Response(status=400, text="missing 'command' field")

    if command not in _ALLOWED_COMMANDS:
        return web.Response(status=400, text=f"unknown command: {command}")

    try:
        from live_trading.config import COMMANDS_FILE

        cmd_entry = {
            "command":   command,
            "payload":   data.get("payload") or {},
            "issued_at": datetime.now(timezone.utc).isoformat(),
        }

        # Thread-safe atomic file append using the module-level lock.
        # _commands_lock is defined at module level so ALL concurrent requests
        # share the same lock — unlike a local lock which provides no exclusion.
        async with _commands_lock:
            # Ensure the commands file directory exists (e.g. /data/ on a
            # Render persistent disk that may not have been pre-created).
            _cmd_dir = os.path.dirname(COMMANDS_FILE)
            if _cmd_dir:
                os.makedirs(_cmd_dir, exist_ok=True)
            existing: list = []
            if os.path.exists(COMMANDS_FILE):
                try:
                    with open(COMMANDS_FILE, "r", encoding="utf-8") as _f:
                        existing = json.load(_f)
                    if not isinstance(existing, list):
                        existing = []
                except Exception:
                    existing = []
            existing.append(cmd_entry)
            tmp = COMMANDS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as _f:
                json.dump(existing, _f)
            os.replace(tmp, COMMANDS_FILE)

        return web.Response(
            status=200,
            text=json.dumps({"ok": True, "command": command}),
            content_type="application/json",
        )
    except Exception as exc:
        return web.Response(status=500, text=f"server error: {exc}")


async def _run_health_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    app.router.add_get("/status", _status)
    app.router.add_get("/snapshot", _snapshot)
    app.router.add_post("/command", _command)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[health] Listening on 0.0.0.0:{PORT}", flush=True)
    while True:
        await asyncio.sleep(60)


async def _keepalive():
    """Self-ping /health every 14 min to prevent Render free-tier sleep.

    Render spins down free-tier services after 15 minutes of inactivity.
    Pinging our own health endpoint keeps the service awake without relying
    on an external UptimeRobot or cron job.
    """
    # Wait for health server to fully start before first ping.
    await asyncio.sleep(30)
    import aiohttp
    url = f"http://127.0.0.1:{PORT}/health"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    print(f"[keepalive] ping /health → {resp.status}", flush=True)
        except Exception as exc:
            print(f"[keepalive] ping failed: {exc}", flush=True)
        await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)


async def _run_robot_once():
    global _robot_status
    from live_trading.config import MTAPI_URL, MT5_USER, MT5_PASSWORD
    from live_trading.logger import get_logger
    from live_trading.trading.live_loop import GoldScalperLive
    from live_trading.mt5.connector import disconnect as _mt5_disconnect

    log = get_logger()

    if not MTAPI_URL:
        _robot_status = "CONFIG_ERROR"
        raise RuntimeError("MTAPI_URL is not set — cannot start the trading engine.")
    if not MT5_USER or not MT5_PASSWORD:
        _robot_status = "CONFIG_ERROR"
        raise RuntimeError("MT5_USER / MT5_PASSWORD is not set — cannot connect to broker.")

    _robot_status = "STARTING"
    engine = GoldScalperLive()
    try:
        _robot_status = "RUNNING"
        await engine.start()
    finally:
        _robot_status = "STOPPED"
        try:
            await _mt5_disconnect()
        except Exception:
            pass


async def _robot_supervisor():
    global _backoff, _robot_status
    attempt = 0
    while True:
        attempt += 1
        print(f"[supervisor] Starting robot attempt #{attempt} …", flush=True)
        try:
            await _run_robot_once()
            print("[supervisor] Robot exited cleanly — scheduling restart.", flush=True)
            _backoff = _BACKOFF_BASE
        except Exception:
            wait = min(_backoff, _BACKOFF_MAX)
            _backoff = min(_backoff * 2, _BACKOFF_MAX)
            _robot_status = f"RETRY_IN_{wait}s"
            print(
                f"[supervisor] Robot error (attempt #{attempt}), retrying in {wait}s:",
                flush=True,
            )
            traceback.print_exc()
            await asyncio.sleep(wait)
        else:
            await asyncio.sleep(_BACKOFF_BASE)


async def _main():
    print(f"[server] Python {sys.version}  PORT={PORT}", flush=True)
    health = asyncio.create_task(_run_health_server())
    # Give the health server a moment to bind before loading the trading engine.
    await asyncio.sleep(1)
    keepalive  = asyncio.create_task(_keepalive())
    supervisor = asyncio.create_task(_robot_supervisor())
    try:
        await asyncio.gather(health, keepalive, supervisor)
    except Exception:
        traceback.print_exc()
        # Never exit — keep alive even if both tasks somehow die.
        print("[server] gather() raised — entering keep-alive loop", flush=True)
        while True:
            await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("[server] Keyboard interrupt — shutting down.", flush=True)
    except Exception:
        traceback.print_exc()
        # Last-resort: keep process alive even on unexpected crash.
        import time
        while True:
            time.sleep(60)
